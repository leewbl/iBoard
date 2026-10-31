#!/usr/bin/env python3
"""Conservative GitHub PR guard; refuses delivery when evidence or CI is unclear."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

from workbench import cli, workflow
from workbench.task_lock import locked_task


class DeliveryError(Exception):
    pass


def candidate_diff(repo, base, head):
    try:
        result = subprocess.run(['git', '-C', str(repo), '-c', 'core.quotePath=true', 'diff',
                                 '--no-ext-diff', '--no-textconv', '--no-color', base, head, '--'],
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeliveryError('cannot obtain candidate diff') from error
    if result.returncode or len(result.stdout) > 30000:
        raise DeliveryError('candidate diff unavailable or too large for independent review')
    return result.stdout


class GitHub:
    def set_task_deadline(self, data):
        if data['limits']['seconds'] is None:
            self._deadline = None
            return
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(data['started_at'])).total_seconds()
        self._deadline = time.monotonic() + max(0, data['limits']['seconds'] - elapsed)

    def _bounded(self, maximum):
        deadline = getattr(self, '_deadline', None)
        if deadline is None:
            return maximum
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeliveryError('task time limit reached before GitHub operation')
        return min(maximum, remaining)

    def _call(self, *args):
        try:
            process = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=self._bounded(60))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryError(f'GitHub command unavailable: {exc}') from exc
        if process.returncode:
            raise DeliveryError(f'GitHub request failed: {process.stderr.strip()}')
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise DeliveryError('GitHub returned invalid JSON') from exc

    def job_log(self, repo, job_id):
        try:
            result = subprocess.run(['gh', 'api', f'repos/{repo}/actions/jobs/{job_id}/logs'],
                                    capture_output=True, timeout=self._bounded(60))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryError('cannot read aggregate job log') from exc
        if result.returncode or len(result.stdout) > 1_000_000:
            raise DeliveryError('aggregate job log is unavailable or too large')
        try:
            return result.stdout.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise DeliveryError('aggregate job log is not UTF-8') from exc

    def login(self):
        return self._call('api', 'user')['login']

    def repository(self, repo):
        return self._call('api', f'repos/{repo}')

    def protection(self, repo, branch):
        # 404 and unavailable permission are deliberately both blockers.
        return self._call('api', f'repos/{repo}/branches/{branch}/protection')

    def branch_rules(self, repo, branch):
        return self._call('api', f'repos/{repo}/rules/branches/{branch}')

    def pull(self, repo, number):
        fields = ('number,state,isDraft,isCrossRepository,headRefName,headRefOid,'
                  'baseRefName,baseRefOid,mergeStateStatus,statusCheckRollup')
        return self._call('pr', 'view', str(number), '--repo', repo, '--json', fields)

    def branch(self, repo, branch):
        return self._call('api', f'repos/{repo}/branches/{branch}')['commit']['sha']

    def branch_info(self, repo, branch):
        return self._call('api', f'repos/{repo}/branches/{branch}')

    def fast_forward(self, workspace, repo, base, head):
        _fast_forward_push(workspace, f'https://github.com/{repo}.git', base, head, timeout=self._bounded(120))

    def issue(self, repo, number):
        return self._call('api', f'repos/{repo}/issues/{number}')

    def close_issue(self, repo, number):
        try:
            return self._call('api', '--method', 'PATCH', f'repos/{repo}/issues/{number}',
                              '-f', 'state=closed', '-f', 'state_reason=completed')
        except DeliveryError as error:
            raise DeliveryError('issue close outcome may be unknown; inspect the issue before retrying') from error

    def merge(self, repo, number, sha):
        try:
            process = subprocess.run(['gh', 'pr', 'merge', str(number), '--repo', repo,
                                      '--merge', '--match-head-commit', sha],
                                     capture_output=True, text=True, timeout=self._bounded(60))
        except subprocess.TimeoutExpired as exc:
            raise DeliveryError('merge outcome is unknown after timeout; inspect the PR before retrying') from exc
        except OSError as exc:
            raise DeliveryError(f'merge command could not start: {exc}') from exc
        if process.returncode:
            raise DeliveryError(f'merge refused: {process.stderr.strip()}')


def _fast_forward_push(workspace, remote, base, head, *, timeout=120):
    """Update only an unchanged base, and only to a descendant commit."""
    if not all(re.fullmatch(r'[0-9a-f]{40}', value) for value in (base, head)) or base == head:
        raise DeliveryError('invalid or unchanged commit for fast-forward delivery')
    deadline = time.monotonic() + timeout
    try:
        ancestry = subprocess.run(['git', '-C', str(workspace), 'merge-base', '--is-ancestor', base, head],
                                  capture_output=True, timeout=min(30, timeout), check=False)
    except subprocess.TimeoutExpired as error:
        raise DeliveryError('ancestry check timed out before Git push') from error
    except OSError as error:
        raise DeliveryError(f'Git ancestry check could not start: {type(error).__name__}') from error
    if ancestry.returncode:
        raise DeliveryError('verified candidate is not a descendant of the baseline')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeliveryError('task time limit reached before Git push')
    try:
        push = subprocess.run(['git', '-C', str(workspace), 'push', '--porcelain',
                               f'--force-with-lease=refs/heads/main:{base}', remote,
                               f'{head}:refs/heads/main'], capture_output=True, text=True,
                              timeout=remaining, check=False)
    except subprocess.TimeoutExpired as error:
        raise DeliveryError('push outcome is unknown after timeout; inspect remote before retrying') from error
    except OSError as error:
        raise DeliveryError(f'Git push could not start: {type(error).__name__}') from error
    if push.returncode:
        raise DeliveryError('fast-forward push failed or outcome is unknown; inspect the remote before retrying')


def own_issue_number(source, repo):
    """Return only an explicit issue URL belonging to the destination repository."""
    if not isinstance(source, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        return None
    parsed = urlsplit(source.strip())
    if parsed.scheme != 'https' or parsed.netloc.lower() != 'github.com' or parsed.query or parsed.fragment:
        return None
    match = re.fullmatch(r'/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)/?', parsed.path)
    return int(match.group(2)) if match and match.group(1).lower() == repo.lower() else None


@locked_task('task_path')
def close_verified_issue(task_path, repo, pr_number, github=None):
    """Close a specified issue only after its exact candidate PR is merged."""
    github = github or GitHub()
    data = cli.read(Path(task_path))
    issue = own_issue_number(data.get('source'), repo) if data.get('kind') == 'bug' else None
    if issue is None:
        return {'closed': False, 'reason': 'task does not name an issue in the destination repository'}
    if data.get('original_source') != data.get('source'):
        raise DeliveryError('original issue source is missing or has changed')
    head, errors = cli.gate(data, 'HEAD')
    if errors:
        raise DeliveryError('issue closure blocked by evidence gate: ' + '; '.join(errors))
    _independent_verification(data, head)
    if isinstance(github, GitHub):
        github.set_task_deadline(data)
    owner = repo.split('/')[0]
    repository = github.repository(repo)
    if (github.login().lower() != owner.lower() or not repository.get('fork') or
            repository.get('owner', {}).get('login', '').lower() != owner.lower() or
            repository.get('default_branch') != 'main'):
        raise DeliveryError('issue closure is restricted to the signed-in personal fork')
    pull = github.pull(repo, pr_number)
    if (pull.get('state') != 'MERGED' or pull.get('headRefOid') != head or
            pull.get('baseRefName') != 'main' or pull.get('headRefName') != f"workbench/{data['id']}"):
        raise DeliveryError('the verified candidate PR is not confirmed merged')
    current = github.issue(repo, issue)
    if current.get('number') != issue or current.get('pull_request') is not None:
        raise DeliveryError('source URL does not identify a GitHub issue')
    if current.get('state') == 'closed':
        return {'closed': True, 'issue': issue, 'already_closed': True}
    if current.get('state') != 'open':
        raise DeliveryError('issue state is unknown')
    _, errors = cli.gate(cli.read(Path(task_path)), head)
    if errors:
        raise DeliveryError('issue closure blocked before write: ' + '; '.join(errors))
    updated = github.close_issue(repo, issue)
    if updated.get('number') != issue or updated.get('state') != 'closed':
        raise DeliveryError('issue close result was not confirmed; inspect before retrying')
    return {'closed': True, 'issue': issue, 'already_closed': False}


def _valid_run(evidence, actor, kind, commit, repo):
    if evidence['actor'] != actor or evidence['kind'] != kind or evidence['commit'] != commit:
        return False
    try:
        report = json.loads(Path(evidence['path']).read_text())
    except (OSError, ValueError):
        return False
    marker = report.get('expected_marker')
    output = str(report.get('stdout', '')) + '\n' + str(report.get('stderr', ''))
    source_mode = report.get('source_mode')
    if report.get('source_verified_commit') != commit or report.get('workspace') != str(Path(repo).resolve()):
        return False
    if source_mode == 'workspace':
        if not workflow._workspace_command(report.get('command')):
            return False
    elif source_mode == 'image':
        image_id = report.get('verified_image_id')
        if not isinstance(image_id, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id) or report.get('image') != image_id:
            return False
    else:
        return False
    return (report.get('actor') == actor and report.get('kind') == kind
            and report.get('commit') == commit and report.get('container_created') is True
            and report.get('cleanup_confirmed') is True and not report.get('timed_out')
            and not report.get('error') and report.get('exit_code') == report.get('expected_exit')
            and (not marker or marker in output)
            and (kind != 'reproduction' or bool(marker)))


def _valid_screenshot(evidence, actor, commit, repo):
    report_path = evidence.get('execution_report')
    if not report_path:
        return False
    try:
        if cli.digest(report_path) != evidence.get('execution_report_sha256'):
            return False
        report = json.loads(Path(report_path).read_text(encoding='utf-8'))
        if (report.get('screenshot_path') != evidence.get('path') or
                report.get('screenshot_sha256') != evidence.get('sha256')):
            return False
    except (OSError, ValueError, cli.WorkbenchError):
        return False
    return _valid_run({**evidence, 'path': report_path}, actor, 'screenshot', commit, repo)


def _independent_verification(data, target):
    """Require a structured PASS and independent container reruns, not a name alone."""
    desired_ids = {criterion['id'] for criterion in data['criteria']}
    for item in data['evidence']:
        if (item.get('superseded') or item['kind'] != 'independent-review' or item['commit'] != target
                or item['actor'] == data['implementer']
                or item['criteria_digest'] != cli.criteria_digest(data)):
            continue
        try:
            report = json.loads(Path(item['path']).read_text())
        except (OSError, ValueError):
            continue
        if (report.get('verdict') != 'PASS' or report.get('candidate_commit') != target
                or report.get('baseline_commit') != data['base_commit']
                or set(report.get('criteria', [])) != desired_ids
                or report.get('criterion_digest') != cli.criteria_digest(data)):
            continue
        assessments, reports = report.get('assessments'), report.get('reports')
        if not isinstance(assessments, list) or len(assessments) != len(desired_ids) or not isinstance(reports, dict):
            continue
        available = {}
        for label, record in reports.items():
            if not isinstance(label, str) or not isinstance(record, dict):
                continue
            if label == 'DIFF' and record.get('kind') == 'git-diff':
                if record.get('base') != data['base_commit'] or record.get('head') != target:
                    continue
                try:
                    digest = hashlib.sha256(candidate_diff(data['repo'], data['base_commit'], target)).hexdigest()
                except DeliveryError:
                    continue
                if digest == record.get('sha256'):
                    available[label] = 'git-diff'
                continue
            path, expected_hash = record.get('path'), record.get('sha256')
            if not isinstance(path, str) or not isinstance(expected_hash, str):
                continue
            try:
                if cli.digest(path) != expected_hash:
                    continue
            except cli.WorkbenchError:
                continue
            if any((e.get('path') == path and e.get('sha256') == expected_hash) or
                   (e.get('execution_report') == path and e.get('execution_report_sha256') == expected_hash)
                   for e in data['evidence']):
                available[label] = path
        seen, cited = set(), set()
        valid_assessments = True
        for assessment in assessments:
            if not isinstance(assessment, dict):
                valid_assessments = False; break
            cid, references, reason = assessment.get('id'), assessment.get('evidence'), assessment.get('reason')
            if (not isinstance(cid, str) or cid not in desired_ids or cid in seen or
                    assessment.get('verdict') != 'PASS' or not isinstance(reason, str) or not reason.strip() or
                    not isinstance(references, list) or not references or
                    any(not isinstance(ref, str) or ref not in available for ref in references)):
                valid_assessments = False; break
            seen.add(cid); cited.update(references)
        if not valid_assessments or seen != desired_ids or not any(ref != 'DIFF' for ref in cited):
            continue
        actor = item['actor']
        required = [('test-result', target)]
        if data['kind'] == 'bug':
            required += [('reproduction', data['base_commit']), ('resolution', target)]
        if all(any(_valid_run(evidence, actor, kind, commit, data['repo']) for evidence in data['evidence'])
               for kind, commit in required):
            if data.get('surface') == 'ui':
                hashes = report.get('screenshot_hashes')
                if not isinstance(hashes, list) or not any(
                    evidence.get('sha256') in hashes and _valid_screenshot(evidence, actor, target, data['repo']) and
                    any(available.get(ref) == evidence.get('execution_report') for ref in cited)
                    for evidence in data['evidence']):
                    continue
            return
    raise DeliveryError('independent PASS and independent sandbox reruns are not proven')


def _checks_pass(pull, *, required=True):
    checks = pull.get('statusCheckRollup')
    if not isinstance(checks, list) or (required and not checks):
        # Strict branch protection requires explicit status checks; otherwise no
        # atomic guarantee prevents the base moving between the check and merge.
        return False
    for check in checks:
        if not isinstance(check, dict):
            return False
        if 'status' in check:
            if check.get('status') != 'COMPLETED' or check.get('conclusion') != 'SUCCESS':
                return False
        elif check.get('state') != 'SUCCESS':
            return False
    return True


def _attempt_jobs(github, repo, run_id, attempt):
    """Fetch one complete attempt, refusing truncated, duplicate or failed jobs."""
    total_expected = None
    seen = set()
    complete = []
    for page in range(1, 11):
        response = github._call('api', f'repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}')
        if not isinstance(response, dict):
            return None
        jobs, total = response.get('jobs'), response.get('total_count')
        if (type(total) is not int or total < 1 or total > 1000 or not isinstance(jobs, list) or
                (total_expected is not None and total != total_expected) or
                len(jobs) != min(100, max(0, total - (page - 1) * 100))):
            return None
        total_expected = total
        for job in jobs:
            if (not isinstance(job, dict) or type(job.get('id')) is not int or job['id'] < 1 or
                    job['id'] in seen or job.get('run_id') != run_id or
                    ('run_attempt' in job and job['run_attempt'] != attempt) or
                    job.get('status') != 'completed' or job.get('conclusion') not in ('success', 'skipped')):
                return None
            seen.add(job['id'])
            complete.append(job)
        if page * 100 >= total:
            break
    else:
        return None
    return complete if len(seen) == total_expected else None


# This narrowly scoped policy is tied to the audited OpenClaw CI workflow blob.
# Unknown names, workflows or revisions are rejected rather than inferred.
_OPENCLAW_CI_BLOB = 'bdcbb952a17681d9a79d327ce87ee49a8b561b74'
_OPENCLAW_OPTIONAL = {
    # name: (path, frozen blob, event, allowed skipped job names, allowed run names)
    'Auto response': ('.github/workflows/auto-response.yml', '4406609e14b047e9502ba902192f2aee0109bde8', 'pull_request_target', {'auto-response'}, {'Auto response'}),
    'PR context and evidence': ('.github/workflows/real-behavior-proof.yml', '3eef8c3890f9793373519bde27724061c4343b5e', 'pull_request_target', {'PR context and evidence'}, {'PR context and evidence'}),
    'Labeler': ('.github/workflows/labeler.yml', '9ac058b8136d1155c36f75c4ee452d37c4c97b4e', 'pull_request_target', {'backfill-pr-labels', 'label-issues'}, {'Labeler'}),
    'Workflow Sanity': ('.github/workflows/workflow-sanity.yml', 'bc19ea79995a6ef3ea992bae30ce0670341cb83e', 'pull_request', {'generated-doc-baselines'}, {'Workflow Sanity'}),
    'Security Review': ('.github/workflows/security-review.yml', '21ffa44c56b404ab9816dadc15b3760db22f6088', 'pull_request_target', {'reconcile-backlog'}, {'Security Review'}),
    'macOS Periphery Dead Code': ('.github/workflows/macos-periphery.yml', 'fee2012441df00c0d839ef7c155a4d80ccd556fb', 'pull_request', {'Build macOS Periphery reports', 'Build iOS Periphery reports', 'Scan macOS dead code', 'Scan shared kit from macOS', 'Scan iOS dead code', 'Scan shared kit from iOS', 'Intersect shared OpenClawKit dead code'}, {'macOS Periphery Dead Code [report]'}),
    'CodeQL Critical Quality': ('.github/workflows/codeql-critical-quality.yml', 'af34d04f2fa65494c7c1171d1bad112687682f42', 'pull_request', {f'Critical Quality ({category})' for category in ('ui-control-plane', 'web-media-runtime-boundary', 'core-auth-secrets', 'config-boundary', 'gateway-runtime-boundary', 'channel-runtime-boundary', 'network-runtime-boundary', 'agent-runtime-boundary', 'mcp-process-runtime-boundary', 'memory-runtime-boundary', 'session-diagnostics-boundary', 'plugin-sdk-reply-runtime', 'provider-runtime-boundary', 'plugin-boundary', 'plugin-sdk-package-contract')}, {'CodeQL Critical Quality'}),
}
_OPENCLAW_SKIP_LANES = {
    'control-ui-performance': ('control-ui-performance',),
    'native-i18n': ('native-i18n',),
    "needs.preflight.outputs.compatibility_target == 'true' && 'checks-ui' || format('checks-ui ({0}/{1})', matrix.shard, needs.preflight.outputs.ui_test_shard_count)": ('checks-ui',),
    'checks-ui-e2e (${{ matrix.shard }}/${{ matrix.shard_count }})': ('checks-ui-e2e',),
    "matrix.shard_count == 1 && 'checks-ui-e2e-real-gateway' || format('checks-ui-e2e-real-gateway ({0}/{1})', matrix.shard, matrix.shard_count)": ('checks-ui-e2e-real-gateway',),
    'control-ui-i18n': ('control-ui-i18n',),
    'QA Smoke CI (${{ matrix.name }})': ('qa-smoke-ci-profile',),
    'checks-node-compat-node24': ('checks-node-compat',),
    'check-plan': ('check-plan',),
    "needs.preflight.outputs.shared_sdk_declarations == 'true' && 'check-additional-extension-package-boundary' || 'shared-sdk-declarations'": ('check-extension-package-boundary',),
    'check-docs': ('check-docs',), 'skills-python': ('skills-python',),
    "matrix.check_name || 'checks-windows'": ('checks-windows',),
    "matrix.check_name || 'macos-node'": ('macos-node',),
    # Both workflow jobs produce this same skipped matrix display name.
    'macos-swift (${{ matrix.phase }})': ('macos-swift', 'macos-swift-packages'),
    'ios-build (${{ matrix.phase }})': ('ios-build',),
    'ios-release-e2e': ('ios-release-e2e',), 'ios-screenshots': ('ios-screenshot-capture',),
    'ios-screenshot-evidence': ('ios-screenshot-evidence',),
    'android-release-e2e': ('android-release-e2e',),
    "matrix.check_name || 'android'": ('android',),
    'android-screenshots': ('android-screenshots',), 'android-access-native': ('android-access-native',),
    'docker-seed-e2e': ('docker-seed-e2e',), 'published-driver-update': ('published-driver-update',),
    'check-lint-extensions-${{ matrix.stripe }}': ('check-lint-hosted-extension-shard',),
}


def _openclaw_workflow_blob_matches(data, head, path, expected):
    try:
        process = subprocess.run(['git', '-C', str(data['repo']), 'rev-parse',
                                  data['base_commit'] + ':' + path, head + ':' + path],
                                 capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired, KeyError, TypeError):
        return False
    return process.returncode == 0 and process.stdout.splitlines() == [expected, expected]


def _openclaw_ci_blob_matches(data, head):
    if not _openclaw_workflow_blob_matches(data, head, '.github/workflows/ci.yml', _OPENCLAW_CI_BLOB):
        return False
    try:
        diff = subprocess.run(['git', '-C', str(data['repo']), 'diff', '--no-ext-diff', '--quiet',
                               data['base_commit'], head, '--', '.github/actions', 'scripts', 'test/vitest'],
                              capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return diff.returncode == 0


def _openclaw_lane_log(log):
    if not isinstance(log, str) or len(log.encode('utf-8')) > 1_000_000:
        return None
    result = {}
    for line in log.splitlines():
        line = re.sub(r'\x1b\[[0-9;]*m', '', line)
        match = re.search(r'(?:^|\s)([a-z][a-z0-9-]*): (success|skipped|failure|cancelled) \(selected=(true|false|missing)\)\s*$', line)
        if not match:
            continue
        name, state, selected = match.groups()
        if name in result:
            return None
        result[name] = (state, selected)
    return result


def _recorded_skip_policy(data):
    events = data.get('events')
    if not isinstance(events, list):
        return False
    records = [event for event in events if isinstance(event, dict) and event.get('type') == 'ci-skip-policy']
    if len(records) != 1:
        return False
    record = records[0]
    if (record.get('policy') != 'openclaw-selected-v1' or record.get('base_commit') != data.get('base_commit') or
            record.get('required_ci_workflows') != data.get('required_ci_workflows') or
            not isinstance(record.get('reason'), str) or not record['reason'].strip() or
            not isinstance(record.get('source'), str) or not record['source'].strip()):
        return False
    if record.get('mode') == 'creation':
        return record['source'] == 'task-creation'
    if record.get('mode') != 'existing' or record.get('criteria_sha256') != cli.criteria_digest(data):
        return False
    try:
        basis = Path(record['basis']).resolve(strict=True)
        return basis.is_file() and basis.stat().st_size > 0 and cli.digest(basis) == record.get('basis_sha256')
    except (OSError, TypeError, KeyError, cli.WorkbenchError):
        return False


def _openclaw_checks_pass(github, repo, number, pull, data, head, required):
    """Prove skips against a fixed OpenClaw gate and exact GitHub runs, or refuse."""
    if ('CI' not in required or data.get('ci_skip_policy') != 'openclaw-selected-v1' or
            not _recorded_skip_policy(data) or
            not _openclaw_ci_blob_matches(data, head)):
        return False
    checks = pull.get('statusCheckRollup')
    if not isinstance(checks, list) or not checks:
        return False
    verified = {}
    checked_skips = set()
    seen_checks = set()
    for check in checks:
        if not isinstance(check, dict):
            return False
        if 'status' not in check:
            if check.get('state') != 'SUCCESS':
                return False
            continue
        if check.get('status') != 'COMPLETED':
            return False
        is_ci_gate = (check.get('conclusion') == 'SUCCESS' and check.get('workflowName') == 'CI' and
                      check.get('name') == 'openclaw/ci-gate')
        if check.get('conclusion') == 'SUCCESS' and not is_ci_gate:
            continue
        if check.get('conclusion') != 'SKIPPED' and not is_ci_gate:
            return False
        name, workflow_name, url = check.get('name'), check.get('workflowName'), check.get('detailsUrl')
        if not isinstance(name, str) or not isinstance(workflow_name, str) or not workflow_name or not isinstance(url, str):
            return False
        # A required non-CI workflow has no audited aggregate proving its skips.
        if workflow_name in required and workflow_name != 'CI':
            return False
        if workflow_name == 'CI':
            workflow_path, event, run_names = '.github/workflows/ci.yml', 'pull_request', {'CI'}
        else:
            optional = _OPENCLAW_OPTIONAL.get(workflow_name)
            if optional is None:
                return False
            workflow_path, blob, event, allowed_jobs, run_names = optional
            if name not in allowed_jobs or not _openclaw_workflow_blob_matches(data, head, workflow_path, blob):
                return False
        pattern = r'https://github\.com/' + re.escape(repo) + r'/actions/runs/([1-9][0-9]*)/job/([1-9][0-9]*)'
        match = re.fullmatch(pattern, url, re.IGNORECASE)
        if not match:
            return False
        run_id, job_id = map(int, match.groups())
        if (run_id, job_id) in seen_checks:
            return False
        seen_checks.add((run_id, job_id))
        if run_id not in verified:
            runs = github._call('run', 'list', '--repo', repo, '--workflow', workflow_name,
                                '--commit', head, '--event', event,
                                '--json', 'databaseId,workflowName,headSha,headBranch,status,conclusion,event',
                                '--limit', '2')
            if (not isinstance(runs, list) or not runs or not isinstance(runs[0], dict) or
                    runs[0].get('databaseId') != run_id or runs[0].get('workflowName') != workflow_name or
                    runs[0].get('headSha') != head or runs[0].get('headBranch') != pull.get('headRefName') or
                    runs[0].get('event') != event or runs[0].get('status') != 'completed' or
                    runs[0].get('conclusion') not in ('success', 'skipped')):
                return False
            details = github._call('api', f'repos/{repo}/actions/runs/{run_id}')
            if (not isinstance(details, dict) or details.get('id') != run_id or
                    type(details.get('run_attempt')) is not int or details['run_attempt'] != 1 or
                    details.get('name') not in run_names or details.get('path') != workflow_path or
                    details.get('head_sha') != head or
                    details.get('head_branch') != pull.get('headRefName') or details.get('event') != event or
                    details.get('status') != 'completed' or details.get('conclusion') != runs[0]['conclusion']):
                return False
            prs = details.get('pull_requests')
            if not isinstance(prs, list) or not any(
                    isinstance(pr, dict) and pr.get('number') == int(number) and
                    isinstance(pr.get('base'), dict) and pr['base'].get('ref') == 'main' and
                    pr['base'].get('sha') == data['base_commit'] and
                    isinstance(pr.get('head'), dict) and pr['head'].get('ref') == pull.get('headRefName') and
                    pr['head'].get('sha') == head for pr in prs):
                return False
            jobs = _attempt_jobs(github, repo, run_id, 1)
            if jobs is None or (details['conclusion'] == 'skipped' and
                                any(job.get('conclusion') != 'skipped' for job in jobs)):
                return False
            lanes = None
            if workflow_name == 'CI':
                gates = [job for job in jobs if job.get('name') == 'openclaw/ci-gate']
                if (details['conclusion'] != 'success' or len(gates) != 1 or
                        gates[0].get('conclusion') != 'success' or
                        not isinstance(gates[0].get('steps'), list) or
                        not any(isinstance(step, dict) and step.get('name') == 'Verify selected CI lanes' and
                                step.get('status') == 'completed' and step.get('conclusion') == 'success'
                                for step in gates[0]['steps'])):
                    return False
                lanes = _openclaw_lane_log(github.job_log(repo, gates[0]['id']))
                if lanes is None:
                    return False
            latest = github._call('api', f'repos/{repo}/actions/runs/{run_id}')
            stable = ('id', 'run_attempt', 'name', 'path', 'head_sha', 'head_branch', 'event', 'status',
                      'conclusion', 'pull_requests')
            if not isinstance(latest, dict) or any(latest.get(key) != details.get(key) for key in stable):
                return False
            verified[run_id] = (workflow_name, jobs, lanes)
        verified_name, jobs, lanes = verified[run_id]
        if verified_name != workflow_name:
            return False
        matching = [job for job in jobs if job.get('id') == job_id]
        if (len(matching) != 1 or matching[0].get('name') != name or
                matching[0].get('conclusion') != ('success' if is_ci_gate else 'skipped')):
            return False
        if not is_ci_gate:
            checked_skips.add((run_id, job_id))
        if workflow_name == 'CI' and not is_ci_gate:
            lane_names = _OPENCLAW_SKIP_LANES.get(name)
            if not lane_names or any(lanes.get(lane) not in (('skipped', 'false'), ('success', 'false'))
                                     for lane in lane_names):
                return False
    # Never infer aggregate coverage from a different workflow's success.
    ci_runs = [(run_id, jobs) for run_id, (name, jobs, _) in verified.items() if name == 'CI']
    if len(ci_runs) != 1:
        return False
    # The rollup must cover every skipped job from every verified workflow.
    # Otherwise a truncated rollup could hide an inapplicable or unknown job.
    return checked_skips == {(run_id, job['id']) for run_id, (_, jobs, _) in verified.items()
                             for job in jobs if job.get('conclusion') == 'skipped'}


def _required_workflows_pass(github, repo, number, pull, data, head, required):
    """Verify the latest named PR runs directly; other successful checks do not imply CI ran."""
    for name in required:
        runs = github._call('run', 'list', '--repo', repo, '--workflow', name,
                            '--commit', head, '--event', 'pull_request',
                            '--json', 'databaseId,workflowName,headSha,headBranch,status,conclusion,event',
                            '--limit', '2')
        if not isinstance(runs, list) or not runs or not isinstance(runs[0], dict):
            return False
        run = runs[0]
        run_id = run.get('databaseId')
        if (type(run_id) is not int or run_id < 1 or run.get('workflowName') != name or
                run.get('headSha') != head or run.get('headBranch') != pull.get('headRefName') or
                run.get('event') != 'pull_request' or run.get('status') != 'completed' or
                run.get('conclusion') != 'success'):
            return False
        details = github._call('api', f'repos/{repo}/actions/runs/{run_id}')
        if (not isinstance(details, dict) or details.get('id') != run_id or
                type(details.get('run_attempt')) is not int or details['run_attempt'] != 1 or
                details.get('name') != name or details.get('head_sha') != head or
                details.get('head_branch') != pull.get('headRefName') or
                details.get('event') != 'pull_request' or details.get('status') != 'completed' or
                details.get('conclusion') != 'success'):
            return False
        prs = details.get('pull_requests')
        if not isinstance(prs, list) or not any(
                isinstance(pr, dict) and pr.get('number') == int(number) and
                isinstance(pr.get('base'), dict) and pr['base'].get('ref') == 'main' and
                pr['base'].get('sha') == data['base_commit'] and
                isinstance(pr.get('head'), dict) and pr['head'].get('ref') == pull.get('headRefName') and
                pr['head'].get('sha') == head for pr in prs):
            return False
        # A successful workflow conclusion can conceal failures (for example,
        # a workflow-specific known-main-red exception). Check every actual job
        # from this exact attempt, with strict pagination and no unknown states.
        attempt = details['run_attempt']
        jobs = _attempt_jobs(github, repo, run_id, attempt)
        if jobs is None or (any(job.get('conclusion') == 'skipped' for job in jobs) and _checks_pass(pull)):
            # A supposedly all-success rollup omitted at least one skipped job;
            # never trust it as complete or bypass the explicit skip proof.
            return False
        latest = github._call('api', f'repos/{repo}/actions/runs/{run_id}')
        stable = ('id', 'run_attempt', 'name', 'head_sha', 'head_branch', 'event', 'status',
                  'conclusion', 'pull_requests')
        if not isinstance(latest, dict) or any(latest.get(key) != details.get(key) for key in stable):
            return False
    return True


@locked_task('task_path')
def guarded_merge(task_path, repo, number, github=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or int(number) < 1:
        raise DeliveryError('invalid repository or pull request number')
    github = github or GitHub()
    data = cli.read(Path(task_path))
    local_policy = data.get('local_gate')
    try:
        required_workflows = workflow.validate_required_ci_workflows(data.get('required_ci_workflows'), required=not local_policy)
    except workflow.WorkflowError as error:
        raise DeliveryError(str(error)) from error
    head, issues = cli.gate(data, 'HEAD')
    if issues:
        raise DeliveryError('local evidence gate failed: ' + '; '.join(issues))
    _independent_verification(data, head)
    if local_policy:
        from workbench import local_gate
        try:
            local_gate.validate(data, head)
        except local_gate.LocalGateError as error:
            raise DeliveryError(str(error)) from error
    if isinstance(github, GitHub):
        github.set_task_deadline(data)
    owner = repo.split('/')[0]
    if github.login().lower() != owner.lower():
        raise DeliveryError('only the signed-in personal account fork may be merged')
    repository = github.repository(repo)
    if not repository.get('fork') or repository.get('owner', {}).get('login', '').lower() != owner.lower():
        raise DeliveryError('destination is not the signed-in user personal fork')
    branch = repository.get('default_branch')
    if branch != 'main':
        raise DeliveryError('destination default branch is not main')
    branch_info = github.branch_info(repo, branch)
    protected = branch_info.get('protected')
    if type(protected) is not bool:
        raise DeliveryError('cannot determine whether the main branch is protected')
    if local_policy:
        if protected:
            raise DeliveryError('local-only delivery refuses a protected branch')
        rules = github.branch_rules(repo, branch)
        if not isinstance(rules, list) or rules:
            raise DeliveryError('local-only delivery requires no active branch rules')
    if protected:
        protection = github.protection(repo, branch)
        required = protection.get('required_status_checks') or {}
        if required.get('strict') is not True or not (required.get('contexts') or required.get('checks')):
            raise DeliveryError('protected branches need strict required GitHub checks')

    def verify_remote():
        pull = github.pull(repo, number)
        if (pull.get('state') != 'OPEN' or pull.get('isDraft') or pull.get('isCrossRepository')
                or pull.get('headRefName') != f"workbench/{data['id']}"
                or pull.get('headRefOid') != head or pull.get('baseRefName') != branch
                or pull.get('baseRefOid') != data['base_commit']
                or github.branch(repo, branch) != data['base_commit']
                or pull.get('mergeStateStatus') not in (('CLEAN', 'UNSTABLE') if local_policy else ('CLEAN',))
                or cli.read(Path(task_path)).get('required_ci_workflows') != required_workflows
                or cli.read(Path(task_path)).get('ci_skip_policy') != data.get('ci_skip_policy')
                or cli.read(Path(task_path)).get('local_gate') != local_policy
                or (not local_policy and not (_checks_pass(pull) or _openclaw_checks_pass(github, repo, number, pull, data, head, required_workflows)))
                or (not local_policy and not _required_workflows_pass(github, repo, number, pull, data, head, required_workflows))):
            raise DeliveryError('PR head, base, CI or merge state changed or is not ready')
        if local_policy:
            try:
                local_gate.validate(cli.read(Path(task_path)), head)
            except local_gate.LocalGateError as error:
                raise DeliveryError(str(error)) from error
            if github.branch_rules(repo, branch) != []:
                raise DeliveryError('branch rules changed before local-only delivery')
    verify_remote()
    # Detect changed local files/evidence again immediately before contacting GitHub.
    _, issues = cli.gate(cli.read(Path(task_path)), head)
    if issues:
        raise DeliveryError('evidence changed before merge: ' + '; '.join(issues))
    verify_remote()
    if protected:
        _, issues = cli.gate(cli.read(Path(task_path)), head)
        if issues:
            raise DeliveryError('evidence or budget changed before merge: ' + '; '.join(issues))
        github.merge(repo, number, head)
    else:
        # Do not use an indirect merge if protection appeared while validating.
        latest = github.branch_info(repo, branch)
        if latest.get('protected') is not False or latest.get('commit', {}).get('sha') != data['base_commit']:
            raise DeliveryError('main branch protection or base changed before delivery')
        if local_policy and github.branch_rules(repo, branch) != []:
            raise DeliveryError('branch rules changed before local-only push')
        _, issues = cli.gate(cli.read(Path(task_path)), head)
        if issues:
            raise DeliveryError('evidence or budget changed before push: ' + '; '.join(issues))
        github.fast_forward(data['repo'], repo, data['base_commit'], head)
    try:
        final = github.pull(repo, number)
        if not protected:
            for _ in range(3):
                if final.get('state') == 'MERGED':
                    break
                if isinstance(github, GitHub):
                    time.sleep(github._bounded(2))
                else:
                    time.sleep(2)
                final = github.pull(repo, number)
    except DeliveryError as error:
        raise DeliveryError('merge may have completed; inspect the PR and main before retrying') from error
    if (final.get('state') != 'MERGED' or final.get('headRefOid') != head
            or final.get('baseRefName') != branch):
        raise DeliveryError('delivery may have changed main, but GitHub has not confirmed the verified PR merge')
    if not protected:
        try:
            current = github.branch(repo, branch)
        except DeliveryError as error:
            raise DeliveryError('merge may have completed; inspect the PR and main before retrying') from error
        if current != head:
            raise DeliveryError('main changed after delivery; inspect the remote before retrying')
    return {'merged': True, 'repo': repo, 'pr': number, 'verified_head': head}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--repo', required=True, help='signed-in user personal fork owner/repo')
    parser.add_argument('--pr', required=True, type=int)
    args = parser.parse_args(argv)
    print(json.dumps(guarded_merge(args.task, args.repo, args.pr), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (DeliveryError, cli.WorkbenchError, OSError, ValueError, KeyError) as exc:
        print(f'delivery: {exc}', file=sys.stderr)
        sys.exit(2)
