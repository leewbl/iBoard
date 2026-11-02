"""Explicit, single-use checkpoint for quota-interrupted first implementation.

Only cooperative local processes protected by task_lock are covered. This is not
a security boundary against noncooperating processes changing the worktree.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from model.provider_limits import classify_provider_limit
from workbench import autopilot, cli
from workbench.task_lock import task_lock


class PartialRecoveryError(autopilot.AutopilotError):
    pass


def _hash(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def _file(path):
    if path.is_symlink() or not path.is_file():
        raise PartialRecoveryError(f'missing or unsafe retained file: {path}')
    return cli.digest(path)


def _git_bytes(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True)
    return result.stdout


def _safe_index_flags(repo):
    flags = _git_bytes(repo, 'ls-files', '-v', '-z')
    if any(entry and not entry.startswith(b'H ') for entry in flags.split(b'\0')):
        raise PartialRecoveryError('assume-unchanged or skip-worktree index flags are unsupported')
    return flags


def _inventory(repo):
    if subprocess.run(['git', '-C', str(repo), 'diff', '--cached', '--quiet']).returncode:
        raise PartialRecoveryError('staged or ambiguous index state is not recoverable')
    flags = _safe_index_flags(repo)
    raw = _git_bytes(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    entries = raw.split(b'\0')
    result = []
    for entry in entries:
        if not entry:
            continue
        if len(entry) < 4 or entry[2:3] != b' ' or entry[:1] not in (b' ', b'?'):
            raise PartialRecoveryError('unsupported git status; preserve worktree')
        if entry[1:2] not in (b'M', b'D', b'?'):
            raise PartialRecoveryError('rename, copy, type or conflicted state is unsupported')
        relative = os.fsdecode(entry[3:])
        path = repo / relative
        if path.is_symlink():
            raise PartialRecoveryError('changed symlink is unsupported')
        if entry[1:2] == b'D':
            result.append({'path': relative, 'status': os.fsdecode(entry[:2]), 'deleted': True})
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not path.resolve().is_relative_to(repo.resolve()):
            raise PartialRecoveryError('changed path is not a safe regular file')
        result.append({'path': relative, 'status': os.fsdecode(entry[:2]),
                       'mode': stat.S_IMODE(info.st_mode), 'sha256': cli.digest(path)})
    if not result:
        raise PartialRecoveryError('no partial work to preserve')
    return {'status_sha256': hashlib.sha256(raw).hexdigest(), 'files': result,
            'index_flags_sha256': hashlib.sha256(flags).hexdigest(),
            'index_sha256': hashlib.sha256(_git_bytes(repo, 'ls-files', '--stage', '-z')).hexdigest()}


def _workspace_identity(data):
    workspace = Path(data['repo'])
    if workspace.is_symlink() or not workspace.is_dir():
        raise PartialRecoveryError('workspace is missing or unsafe')
    workspace = workspace.resolve(strict=True)
    branch = cli.git(workspace, 'branch', '--show-current')
    if not branch:
        raise PartialRecoveryError('detached workspace is unsupported')
    common = Path(cli.git(workspace, 'rev-parse', '--git-common-dir'))
    common = (workspace / common).resolve(strict=True) if not common.is_absolute() else common.resolve(strict=True)
    return workspace, branch, common


def _contract(task, data, state, provider_report):
    if (data.get('kind') != 'bug' or data.get('surface') != 'non-ui' or
            state.get('phase') != 'implementing' or state.get('candidate_commit') or
            data.get('adoption') or data.get('iterations') != 1):
        raise PartialRecoveryError('only interrupted first bug implementation is supported')
    stage_path = task.parent / 'input-stage.json'
    issue = task.parent / 'issue.txt'
    base_file = task.parent / 'input-base.txt'
    stage = cli.read(stage_path)
    issue_digest = _file(issue)
    if (stage.get('phase') != 'ready' or stage.get('request_digest') != issue_digest or
            stage.get('base_digest') != _file(base_file) or
            state.get('issue_digest') != issue_digest or
            stage.get('input_digest') != autopilot._input_digest(task) or
            state.get('input_digest') != autopilot._input_digest(task)):
        raise PartialRecoveryError('original input or stage binding changed')
    workspace, branch, common = _workspace_identity(data)
    base = data['base_commit']
    if cli.sha(workspace, 'HEAD') != base:
        raise PartialRecoveryError('HEAD must remain at original baseline')
    reports = state.get('baseline_reports')
    if not isinstance(reports, list) or len(reports) != 2 or reports[0] == reports[1]:
        raise PartialRecoveryError('two distinct baseline reproduction reports are required')
    evidence = data.get('evidence', [])
    bound_reports = []
    for path, actor in zip(reports, (data['implementer'], 'independent-verifier')):
        report = Path(path)
        report_hash = _file(report)
        match = [item for item in evidence if item.get('path') == str(report) and
                 item.get('sha256') == report_hash and item.get('commit') == base and
                 item.get('kind') == 'reproduction' and item.get('actor') == actor and
                 item.get('criteria_digest') == cli.criteria_digest(data)]
        if len(match) != 1:
            raise PartialRecoveryError('baseline report lacks exact evidence binding')
        raw = json.loads(report.read_text(encoding='utf-8'))
        reproduction = state['plan']['reproduction']
        if (raw.get('commit') != base or raw.get('actor') != actor or
                raw.get('container_created') is not True or raw.get('source_verified_commit') != base or
                raw.get('source_mode') != stage['run_settings']['source_mode'] or
                (raw.get('source_mode') == 'image' and
                 raw.get('verified_image_id') != stage['run_settings']['image']) or
                raw.get('command') != reproduction['command'] or
                raw.get('expected_exit') != reproduction['exit_code'] or
                raw.get('expected_marker') != reproduction['marker'] or
                raw.get('cleanup_confirmed') is not True or raw.get('timed_out') is not False or
                raw.get('exit_code') != raw.get('expected_exit') or
                not raw.get('expected_marker') or raw['expected_marker'] not in raw.get('stdout', '')):
            raise PartialRecoveryError('baseline report did not verify the reproduction')
        bound_reports.append({'path': str(report), 'sha256': report_hash})
    review_path = task.parent / 'baseline-review-model.json'
    review_hash = _file(review_path)
    review = json.loads(review_path.read_text(encoding='utf-8'))
    if (review.get('exit_code') != 0 or review.get('timed_out') or
            autopilot._result(review.get('stdout', '')).get('verdict') != 'PASS'):
        raise PartialRecoveryError('independent baseline semantic PASS is required')
    if provider_report != task.parent / 'implementation-model.json':
        raise PartialRecoveryError('original implementation model result is required')
    provider_hash = _file(provider_report)
    provider = json.loads(provider_report.read_text(encoding='utf-8'))
    signal = classify_provider_limit(provider)
    if not signal:
        raise PartialRecoveryError('the interrupted implementation is not a provider limit')
    stable_task = {key: data.get(key) for key in ('id', 'kind', 'surface', 'source', 'repo',
                   'base_commit', 'implementer', 'criteria', 'criterion_changes', 'evidence',
                   'iterations', 'limits', 'started_at', 'required_ci_workflows', 'ci_skip_policy')}
    return {'task_contract_sha256': _hash(stable_task), 'state_sha256': _hash(state),
            'issue_sha256': issue_digest, 'input_base_sha256': cli.digest(base_file),
            'input_stage_sha256': _file(stage_path), 'settings': stage['run_settings'],
            'workspace': str(workspace), 'base': base, 'branch': branch, 'common_git_dir': str(common),
            'inventory': _inventory(workspace), 'reports': bound_reports,
            'semantic_review_sha256': review_hash, 'provider_report_sha256': provider_hash,
            'provider_signal': signal}


def register(task_path, provider_report):
    task = Path(task_path).expanduser().resolve(strict=True)
    with task_lock(task):
        data = cli.read(task)
        if data['status'] != 'paused' or cli.enforce_budget(data):
            raise PartialRecoveryError('a paused task within its original limits is required')
        checkpoint = task.parent / 'partial-implementation-checkpoint.json'
        if checkpoint.exists() or checkpoint.is_symlink():
            raise PartialRecoveryError('checkpoint already exists; inspect retained attempt')
        state = cli.read(task.parent / 'autopilot.json')
        report = Path(provider_report).expanduser().resolve(strict=True)
        contract = _contract(task, data, state, report)
        value = {'schema': 1, 'status': 'ready', 'registered_at': cli.now(),
                 'resume_attempts': 0, 'contract': contract}
        cli.write(checkpoint, value)
        return value


def claim(task, data, state, settings):
    checkpoint = task.parent / 'partial-implementation-checkpoint.json'
    if checkpoint.is_symlink() or not checkpoint.is_file():
        raise PartialRecoveryError('interrupted implementation requires an explicit checkpoint')
    value = cli.read(checkpoint)
    if value.get('status') != 'ready' or value.get('resume_attempts') != 0:
        raise PartialRecoveryError('checkpoint already used or interrupted; inspect retained state')
    contract = _contract(task, data, state, task.parent / 'implementation-model.json')
    if contract != value.get('contract') or contract['settings'] != settings:
        raise PartialRecoveryError('partial checkpoint or original contract changed')
    value.update(status='attempted', attempted_at=cli.now(), resume_attempts=value['resume_attempts'] + 1)
    cli.write(checkpoint, value)
    return value


def validate_after_model(task, data, state, settings, checkpoint):
    """Allow model edits, but not changes to the bound task and baseline evidence."""
    current = _contract(task, data, state, task.parent / 'implementation-model.json')
    previous = checkpoint['contract']
    if (current['settings'] != settings or
            {k: v for k, v in current.items() if k != 'inventory'} !=
            {k: v for k, v in previous.items() if k != 'inventory'}):
        raise PartialRecoveryError('partial task contract changed during model continuation')


def clean_reverify_binding(task, data, state, issue_path, settings):
    """Bind retained current state; this cannot prove unavailable historical digests."""
    workspace, branch, common = _workspace_identity(data)
    _safe_index_flags(workspace)
    stage_path = task.parent / 'input-stage.json'
    issue = task.parent / 'issue.txt'
    base = task.parent / 'input-base.txt'
    if issue_path != issue or _file(issue) != state.get('issue_digest'):
        raise PartialRecoveryError('clean repair request binding changed')
    stage = cli.read(stage_path)
    if (stage.get('phase') != 'ready' or stage.get('request_digest') != cli.digest(issue)
            or stage.get('base_digest') != _file(base) or stage.get('run_settings') != settings
            or stage.get('input_digest') != autopilot._input_digest(task)
            or state.get('input_digest') != autopilot._input_digest(task)):
        raise PartialRecoveryError('clean repair input or execution settings changed')
    evidence = data.get('evidence')
    if not isinstance(evidence, list) or not evidence or any(not isinstance(item, dict) for item in evidence):
        raise PartialRecoveryError('clean repair requires retained evidence')
    indexed = {item.get('id'): item for item in evidence}
    if len(indexed) != len(evidence) or None in indexed:
        raise PartialRecoveryError('clean repair evidence identities are ambiguous')
    for item in evidence:
        path = Path(item['path'])
        if item.get('criteria_digest') != cli.criteria_digest(data) or _file(path) != item.get('sha256'):
            raise PartialRecoveryError('clean repair retained evidence or criteria changed')
    existing = state.get('clean_reverify_binding')
    retained = existing.get('retained_evidence') if isinstance(existing, dict) else evidence
    if (not isinstance(retained, list) or not retained or
            any(not isinstance(item, dict) or indexed.get(item.get('id')) != item for item in retained)):
        raise PartialRecoveryError('clean repair original evidence was removed or changed')
    for name in ('reproduction', 'resolution', 'tests'):
        if name != 'reproduction' or data['kind'] == 'bug':
            autopilot._check_spec(state['plan'], name)
    return {'plan_sha256': _hash(state['plan']), 'input_stage_sha256': _file(stage_path),
            'criteria_sha256': cli.criteria_digest(data), 'criterion_changes_sha256': _hash(data['criterion_changes']),
            'limits_sha256': _hash(data['limits']), 'base_commit': data['base_commit'],
            'candidate_commit': state['candidate_commit'], 'issue_sha256': cli.digest(issue),
            'settings_sha256': _hash(settings), 'workspace': str(workspace),
            'branch': branch, 'common_git_dir': str(common), 'retained_evidence': retained}
