#!/usr/bin/env python3
"""Local task record and evidence completeness gate; not a semantic verifier."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from workbench.task_lock import task_lock
else:
    from task_lock import task_lock

SCHEMA = 1
KINDS = ('reproduction', 'resolution', 'feature-doc', 'test-result', 'screenshot', 'runtime-log', 'independent-review')
CI_SKIP_POLICIES = ('openclaw-selected-v1',)

class WorkbenchError(Exception):
    pass

def now():
    return datetime.now(timezone.utc).isoformat()

def git(repo, *args, timeout=None):
    try:
        p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise WorkbenchError('Git operation exceeded the task time limit') from error
    if p.returncode:
        raise WorkbenchError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()

def sha(repo, ref, *, timeout=None):
    return git(repo, 'rev-parse', '--verify', f'{ref}^{{commit}}', timeout=timeout)

def nonempty(value, name):
    if not value or not value.strip():
        raise WorkbenchError(f'{name} must not be empty')
    return value.strip()

def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.task-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def read(path):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise WorkbenchError(f'cannot read task record: {e}') from e
    if data.get('schema') != SCHEMA:
        raise WorkbenchError('unsupported task record schema')
    return data

def digest(path):
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for block in iter(lambda: f.read(1024 * 1024), b''):
                h.update(block)
    except OSError as e:
        raise WorkbenchError(f'cannot read evidence {path}: {e}') from e
    return h.hexdigest()

def criteria_digest(data):
    # Binding reviews to the exact set and revision prevents stale approvals.
    text = json.dumps(data['criteria'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(text.encode()).hexdigest()

def enforce_budget(data):
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(data['started_at'])).total_seconds()
    failures = []
    if data['limits']['seconds'] is not None and elapsed >= data['limits']['seconds']:
        failures.append('time limit reached')
    if data['limits']['iterations'] is not None and data['iterations'] > data['limits']['iterations']:
        failures.append('iteration limit exceeded')
    return failures

def iteration_limit_reached(data):
    limit = data['limits']['iterations']
    return limit is not None and data['iterations'] >= limit

def find_criterion(data, cid):
    for c in data['criteria']:
        if c['id'] == cid:
            return c
    raise WorkbenchError(f'unknown criterion: {cid}')

def init(a):
    path = Path(a.task).expanduser().resolve()
    if path.exists():
        raise WorkbenchError('task record already exists')
    repo = Path(a.repo).expanduser().resolve()
    if ((a.iterations is not None and (type(a.iterations) is not int or a.iterations < 1)) or
            (a.seconds is not None and (type(a.seconds) is not int or a.seconds < 1))):
        raise WorkbenchError('iteration and time limits must be positive')
    source = nonempty(a.source, 'source')
    started_at = getattr(a, 'started_at', None) or now()
    try:
        parsed_start = datetime.fromisoformat(started_at)
        if parsed_start.tzinfo is None or parsed_start > datetime.now(timezone.utc):
            raise ValueError('invalid start time')
    except (TypeError, ValueError) as error:
        raise WorkbenchError('task start time must be a valid past timestamp with timezone') from error
    remaining = None if a.seconds is None else a.seconds - (datetime.now(timezone.utc) - parsed_start).total_seconds()
    if remaining is not None and remaining <= 0:
        raise WorkbenchError('task time limit reached during initialization')
    head = sha(repo, 'HEAD', timeout=remaining)
    data = dict(schema=SCHEMA, id=nonempty(a.id, 'id'), kind=a.kind, source=source, original_source=source, repo=str(repo), base_commit=head,
                implementer=nonempty(a.implementer, 'implementer'), surface=a.surface, started_at=started_at, status='running',
                limits={'iterations':a.iterations, 'seconds':a.seconds, 'cost':'provider-limit'}, iterations=0,
                criteria=[], criterion_changes=[], evidence=[], events=[{'at':now(), 'type':'created'}])
    write(path, data)
    print(f'Created {path}; base {head}')

def criterion_add(a, data):
    cid = nonempty(a.id, 'criterion id')
    if not re.fullmatch(r'[A-Za-z0-9._-]+', cid):
        raise WorkbenchError('criterion id must contain only letters, numbers, dot, underscore or hyphen')
    if any(c['id'] == cid for c in data['criteria']):
        raise WorkbenchError('criterion id already exists')
    data['criteria'].append({'id':cid, 'text':nonempty(a.text, 'criterion text'), 'revision':1})
    data['criterion_changes'].append({'at':now(), 'id':cid, 'action':'add', 'text':a.text, 'reason':nonempty(a.reason, 'reason')})

def criterion_change(a, data):
    c = find_criterion(data, a.id)
    old = c['text']
    new = nonempty(a.text, 'criterion text')
    if old == new:
        raise WorkbenchError('criterion has not changed')
    reason = nonempty(a.reason, 'reason')
    evidence_path = Path(a.basis).expanduser().resolve(strict=True)
    basis_hash = digest(evidence_path)
    data['criterion_changes'].append({'at':now(), 'id':a.id, 'action':'change', 'old':old, 'new':new, 'reason':reason,
                                      'basis':str(evidence_path), 'basis_sha256':basis_hash})
    c['text'] = new
    c['revision'] += 1


def record_ci_skip_policy(a, data):
    """Record an explicit, evidence-backed policy for an existing paused task."""
    if data.get('status') != 'paused':
        raise WorkbenchError('only paused tasks can change the CI skip policy')
    if a.policy not in CI_SKIP_POLICIES or data.get('ci_skip_policy'):
        raise WorkbenchError('unknown or already configured CI skip policy')
    required = data.get('required_ci_workflows')
    supplied = getattr(a, 'required_ci_workflows', None)
    if required is None and supplied == ['CI']:
        required = supplied
    elif supplied is not None and supplied != required:
        raise WorkbenchError('cannot replace existing required workflows')
    if not isinstance(required, list) or 'CI' not in required:
        raise WorkbenchError('OpenClaw skip policy requires CI as a required workflow')
    reason = nonempty(a.reason, 'reason')
    source = nonempty(a.source, 'source')
    basis = Path(a.basis).expanduser().resolve(strict=True)
    if not basis.is_file() or not basis.stat().st_size:
        raise WorkbenchError('policy evidence must be a nonempty regular file')
    if data.get('required_ci_workflows') is None:
        data['required_ci_workflows'] = required
    data['ci_skip_policy'] = a.policy
    data['events'].append({'at': now(), 'type': 'ci-skip-policy', 'mode': 'existing', 'policy': a.policy,
                           'source': source, 'reason': reason, 'basis': str(basis),
                           'basis_sha256': digest(basis), 'criteria_sha256': criteria_digest(data),
                           'base_commit': data['base_commit'], 'head': sha(data['repo'], 'HEAD'),
                           'required_ci_workflows': list(required)})

def evidence_add(a, data):
    path = Path(a.file).expanduser().resolve(strict=True)
    if not path.is_file():
        raise WorkbenchError('evidence must be a regular file')
    if path.stat().st_size == 0:
        raise WorkbenchError('evidence file is empty')
    actor = nonempty(a.actor, 'actor')
    commit = sha(data['repo'], a.commit)
    if a.kind == 'reproduction' and (commit != data['base_commit'] or sha(data['repo'], 'HEAD') != data['base_commit']):
        raise WorkbenchError('reproduction must be recorded while the base commit is checked out')
    if a.kind == 'independent-review' and actor == data['implementer']:
        raise WorkbenchError('independent review actor must differ from implementer')
    cids = list(dict.fromkeys(a.criterion or []))
    for cid in cids:
        find_criterion(data, cid)
    if a.kind == 'independent-review' and not cids:
        raise WorkbenchError('independent review must identify covered criteria')
    data['evidence'].append({'id':len(data['evidence']) + 1, 'kind':a.kind, 'path':str(path), 'sha256':digest(path),
                             'commit':commit, 'actor':actor, 'criteria':cids, 'criteria_digest':criteria_digest(data),
                             'description':nonempty(a.description, 'description'), 'at':now()})

def gate(data, expected):
    issues = []
    if data['status'] != 'running':
        issues.append(f"task is {data['status']}, not running")
    issues += enforce_budget(data)
    if not data['criteria']:
        issues.append('no acceptance criteria recorded')
    for change in data['criterion_changes']:
        if change['action'] == 'change':
            try:
                if digest(change['basis']) != change['basis_sha256']:
                    issues.append(f"criterion {change['id']} change basis was modified")
            except WorkbenchError:
                issues.append(f"criterion {change['id']} change basis missing or unreadable")
    repo = data['repo']
    head = sha(repo, 'HEAD')
    target = sha(repo, expected)
    if head != target:
        issues.append('expected commit differs from current HEAD')
    if git(repo, 'status', '--porcelain', '--untracked-files=all'):
        issues.append('working tree is not clean')
    valid = []
    for e in data['evidence']:
        if e.get('superseded'):
            continue
        try:
            if digest(e['path']) != e['sha256']:
                issues.append(f"evidence {e['id']} changed after recording")
                continue
            if e.get('execution_report') and digest(e['execution_report']) != e.get('execution_report_sha256'):
                issues.append(f"evidence {e['id']} execution report changed after recording")
                continue
        except WorkbenchError:
            issues.append(f"evidence {e['id']} missing or unreadable")
            continue
        if e['kind'] == 'reproduction':
            if e['commit'] == data['base_commit']:
                valid.append(e)
        elif e['commit'] == target:
            valid.append(e)
    kinds = {e['kind'] for e in valid}
    required = {'reproduction', 'resolution', 'test-result', 'independent-review'} if data['kind'] == 'bug' else {'feature-doc', 'test-result', 'independent-review'}
    required.add('screenshot' if data['surface'] == 'ui' else 'runtime-log')
    for missing in sorted(required - kinds):
        issues.append(f'missing valid {missing} evidence')
    covered = set()
    cd = criteria_digest(data)
    for e in valid:
        if e['kind'] == 'independent-review' and e['actor'] != data['implementer'] and e['criteria_digest'] == cd:
            covered.update(e['criteria'])
    for c in data['criteria']:
        if c['id'] not in covered:
            issues.append(f"criterion {c['id']} lacks current independent review")
    if data['kind'] == 'bug' and target == data['base_commit']:
        issues.append('bug fix target is identical to base commit')
    return head, issues

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sp = p.add_subparsers(dest='command', required=True)
    q = sp.add_parser('init'); q.add_argument('--task', required=True); q.add_argument('--id', required=True); q.add_argument('--repo', required=True); q.add_argument('--kind', choices=('bug','feature'), required=True); q.add_argument('--source', required=True); q.add_argument('--implementer', required=True); q.add_argument('--surface', choices=('ui','non-ui'), required=True); q.add_argument('--iterations', type=int, required=True); q.add_argument('--seconds', type=int, required=True)
    q = sp.add_parser('criterion-add'); q.add_argument('--task', required=True); q.add_argument('--id', required=True); q.add_argument('--text', required=True); q.add_argument('--reason', required=True)
    q = sp.add_parser('criterion-change'); q.add_argument('--task', required=True); q.add_argument('--id', required=True); q.add_argument('--text', required=True); q.add_argument('--reason', required=True); q.add_argument('--basis', required=True)
    q = sp.add_parser('evidence-add'); q.add_argument('--task', required=True); q.add_argument('--kind', choices=KINDS, required=True); q.add_argument('--file', required=True); q.add_argument('--commit', required=True); q.add_argument('--actor', required=True); q.add_argument('--description', required=True); q.add_argument('--criterion', action='append')
    q = sp.add_parser('iteration'); q.add_argument('--task', required=True)
    q = sp.add_parser('pause'); q.add_argument('--task', required=True); q.add_argument('--reason', required=True)
    q = sp.add_parser('extend-time', help='record an explicitly authorized increase to the total time limit')
    q.add_argument('--task', required=True); q.add_argument('--seconds', type=int, required=True); q.add_argument('--reason', required=True)
    q = sp.add_parser('remove-limits', help='record explicitly authorized removal of task time and iteration limits')
    q.add_argument('--task', required=True); q.add_argument('--reason', required=True)
    q = sp.add_parser('ci-skip-policy', help='record an evidence-backed CI skip policy on a paused task')
    q.add_argument('--task', required=True); q.add_argument('--policy', choices=CI_SKIP_POLICIES, required=True)
    q.add_argument('--reason', required=True); q.add_argument('--source', required=True); q.add_argument('--basis', required=True)
    q.add_argument('--required-ci-workflow', action='append', dest='required_ci_workflows')
    q = sp.add_parser('resume'); q.add_argument('--task', required=True)
    q = sp.add_parser('show'); q.add_argument('--task', required=True)
    q = sp.add_parser('gate'); q.add_argument('--task', required=True); q.add_argument('--commit', required=True)
    a = p.parse_args(argv)
    if a.command == 'init':
        init(a); return 0
    with task_lock(a.task):
        return _execute(a)


def _execute(a):
    path = Path(a.task).expanduser().resolve()
    data = read(path)
    if a.command == 'show':
        print(json.dumps(data, indent=2, ensure_ascii=False)); return 0
    if a.command == 'gate':
        head, issues = gate(data, a.commit)
        print(json.dumps({'passed':not issues, 'head':head, 'issues':issues, 'note':'Metadata/completeness check only; independent semantic verification and PR checks remain mandatory.'}, ensure_ascii=False, indent=2))
        return 1 if issues else 0
    if a.command == 'pause':
        data['status'] = 'paused'; data['events'].append({'at':now(), 'type':'pause', 'reason':nonempty(a.reason, 'reason')})
    elif a.command == 'ci-skip-policy':
        record_ci_skip_policy(a, data)
    elif a.command == 'remove-limits':
        if data['status'] != 'paused':
            raise WorkbenchError('only paused tasks can have limits removed')
        reason = nonempty(a.reason, 'reason')
        old = {key: data['limits'][key] for key in ('seconds', 'iterations')}
        data['limits']['seconds'] = None
        data['limits']['iterations'] = None
        data['events'].append({'at': now(), 'type': 'limits-removed', 'old_limits': old, 'reason': reason})
    elif a.command == 'extend-time':
        if data['status'] != 'paused':
            raise WorkbenchError('only paused tasks can have their time limit increased')
        old = data['limits']['seconds']
        if old is None or a.seconds <= old:
            raise WorkbenchError('new total time limit must exceed the previous limit')
        reason = nonempty(a.reason, 'reason')
        data['limits']['seconds'] = a.seconds
        data['events'].append({'at':now(), 'type':'time-limit-increase', 'old_seconds':old,
                               'new_seconds':a.seconds, 'reason':reason})
    elif a.command == 'resume':
        if data['status'] != 'paused':
            raise WorkbenchError('only paused tasks can resume')
        if enforce_budget(data):
            raise WorkbenchError('task budget exceeded; do not resume without an explicit budget change')
        data['status'] = 'running'; data['events'].append({'at':now(), 'type':'resume'})
    else:
        if data['status'] != 'running':
            raise WorkbenchError('task is not running')
        failures = enforce_budget(data)
        if failures:
            data['status'] = 'paused'; data['events'].append({'at':now(), 'type':'budget-stop', 'reason':'; '.join(failures)}); write(path,data)
            raise WorkbenchError('; '.join(failures))
        if a.command == 'iteration':
            if iteration_limit_reached(data):
                data['status'] = 'paused'; data['events'].append({'at':now(), 'type':'budget-stop', 'reason':'iteration limit reached'}); write(path,data)
                raise WorkbenchError('iteration limit reached')
            data['iterations'] += 1; data['events'].append({'at':now(), 'type':'iteration', 'number':data['iterations']})
        elif a.command == 'criterion-add': criterion_add(a,data)
        elif a.command == 'criterion-change': criterion_change(a,data)
        elif a.command == 'evidence-add': evidence_add(a,data)
    write(path,data)
    print(f'{a.command}: recorded')
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (WorkbenchError, OSError, ValueError) as e:
        print(f'workbench: {e}', file=sys.stderr)
        sys.exit(2)
