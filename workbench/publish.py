"""Publish a verified task to its owner's GitHub fork; merging is a separate gate."""
from __future__ import annotations

import json
import hashlib
import base64
import os
import unicodedata
import zlib
from pathlib import Path
import re
import subprocess
import tempfile

from workbench import cli, delivery
from workbench.task_lock import locked_task


class PublishError(Exception):
    pass


_SAFE_SIGNALS = (
    ('database is locked', 'database-locked error observed'),
    ('SQLITE_BUSY', 'SQLite busy error observed'),
    ('ERR_SQLITE_ERROR', 'SQLite error observed'),
    ('LOCK_RESOLUTION_OBSERVED', 'lock-recovery scenario completed'),
    ('NON_LOCK_FAILURE_OBSERVED', 'non-lock failure scenario observed'),
    ('LOCK_FAILURE_OBSERVED', 'persistent-lock failure scenario observed'),
    ('RETRY_BEHAVIOR_PASS', 'retry behavior checks passed'),
    ('TimeoutError', 'timeout error observed'),
    ('AssertionError', 'assertion error observed'),
    ('TypeError', 'type error observed'),
    ('ReferenceError', 'reference error observed'),
    ('ECONNRESET', 'connection-reset error observed'),
    ('ECONNREFUSED', 'connection-refused error observed'),
    ('ENOENT', 'file-not-found error observed'),
    ('ETIMEDOUT', 'timeout error observed'),
    ('HTTP 500', 'HTTP 500 response observed'),
    ('HTTP 503', 'HTTP 503 response observed'),
)


def _public_log_excerpt(text):
    """Bound a log excerpt and remove common credentials and private identifiers."""
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    if 'PRIVATE KEY-----' in text:
        return '[log excerpt omitted: private key material]'
    text = re.sub(r'(?i)\b(?:authorization|cookie|set-cookie)["\']?\s*[:=]\s*[^\r\n]+', '[sensitive header redacted]', text)
    text = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer [redacted]', text)
    text = re.sub(r'(?i)https?://[^\s<>"\']+', '[URL omitted]', text)
    text = re.sub(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[email omitted]', text)
    text = re.sub(r'(?i)(["\']?(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|session[_ -]?id|private[_ -]?key|token|password|passwd|passphrase|secret|credential)["\']?\s*[:=]\s*)(?:["\'][^"\'\r\n]*["\']|[^\s,;}]+)',
                  r'\1[redacted]', text)
    text = re.sub(r'(?i)\b(api[_ -]?key|token|password|passwd|passphrase|secret|credential)\s+[^\s,;]+',
                  r'\1 [redacted]', text)
    text = re.sub(r'(?i)\b[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|SESSION)[A-Z0-9_]*\s*=\s*[^\s,;]+',
                  '[sensitive assignment redacted]', text)
    text = re.sub(r'(?i)\b(?:secret|token|password|credential)[-_][A-Za-z0-9._~+/-]{3,}', '[redacted]', text)
    text = re.sub(r'(?i)\b[A-Za-z0-9._-]*(?:secret|password|token|credential)[A-Za-z0-9._-]*\b', '[redacted]', text)
    text = re.sub(r'\b(?:sk|ghp|gho|github_pat|xox[baprs])[-_][A-Za-z0-9._-]{8,}\b', '[redacted]', text)
    text = re.sub(r'\bAKIA[A-Z0-9]{16}\b', '[redacted]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b', '[redacted]', text)
    text = re.sub(r'(?<![A-Za-z0-9])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9])', '[long identifier omitted]', text)
    text = re.sub(r'(?<![A-Za-z0-9_])/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+', '[path omitted]', text)
    text = re.sub(r'(?i)\b[A-Z]:\\(?:[^\\\s]+\\)*[^\\\s]+', '[path omitted]', text)
    text = ''.join(ch for ch in text if ch in '\n\t' or not unicodedata.category(ch).startswith('C'))
    lines = [line.strip()[:240] for line in text.splitlines() if line.strip()]
    if len(lines) > 12:
        lines = lines[:6] + ['[intermediate lines omitted]'] + lines[-6:]
    excerpt = '\n'.join(lines)[:1000]
    return excerpt or '[no printable log output]'


def _report_summary(data, item, commit):
    """Render only fixed, non-secret signals from a hash-bound sandbox report."""
    try:
        path = Path(item['path'])
        if path.stat().st_size > 2_000_000:
            raise PublishError('sandbox evidence changed or exceeds the publication limit')
        raw = path.read_bytes()
        if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise PublishError('sandbox evidence changed or exceeds the publication limit')
        report = json.loads(raw)
        if not delivery._valid_run(item, item['actor'], item['kind'], commit, data['repo']):
            raise PublishError('sandbox evidence does not match the verified commit')
        if (type(report.get('exit_code')) is not int or type(report.get('expected_exit')) is not int or
                not -255 <= report['exit_code'] <= 255):
            raise PublishError('sandbox exit code is invalid')
        output = report.get('stdout', '') + '\n' + report.get('stderr', '')
        if not isinstance(report.get('stdout', ''), str) or not isinstance(report.get('stderr', ''), str):
            raise PublishError('sandbox output is not text')
    except (OSError, ValueError, TypeError, KeyError, cli.WorkbenchError) as error:
        raise PublishError('sandbox evidence is unavailable or invalid') from error
    signals = [description for literal, description in _SAFE_SIGNALS
               if re.search(r'(?<![A-Za-z0-9_])' + re.escape(literal) + r'(?![A-Za-z0-9_])',
                            output, flags=re.IGNORECASE)]
    counts = re.findall(r'\b([0-9]{1,6})\s+passed\b', output, flags=re.IGNORECASE)
    marker_result = 'expected output marker observed' if report.get('expected_marker') else 'no output marker specified'
    summary = f"exit {report['exit_code']} (expected {report['expected_exit']}); {marker_result}"
    if signals:
        summary += '; ' + ', '.join(signals)
    if counts:
        summary += '; test output includes ' + ', '.join(f'{value} passed' for value in counts[:5])
    if not signals and not counts:
        summary += '; no fixed log signal recognized'
    excerpt = ('[stdout]\n' + _public_log_excerpt(report.get('stdout', '')) +
               '\n[stderr]\n' + _public_log_excerpt(report.get('stderr', '')))
    return summary, excerpt


def _feature_doc_link(data, head):
    repo = Path(data['repo']).resolve()
    destination = data.get('delivery_repo')
    if not isinstance(destination, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', destination):
        raise PublishError('feature documentation repository is unavailable')
    for item in data['evidence']:
        if item.get('kind') != 'feature-doc' or item.get('commit') != head or item.get('superseded'):
            continue
        try:
            path = Path(item['path']).resolve(strict=True)
            relative = path.relative_to(repo).as_posix()
            if (not re.fullmatch(r'[A-Za-z0-9_./-]+\.md', relative) or
                    re.search(r'(?:^|[_.\-/])(secret|password|credential|token|private)(?:[_.\-/]|$)', relative, re.I) or
                    cli.digest(path) != item['sha256']):
                continue
            proc = subprocess.run(['git', '-C', str(repo), 'show', f'{head}:{relative}'],
                                  capture_output=True, timeout=30, check=False)
            if proc.returncode or len(proc.stdout) > 1_000_000 or hashlib.sha256(proc.stdout).hexdigest() != item['sha256']:
                continue
            return f'https://github.com/{destination}/blob/{head}/{relative}'
        except (OSError, ValueError, KeyError, cli.WorkbenchError, subprocess.TimeoutExpired):
            continue
    raise PublishError('feature documentation cannot be bound to an accessible candidate file')


def _valid_png(image):
    if not image.startswith(b'\x89PNG\r\n\x1a\n') or len(image) > 5_000_000:
        return False
    position, parts, width, height, ended = 8, [], 0, 0, False
    frame_width, frame_height = 0, 0
    first_image_seen = False
    while position + 12 <= len(image):
        size = int.from_bytes(image[position:position + 4], 'big')
        end = position + 12 + size
        if end > len(image):
            return False
        kind = image[position + 4:position + 8]
        payload = image[position + 8:position + 8 + size]
        crc = int.from_bytes(image[position + 8 + size:end], 'big')
        if zlib.crc32(kind + payload) != crc:
            return False
        if kind == b'IHDR' and position != 8:
            return False
        if position == 8:
            if kind != b'IHDR' or size != 13:
                return False
            width, height = int.from_bytes(payload[:4], 'big'), int.from_bytes(payload[4:8], 'big')
            if not 0 < width <= 10000 or not 0 < height <= 10000:
                return False
            bits, color, compression, filtering, interlace = payload[8:13]
            legal_bits = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
                          4: (8, 16), 6: (8, 16)}
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
            if bits not in legal_bits.get(color, ()) or compression or filtering or interlace not in (0, 1):
                return False
            frame_width, frame_height = width, height
        if kind == b'fcTL' and not first_image_seen:
            if size != 26:
                return False
            frame_width, frame_height = int.from_bytes(payload[4:8], 'big'), int.from_bytes(payload[8:12], 'big')
            offset_x, offset_y = int.from_bytes(payload[12:16], 'big'), int.from_bytes(payload[16:20], 'big')
            if (not frame_width or not frame_height or frame_width + offset_x > width or
                    frame_height + offset_y > height):
                return False
        if kind == b'IDAT':
            first_image_seen = True
            parts.append(payload)
        if kind == b'IEND':
            ended = size == 0 and end == len(image)
            break
        position = end
    if not (width and height and ended and parts):
        return False
    passes = [(0, 0, 1, 1)] if interlace == 0 else [
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
    rows = []
    expected = 0
    for x, y, step_x, step_y in passes:
        pw = max(0, (frame_width - x + step_x - 1) // step_x)
        ph = max(0, (frame_height - y + step_y - 1) // step_y)
        if not pw or not ph:
            continue
        stride = 1 + (pw * channels[color] * bits + 7) // 8
        rows.append((expected, stride, ph))
        expected += stride * ph
    if not expected or expected > 64_000_000:
        return False
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(b''.join(parts), 64_000_001)
        return (len(decoded) == expected and decompressor.eof and not decompressor.unconsumed_tail and
                not decompressor.unused_data and
                all(decoded[offset + row * stride] <= 4 for offset, stride, count in rows for row in range(count)))
    except zlib.error:
        return False


def _safe_exif_orientation(payload):
    """Do not remove EXIF if doing so could change the displayed orientation."""
    if len(payload) < 8 or payload[:2] not in (b'II', b'MM'):
        return False
    endian = 'little' if payload[:2] == b'II' else 'big'
    if int.from_bytes(payload[2:4], endian) != 42:
        return False
    offset = int.from_bytes(payload[4:8], endian)
    if offset < 8 or offset + 2 > len(payload):
        return False
    count = int.from_bytes(payload[offset:offset + 2], endian)
    if count > 1024 or offset + 2 + count * 12 + 4 > len(payload):
        return False
    seen = False
    for index in range(count):
        entry = payload[offset + 2 + index * 12:offset + 14 + index * 12]
        if int.from_bytes(entry[:2], endian) != 0x0112:
            continue
        if seen or int.from_bytes(entry[2:4], endian) != 3 or int.from_bytes(entry[4:8], endian) != 1:
            return False
        seen = True
        if int.from_bytes(entry[8:10], endian) != 1:
            return False
    return True


def _publication_png(image):
    """Remove explicit textual/EXIF/time chunks while preserving rendering chunks byte-for-byte."""
    if not _valid_png(image):
        raise PublishError('screenshot PNG is invalid or unsupported')
    stripped = {b'tEXt', b'zTXt', b'iTXt', b'eXIf', b'tIME'}
    result = bytearray(image[:8])
    position = 8
    while position < len(image):
        size = int.from_bytes(image[position:position + 4], 'big')
        end = position + size + 12
        kind = image[position + 4:position + 8]
        if kind == b'eXIf' and not _safe_exif_orientation(image[position + 8:position + 8 + size]):
            raise PublishError('screenshot EXIF orientation cannot be safely removed')
        if kind not in stripped:
            result.extend(image[position:end])
        position = end
    published = bytes(result)
    if not _valid_png(published):
        raise PublishError('metadata-stripped PNG could not be validated')
    return published


def _reviewed_screenshot(data, head):
    try:
        criteria = {item['id'] for item in data['criteria']}
    except (KeyError, TypeError) as error:
        raise PublishError('screenshot acceptance criteria are unavailable') from error
    for screenshot in data['evidence']:
        if (screenshot.get('kind') != 'screenshot' or screenshot.get('commit') != head or
                screenshot.get('superseded') or screenshot.get('actor') == data['implementer']):
            continue
        try:
            screenshot_path = Path(screenshot['path'])
            report_path = Path(screenshot['execution_report'])
            if (screenshot_path.is_symlink() or report_path.is_symlink() or
                    screenshot_path.resolve(strict=True).parent != report_path.resolve(strict=True).parent or
                    screenshot_path.stat().st_size > 5_000_000):
                continue
            if cli.digest(screenshot['path']) != screenshot['sha256'] or not delivery._valid_screenshot(
                    screenshot, screenshot['actor'], head, data['repo']):
                continue
            image = Path(screenshot['path']).read_bytes()
            if hashlib.sha256(image).hexdigest() != screenshot['sha256'] or not _valid_png(image):
                continue
        except (OSError, KeyError, cli.WorkbenchError):
            continue
        for review in data['evidence']:
            if (review.get('kind') != 'independent-review' or review.get('commit') != head or
                    review.get('superseded') or review.get('actor') == data['implementer'] or
                    review.get('actor') != screenshot.get('actor') or
                    review.get('criteria_digest') != cli.criteria_digest(data)):
                continue
            try:
                review_path = Path(review['path'])
                if (review_path.is_symlink() or review_path.resolve(strict=True).parent != report_path.resolve(strict=True).parent or
                        review_path.stat().st_size > 2_000_000):
                    continue
                review_bytes = review_path.read_bytes()
                if len(review_bytes) > 2_000_000 or hashlib.sha256(review_bytes).hexdigest() != review['sha256']:
                    continue
                record = json.loads(review_bytes)
                if (record.get('verdict') != 'PASS' or record.get('candidate_commit') != head or
                        record.get('baseline_commit') != data['base_commit'] or
                        record.get('criterion_digest') != cli.criteria_digest(data) or
                        set(record.get('criteria', [])) != criteria or
                        screenshot['sha256'] not in record.get('screenshot_hashes', [])):
                    continue
                reports = record.get('reports', {})
                labels = {label for label, item in reports.items() if isinstance(item, dict) and
                          item.get('path') == screenshot.get('execution_report') and
                          item.get('sha256') == screenshot.get('execution_report_sha256')}
                if not any(isinstance(assessment, dict) and
                           labels.intersection(assessment.get('evidence', []))
                           for assessment in record.get('assessments', [])):
                    continue
                # The same review must pass the full independent gate, including every criterion.
                restricted = {**data, 'evidence': [item for item in data['evidence']
                              if item.get('kind') != 'independent-review' or item is review]}
                delivery._independent_verification(restricted, head)
                return image, screenshot, review
            except (OSError, ValueError, TypeError, KeyError, AttributeError, cli.WorkbenchError, delivery.DeliveryError):
                continue
    raise PublishError('no independently reviewed screenshot is bound to this candidate')


def _screenshot_body(task_path, data, head, repo, github):
    """Publish a reviewed screenshot in a standalone, reproducible evidence branch."""
    original, screenshot, review = _reviewed_screenshot(data, head)
    image = _publication_png(original)
    branch = f"workbench-evidence/{data['id']}/{head}"
    manifest = {'schema': 1, 'candidate_commit': head, 'original_screenshot_sha256': screenshot['sha256'],
                'published_screenshot_sha256': hashlib.sha256(image).hexdigest(),
                'execution_report_sha256': screenshot['execution_report_sha256'],
                'independent_review_sha256': review['sha256']}
    manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
    with tempfile.TemporaryDirectory(prefix='workbench-screenshot-') as directory:
        root = Path(directory)
        (root / 'screenshot.png').write_bytes(image)
        (root / 'manifest.json').write_bytes(manifest_bytes)
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': directory,
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
               'GIT_AUTHOR_NAME': 'Workbench Evidence', 'GIT_AUTHOR_EMAIL': 'workbench@invalid',
               'GIT_COMMITTER_NAME': 'Workbench Evidence', 'GIT_COMMITTER_EMAIL': 'workbench@invalid',
               'GIT_AUTHOR_DATE': '2000-01-01T00:00:00+00:00', 'GIT_COMMITTER_DATE': '2000-01-01T00:00:00+00:00'}
        def git(*args):
            try:
                timeout = github._bounded(30) if isinstance(github, delivery.GitHub) else 30
                proc = subprocess.run(['git', '-C', directory, '-c', 'core.hooksPath=/dev/null', *args],
                                      env=env, capture_output=True, text=True, timeout=timeout, check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise PublishError('cannot prepare isolated screenshot evidence') from error
            if proc.returncode:
                raise PublishError('cannot prepare isolated screenshot evidence')
            return proc.stdout.strip()
        git('init', '-q', '--object-format=sha1')
        git('add', '--', 'screenshot.png', 'manifest.json')
        git('commit', '-q', '--no-verify', '-m', 'Bind reviewed screenshot to verified candidate')
        commit = git('rev-parse', 'HEAD')
        screenshot_blob = git('rev-parse', 'HEAD:screenshot.png')
        manifest_blob = git('rev-parse', 'HEAD:manifest.json')
        if not all(re.fullmatch(r'[0-9a-f]{40}', value) for value in (commit, screenshot_blob, manifest_blob)):
            raise PublishError('invalid screenshot evidence commit')
        artifact = {'candidate': head, 'commit': commit, 'sha256': hashlib.sha256(image).hexdigest()}
        body = _pr_body({**data, 'delivery_repo': repo, '_ui_evidence': artifact}, head)
        current = github.remote_head(repo, branch)
        if current is None:
            current_data = cli.read(task_path)
            checked_head, issues = cli.gate(current_data, head)
            if issues or checked_head != head:
                raise PublishError('evidence or budget changed before screenshot publication')
            delivery._independent_verification(current_data, head)
            github.push_new(directory, repo, branch, commit)
        elif current != commit:
            raise PublishError('screenshot evidence branch differs; refusing to overwrite')
        if github.remote_head(repo, branch) != commit:
            raise PublishError('screenshot evidence branch update was not confirmed')
        for blob, expected in ((screenshot_blob, image), (manifest_blob, manifest_bytes)):
            response = github._call('api', f'repos/{repo}/git/blobs/{blob}')
            try:
                encoded = response['content']
                if response.get('sha') != blob or response.get('encoding') != 'base64' or not isinstance(encoded, str):
                    raise ValueError('invalid blob response')
                if len(encoded) > 7_000_000:
                    raise ValueError('remote blob exceeds screenshot bound')
                content = base64.b64decode(''.join(encoded.split()), validate=True)
            except (KeyError, TypeError, ValueError) as error:
                raise PublishError('remote screenshot evidence cannot be verified') from error
            if hashlib.sha256(content).digest() != hashlib.sha256(expected).digest():
                raise PublishError('remote screenshot evidence differs from the verified screenshot')
        return body, artifact


def _pr_body(data, head):
    if data.get('surface') == 'ui':
        artifact, destination = data.get('_ui_evidence'), data.get('delivery_repo')
        if (not isinstance(artifact, dict) or artifact.get('candidate') != head or
                not re.fullmatch(r'[0-9a-f]{40}', str(artifact.get('commit', ''))) or
                not re.fullmatch(r'[0-9a-f]{64}', str(artifact.get('sha256', ''))) or
                not isinstance(destination, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', destination)):
            raise PublishError('UI publication requires an accessible verified screenshot')
    sections = [('reproduction', data['base_commit']), ('resolution', head), ('test-result', head)]
    if data.get('kind') == 'feature':
        sections = [('test-result', head)]
    body = ('This change was prepared by the engineering workbench.\n\n'
            f'Verified candidate: `{head}`\n')
    if data.get('surface') == 'ui':
        body += (f"\n## Screenshot\n\n[View verified screenshot](https://github.com/{destination}/blob/"
                 f"{artifact['commit']}/screenshot.png) (SHA-256 `{artifact['sha256']}`). "
                 f"[View screenshot provenance](https://github.com/{destination}/blob/{artifact['commit']}/manifest.json).\n")
    if data.get('kind') == 'feature':
        body += f'\n## Functional documentation\n\n[View documentation at the verified commit]({_feature_doc_link(data, head)})\n'
    for kind, commit in sections:
        matching = [item for item in data['evidence'] if not item.get('superseded') and
                    item.get('kind') == kind and item.get('commit') == commit and
                    item.get('actor') != data['implementer']]
        if not matching or len(matching) > 20:
            raise PublishError(f'independent {kind} evidence is missing or exceeds publication limits')
        body += f'\n## {kind.replace("-", " ").title()}\n\n'
        for item in matching:
            summary, excerpt = _report_summary(data, item, commit)
            body += f"- {summary}; commit `{commit}`; report SHA-256 `{item['sha256']}`\n"
            body += '\n' + '\n'.join('      ' + line for line in excerpt.splitlines()) + '\n'
    body += '\nLog excerpts are bounded and common credentials, URLs, email addresses, and paths are redacted.\n'
    if len(body.encode('utf-8')) > 60000:
        raise PublishError('PR evidence summary exceeds the publication size limit')
    issue = (delivery.own_issue_number(data.get('source'), data.get('delivery_repo', ''))
             if data.get('kind') == 'bug' and data.get('original_source') == data.get('source') else None)
    if issue is not None:
        body += f'\nResolves #{issue}\n'
    return body


def _matches_pr(pr, repo, number, branch, head, base):
    owner, name = repo.split('/')
    head_repo = pr.get('headRepository')
    head_owner = pr.get('headRepositoryOwner')
    return (pr.get('number') == number and pr.get('isCrossRepository') is False and
            isinstance(head_repo, dict) and isinstance(head_owner, dict) and
            str(head_repo.get('name', '')).lower() == name.lower() and
            str(head_owner.get('login', '')).lower() == owner.lower() and
            pr.get('headRefName') == branch and pr.get('headRefOid') == head and
            pr.get('baseRefName') == 'main' and pr.get('baseRefOid') == base)


class Publisher(delivery.GitHub):
    def remote_head(self, repo, branch):
        try:
            proc = subprocess.run(['git', 'ls-remote', '--heads', f'https://github.com/{repo}.git',
                                   f'refs/heads/{branch}'], capture_output=True, text=True, timeout=self._bounded(60))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PublishError('unable to inspect remote branch') from error
        if proc.returncode:
            raise PublishError('unable to inspect remote branch')
        return proc.stdout.split()[0] if proc.stdout.strip() else None

    def push_new(self, workspace, repo, branch, head):
        # The empty lease atomically rejects any branch created since inspection.
        try:
            proc = subprocess.run(['git', '-C', workspace, 'push',
                                   f'--force-with-lease=refs/heads/{branch}:',
                                   f'https://github.com/{repo}.git', f'{head}:refs/heads/{branch}'],
                                  capture_output=True, text=True, timeout=self._bounded(120))
        except subprocess.TimeoutExpired as error:
            raise PublishError('push outcome unknown after timeout; inspect remote before retrying') from error
        except OSError as error:
            raise PublishError('unable to start branch push') from error
        if proc.returncode:
            raise PublishError('push failed or remote branch changed; inspect remote state')

    def push_update(self, workspace, repo, branch, old, head):
        if not all(re.fullmatch(r'[0-9a-f]{40}', x) for x in (old, head)) or old == head:
            raise PublishError('invalid update revisions')
        try:
            ancestor = subprocess.run(['git', '-C', workspace, 'merge-base', '--is-ancestor', old, head],
                                      capture_output=True, timeout=self._bounded(30))
            if ancestor.returncode:
                raise PublishError('updated candidate must descend from the published candidate')
            proc = subprocess.run(['git', '-C', workspace, 'push',
                                   f'--force-with-lease=refs/heads/{branch}:{old}',
                                   f'https://github.com/{repo}.git', f'{head}:refs/heads/{branch}'],
                                  capture_output=True, text=True, timeout=self._bounded(120))
        except subprocess.TimeoutExpired as error:
            raise PublishError('update outcome unknown after timeout; inspect remote before retrying') from error
        except OSError as error:
            raise PublishError('unable to start branch update') from error
        if proc.returncode:
            raise PublishError('update failed or remote branch changed; inspect remote state')

    def pulls(self, repo, branch):
        return self._call('pr', 'list', '--repo', repo, '--state', 'open', '--head', branch,
                          '--base', 'main', '--json', 'number,headRefName,headRefOid,baseRefName,baseRefOid,isCrossRepository,headRepository,headRepositoryOwner,body')

    def create(self, repo, branch, task_id, data):
        body = data['_prepared_pr_body']
        with tempfile.TemporaryDirectory(prefix='workbench-pr-') as directory:
            path = Path(directory) / 'body.md'
            path.write_text(body, encoding='utf-8')
            try:
                proc = subprocess.run(['gh', 'pr', 'create', '--repo', repo, '--base', 'main',
                                       '--head', branch, '--title', f'Complete verified task {task_id}',
                                       '--body-file', str(path)], capture_output=True, text=True, timeout=self._bounded(120))
            except subprocess.TimeoutExpired as error:
                raise PublishError('PR creation outcome unknown after timeout; inspect GitHub before retrying') from error
            except OSError as error:
                raise PublishError('unable to start PR creation') from error
        if proc.returncode:
            raise PublishError('PR creation failed; inspect GitHub before retrying')
        match = re.search(r'https://github\.com/[^/]+/[^/]+/pull/(\d+)', proc.stdout)
        if not match:
            raise PublishError('PR creation response did not identify a pull request')
        return int(match.group(1))

    def update_body(self, repo, number, body):
        with tempfile.TemporaryDirectory(prefix='workbench-pr-') as directory:
            path = Path(directory) / 'body.md'
            path.write_text(body, encoding='utf-8')
            try:
                proc = subprocess.run(['gh', 'pr', 'edit', str(number), '--repo', repo,
                                       '--body-file', str(path)], capture_output=True, text=True,
                                      timeout=self._bounded(120))
            except subprocess.TimeoutExpired as error:
                raise PublishError('PR description update outcome unknown; inspect remote before retrying') from error
            except OSError as error:
                raise PublishError('unable to start PR description update') from error
        if proc.returncode:
            raise PublishError('PR description update failed; inspect remote before retrying')


@locked_task('task_path')
def publish(task_path, repo, github=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise PublishError('invalid repository')
    task_path = Path(task_path)
    data = cli.read(task_path)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', data.get('id', '')):
        raise PublishError('invalid task ID')
    head, issues = cli.gate(data, 'HEAD')
    if issues:
        raise PublishError('local evidence gate failed: ' + '; '.join(issues))
    delivery._independent_verification(data, head)
    github = github or Publisher()
    if isinstance(github, delivery.GitHub):
        github.set_task_deadline(data)
    owner = repo.split('/')[0]
    if github.login().lower() != owner.lower():
        raise PublishError('destination must belong to signed-in user')
    remote = github.repository(repo)
    if not remote.get('fork') or remote.get('owner', {}).get('login', '').lower() != owner.lower() or remote.get('default_branch') != 'main':
        raise PublishError('destination must be the user personal fork with main as default')
    if github.branch(repo, 'main') != data['base_commit']:
        raise PublishError('base changed since verification')
    if data.get('surface') == 'ui':
        body, artifact = _screenshot_body(task_path, data, head, repo, github)
    else:
        artifact = None
        body = _pr_body({**data, 'delivery_repo': repo}, head)
    branch = f"workbench/{data['id']}"
    current = github.remote_head(repo, branch)
    if current is None:
        checked_head, issues = cli.gate(cli.read(task_path), head)
        if issues or checked_head != head:
            raise PublishError('task evidence or budget changed before branch push')
        github.push_new(data['repo'], repo, branch, head)
    elif current != head:
        raise PublishError('remote task branch differs; refusing to overwrite')
    current_head, issues = cli.gate(cli.read(task_path), head)
    if issues or current_head != head or github.remote_head(repo, branch) != head or github.branch(repo, 'main') != data['base_commit']:
        raise PublishError('task or remote changed while publishing; inspect before retrying')
    pulls = github.pulls(repo, branch)
    if len(pulls) > 1:
        raise PublishError('multiple matching pull requests')
    if pulls:
        pr = pulls[0]
        if not _matches_pr(pr, repo, pr.get('number'), branch, head, data['base_commit']):
            raise PublishError('existing pull request does not match verified task')
        number = pr['number']
        if pr.get('body') != body:
            checked = cli.read(task_path)
            checked_head, issues = cli.gate(checked, head)
            if issues or checked_head != head or _pr_body({**checked, 'delivery_repo': repo, '_ui_evidence': artifact}, head) != body:
                raise PublishError('evidence or budget changed before PR description update')
            github.update_body(repo, number, body)
            pulls = github.pulls(repo, branch)
            if (len(pulls) != 1 or not _matches_pr(pulls[0], repo, number, branch, head, data['base_commit']) or
                    pulls[0].get('body') != body):
                raise PublishError('PR description update was not confirmed')
    else:
        checked_head, issues = cli.gate(cli.read(task_path), head)
        if issues or checked_head != head:
            raise PublishError('task evidence or budget changed before PR creation')
        number = github.create(repo, branch, data['id'], {**data, '_prepared_pr_body': body})
        pulls = github.pulls(repo, branch)
        if (len(pulls) != 1 or not _matches_pr(pulls[0], repo, number, branch, head, data['base_commit']) or
                pulls[0].get('body') != body or github.remote_head(repo, branch) != head or
                github.branch(repo, 'main') != data['base_commit']):
            raise PublishError('created PR identity or evidence description could not be confirmed')
    return {'repo': repo, 'pr': number, 'head': head, 'url': f'https://github.com/{repo}/pull/{number}'}


@locked_task('task_path')
def republish(task_path, repo, number, previous_head, github=None):
    """Update one existing task PR only after fresh verification and an exact lease."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or type(number) is not int or number < 1:
        raise PublishError('invalid repository or PR number')
    if not isinstance(previous_head, str) or not re.fullmatch(r'[0-9a-f]{40}', previous_head):
        raise PublishError('invalid previously published revision')
    task_path = Path(task_path)
    data = cli.read(task_path)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', data.get('id', '')):
        raise PublishError('invalid task ID')
    head, issues = cli.gate(data, 'HEAD')
    if issues or head == previous_head:
        raise PublishError('new candidate is not independently verified or unchanged')
    delivery._independent_verification(data, head)
    github = github or Publisher()
    if isinstance(github, delivery.GitHub):
        github.set_task_deadline(data)
    owner = repo.split('/')[0]
    remote = github.repository(repo)
    if (github.login().lower() != owner.lower() or not remote.get('fork') or
            remote.get('owner', {}).get('login', '').lower() != owner.lower() or
            remote.get('default_branch') != 'main'):
        raise PublishError('destination must be the signed-in personal fork')
    branch = f"workbench/{data['id']}"
    pulls = github.pulls(repo, branch)
    already_updated = (len(pulls) == 1 and _matches_pr(pulls[0], repo, number, branch, head, data['base_commit']) and
                       github.branch(repo, 'main') == data['base_commit'] and github.remote_head(repo, branch) == head)
    if not already_updated:
        if (len(pulls) != 1 or not _matches_pr(pulls[0], repo, number, branch, previous_head, data['base_commit']) or
                github.branch(repo, 'main') != data['base_commit'] or github.remote_head(repo, branch) != previous_head):
            raise PublishError('PR, task branch, or base changed before update')
        checked_head, issues = cli.gate(cli.read(task_path), head)
        if issues or checked_head != head:
            raise PublishError('evidence or budget changed before update')
    if data.get('surface') == 'ui':
        body, artifact = _screenshot_body(task_path, data, head, repo, github)
    else:
        artifact = None
        body = _pr_body({**data, 'delivery_repo': repo}, head)
    if not already_updated:
        checked_head, issues = cli.gate(cli.read(task_path), head)
        if issues or checked_head != head:
            raise PublishError('evidence or budget changed before update')
        github.push_update(data['repo'], repo, branch, previous_head, head)
    if github.remote_head(repo, branch) != head or github.branch(repo, 'main') != data['base_commit']:
        raise PublishError('remote changed during update; inspect before retrying')
    pulls = github.pulls(repo, branch)
    if len(pulls) != 1 or not _matches_pr(pulls[0], repo, number, branch, head, data['base_commit']):
        raise PublishError('PR did not update to the verified candidate')
    checked = cli.read(task_path)
    checked_head, issues = cli.gate(checked, head)
    if issues or checked_head != head:
        raise PublishError('evidence or budget changed before PR description update')
    delivery._independent_verification(checked, head)
    if _pr_body({**checked, 'delivery_repo': repo, '_ui_evidence': artifact}, head) != body:
        raise PublishError('published evidence changed during PR update')
    if pulls[0].get('body') != body:
        github.update_body(repo, number, body)
        pulls = github.pulls(repo, branch)
    if (len(pulls) != 1 or not _matches_pr(pulls[0], repo, number, branch, head, data['base_commit']) or
            pulls[0].get('body') != body or github.remote_head(repo, branch) != head or
            github.branch(repo, 'main') != data['base_commit']):
        raise PublishError('PR description or identity changed; inspect before retrying')
    return {'repo': repo, 'pr': number, 'head': head, 'url': f'https://github.com/{repo}/pull/{number}'}
