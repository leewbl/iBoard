"""Import existing runtime artifacts into private, queryable SQLite evidence."""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
from urllib.parse import unquote, urlsplit
import zlib

from workbench import chrome_trace, cli, profile_trace, spindump_trace
from workbench.runtime_forensics import ForensicsError, _tracked


MAX_ARTIFACT = 64 << 20
EXTENSIONS = {'cpu': '.cpuprofile', 'heap': '.heapsnapshot', 'chrome': '.json', 'spindump': '.txt'}


def _load(path, expected):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), 'rb') as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or not 0 < details.st_size <= MAX_ARTIFACT:
            raise ForensicsError('trace must be a bounded regular file')
        raw = stream.read(MAX_ARTIFACT + 1)
    if not raw or len(raw) > MAX_ARTIFACT:
        raise ForensicsError('trace exceeds the input size limit')
    sha = hashlib.sha256(raw).hexdigest()
    if expected is not None and (not re.fullmatch(r'[0-9a-f]{64}', expected) or expected != sha):
        raise ForensicsError('trace SHA-256 differs from the requested evidence')
    compressed = raw.startswith(b'\x1f\x8b')
    if compressed:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as source:
            decoded = source.read(MAX_ARTIFACT + 1)
        if len(decoded) > MAX_ARTIFACT:
            raise ForensicsError('expanded trace exceeds the input size limit')
    else:
        decoded = raw
    content_sha = hashlib.sha256(decoded).hexdigest()
    text = decoded.decode('utf-8-sig')
    if not text.strip():
        raise ForensicsError('trace is empty')
    if text.lstrip()[0] not in '{[':
        return 'spindump', text, raw, sha, compressed, content_sha
    obj = json.loads(text)
    if isinstance(obj, dict) and {'nodes', 'samples', 'timeDeltas'} <= obj.keys():
        kind = 'cpu'
    elif isinstance(obj, dict) and {'snapshot', 'nodes', 'edges', 'strings'} <= obj.keys():
        kind = 'heap'
    elif isinstance(obj, list) or (isinstance(obj, dict) and 'traceEvents' in obj):
        kind = 'chrome'
    else:
        raise ForensicsError('unrecognized trace JSON format')
    return kind, obj, raw, sha, compressed, content_sha


def _source_locator(repo, revision, root):
    line_counts = {}

    def locate(url, line):
        if not url or line is None or line < 1:
            return None, 'location-unavailable'
        try:
            parsed = urlsplit(url)
        except ValueError:
            return None, 'location-unavailable'
        if parsed.scheme == 'file' and not parsed.netloc:
            observed = unquote(parsed.path)
        elif not parsed.scheme and not parsed.netloc:
            observed = parsed.path
        elif parsed.scheme in ('http', 'https') and parsed.hostname and parsed.path:
            try:
                port = parsed.port
            except ValueError:
                return None, 'location-unavailable'
            host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
            authority = host if port in (None, 443 if parsed.scheme == 'https' else 80) else f'{host}:{port}'
            address = parsed._replace(netloc=authority, query='', fragment='').geturl()
            return address, 'reported-browser-location'
        else:
            return None, 'location-unavailable'
        if not observed or '\0' in observed or any(part in ('.', '..') for part in observed.split('/')):
            return None, 'location-unavailable'
        observed_path = PurePosixPath(observed)
        if repo is not None:
            if observed_path.is_absolute():
                relative = str(observed_path.relative_to(root)) if observed_path.is_relative_to(root) else None
            else:
                relative = str(observed_path)
            if relative is None:
                return observed, 'outside-capture-source-root'
            if relative not in line_counts:
                line_counts[relative] = 0
                if _tracked(repo, revision, relative):
                    ref = f'{revision}:{relative}'
                    if int(cli.git(repo, 'cat-file', '-s', ref, timeout=15)) <= 2_000_000:
                        data = subprocess.check_output(
                            ['git', '-C', str(repo), 'cat-file', 'blob', ref], timeout=15)
                        line_counts[relative] = len(data.splitlines())
            if line <= line_counts[relative]:
                return relative, 'verified-git-source'
            return observed, 'reported-location-unverified'
        return observed, 'reported-location'

    return locate


def _write_private(path, content):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
        stream.write(content)


def _import(out, path, loaded, repo, revision, root, class_name):
    kind, data, raw, sha, compressed, content_sha = loaded
    out.mkdir(mode=0o700)
    snapshot = out / ('trace' + EXTENSIONS[kind] + ('.gz' if compressed else ''))
    _write_private(snapshot, raw)
    database = out / 'trace.sqlite'
    _write_private(database, b'')
    db = sqlite3.connect(database)
    try:
        db.execute('PRAGMA trusted_schema=OFF')
        locate = _source_locator(repo, revision, root)
        with db:
            if kind == 'cpu':
                finding = profile_trace.import_cpu(db, data, locate)
            elif kind == 'heap':
                finding = profile_trace.import_heap(db, data, class_name)
            elif kind == 'chrome':
                finding = chrome_trace.import_trace(db, data)
            else:
                finding = spindump_trace.import_trace(db, data)
            if kind in ('chrome', 'spindump'):
                for key in ('duration_groups', 'cpu_frames') if kind == 'chrome' else ('top_frames',):
                    for item in finding.get(key, ()):
                        file, status = locate(item.get('path'), item.get('line'))
                        item.update(source_file=file, source_status=status)
            db.execute('CREATE TABLE capture_identity(sha256 TEXT, content_sha256 TEXT, format TEXT, bytes INTEGER)')
            db.execute('INSERT INTO capture_identity VALUES(?,?,?,?)', (sha, content_sha, kind, len(raw)))
    finally:
        db.close()
    return {'format': kind, 'artifact': {'original': str(path), 'snapshot': str(snapshot), 'sha256': sha,
                                       'content_sha256': content_sha, 'bytes': len(raw), 'gzip': compressed},
            'database': {'path': str(database), 'sha256': cli.digest(database)},
            'source_revision': revision, 'source_root': str(root) if repo is not None else None,
            'finding': finding}


def _compare(kind, before, after):
    old, new = before['finding'], after['finding']
    if kind == 'heap':
        result = {'nodes_delta': new['nodes'] - old['nodes'], 'edges_delta': new['edges'] - old['edges']}
        if 'selected_class' in old and 'selected_class' in new:
            a, b = old['selected_class'], new['selected_class']
            result['selected_class'] = {
                'name': a['name'], 'before_count': a['count'], 'after_count': b['count'],
                'count_delta': b['count'] - a['count'], 'shallow_bytes_delta': b['shallow_bytes'] - a['shallow_bytes'],
                'before_path_length': a['strong_root_path_length'], 'after_path_length': b['strong_root_path_length'],
            }
        return result

    def connect(trial):
        uri = Path(trial['database']['path']).as_uri()
        return sqlite3.connect(uri + '?mode=ro', uri=True)

    a, b = connect(before), connect(after)
    try:
        if kind == 'cpu':
            return {'sampled_us_delta': new['sampled_us'] - old['sampled_us'],
                    'source_frame_deltas': profile_trace.compare_cpu(a, b)}
        if kind == 'chrome':
            return chrome_trace.compare_traces(a, b)
        if old.get('format') != new.get('format'):
            raise ForensicsError('paired macOS traces must use the same capture format')
        coverage = {'baseline': old['coverage'], 'comparison': new['coverage']}
        partial = any(not item['recognized_end_section'] or any(
            count for key, count in item.items() if key != 'recognized_end_section') for item in coverage.values())
        return {'frame_deltas': spindump_trace.compare_traces(a, b, old, new),
                'coverage': {'status': 'partial' if partial else 'complete', **coverage},
                'weight_semantics': 'Zero means no attributable imported samples for a group.'}
    finally:
        a.close()
        b.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--sha256')
    parser.add_argument('--compare', type=Path)
    parser.add_argument('--compare-sha256')
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--revision')
    parser.add_argument('--compare-revision')
    parser.add_argument('--source-root', default='/app')
    parser.add_argument('--compare-source-root')
    parser.add_argument('--class-name')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if (bool(args.repo) != bool(args.revision) or
                (args.compare_revision is not None and (args.compare is None or args.repo is None))
                or (args.compare is not None and args.repo is not None and args.compare_revision is None)
                or (args.compare is None and (args.compare_sha256 or args.compare_source_root))):
            raise ForensicsError('provide an explicit Git revision for each trace that uses source verification')
        name = args.class_name
        if name is not None and (not name or len(name) > 160 or not name.isprintable()):
            raise ForensicsError('heap class name is invalid')
        repo = args.repo.expanduser().resolve(strict=True) if args.repo else None
        out = args.out.expanduser().resolve()
        if repo is not None and out.is_relative_to(repo):
            raise ForensicsError('trace analysis must write outside the selected source repository')
        requested = [('baseline', args.input, args.sha256, args.revision, args.source_root)]
        if args.compare is not None:
            requested.append(('comparison', args.compare, args.compare_sha256,
                              args.compare_revision, args.compare_source_root or args.source_root))
        prepared = []
        for label, path, expected, revision, capture_root in requested:
            path = path.expanduser().absolute()
            root = PurePosixPath(capture_root)
            if not root.is_absolute() or '..' in root.parts:
                raise ForensicsError('capture source root must be an absolute POSIX path')
            if revision is not None and (not revision or revision.startswith('-')):
                raise ForensicsError('select a valid Git commit or ref')
            commit = cli.sha(repo, revision, timeout=15) if revision else None
            loaded = _load(path, expected)
            prepared.append((label, path, loaded, commit, root))
        kind = prepared[0][2][0]
        if any(item[2][0] != kind for item in prepared) or (name is not None and kind != 'heap'):
            raise ForensicsError('paired traces must share a format and class selection requires a heap snapshot')
        out.mkdir(parents=True, mode=0o700)
        strength = 'single-capture-hypothesis'
        if args.compare:
            strength = 'identical-artifact' if prepared[0][2][-1] == prepared[1][2][-1] else 'paired-observation'
        result = {'schema': 1, 'format': kind, 'trials': {}, 'evidence_strength': strength,
                  'causal_scope': 'The artifacts establish recorded observations. Matched workload and controls are needed to confirm a cause.'}
        for label, path, loaded, commit, root in prepared:
            result['trials'][label] = _import(out / label, path, loaded, repo, commit, root, name)
        if args.compare is not None:
            result['comparison'] = _compare(kind, result['trials']['baseline'], result['trials']['comparison'])
        cli.write(out / 'result.json', result)
        print(out / 'result.json')
    except (OSError, EOFError, ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError,
            zlib.error, sqlite3.Error, subprocess.SubprocessError, cli.WorkbenchError) as error:
        parser.exit(1, f'trace forensics: {error}\n')


if __name__ == '__main__':
    main()
