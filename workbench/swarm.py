"""Run frozen coverage and comparison lanes in independent offline sandboxes."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from runner.sandbox import SandboxError, run_sandbox
from workbench import cli, stack_rebase, workflow


SOURCE = 'skills/swarm/SKILL.md'
SHA = re.compile(r'[0-9a-f]{40}')
IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
IDENT = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}')
SCORE = re.compile(r'IBOARD_SCORE=(0|[1-9][0-9]?|100)')
RULES = ('first-pass', 'rank-all', 'best-of')
ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, cli.WorkbenchError,
          stack_rebase.RebaseError, SandboxError, subprocess.SubprocessError)


class SwarmError(ValueError):
    pass


class CoverageFailed(SwarmError):
    pass


def _read(path, digest=None, limit=256_000):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise SwarmError('expected a bounded regular evidence file')
    with path.open('rb') as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit or (digest is not None and hashlib.sha256(raw).hexdigest() != digest):
        raise SwarmError('plan or evidence digest changed')
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('schema') != cli.SCHEMA:
        raise SwarmError('invalid swarm record')
    return data


def _bounded(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and '\0' not in value


def _number(value, low, high):
    return type(value) is int and low <= value <= high


def _driver(command):
    if (not isinstance(command, list) or not 1 <= len(command) <= 40
            or any(not _bounded(arg, 4096) for arg in command)
            or not workflow._workspace_command(command)):
        raise SwarmError('lane needs a tracked workspace entrypoint and bounded arguments')
    entry = (command[0] if command[0].startswith('/workspace/') else command[1]).removeprefix('/workspace/')
    if (not entry or '\\' in entry or not entry.isprintable()
            or any(part in ('', '.', '..', '.git') for part in entry.split('/'))):
        raise SwarmError('invalid swarm entrypoint path')
    return entry


def _manifest(data):
    if set(data) != {'schema', 'predicate', 'shape', 'race_rule', 'parallelism', 'seconds', 'memory_mb',
                     'image', 'lanes'}:
        raise SwarmError('unknown or missing swarm manifest fields')
    shape, rule, lanes = data['shape'], data['race_rule'], data['lanes']
    if (shape not in ('coverage', 'race', 'mixed') or not _bounded(data['predicate'], 1000)
            or (rule is not None if shape == 'coverage' else rule not in RULES)
            or not _number(data['parallelism'], 1, 4) or not _number(data['seconds'], 1, 3600)
            or not _number(data['memory_mb'], 128, 8192) or not isinstance(data['image'], str)
            or not IMAGE.fullmatch(data['image'])
            or not isinstance(lanes, list) or not 1 <= len(lanes) <= 24):
        raise SwarmError('invalid swarm shape, rule, image or resource bounds')
    seen = set()
    for lane in lanes:
        if (not isinstance(lane, dict) or set(lane) != {'id', 'role', 'revision', 'command', 'marker', 'exit_code'}
                or not isinstance(lane['id'], str) or not IDENT.fullmatch(lane['id']) or lane['id'].casefold() in seen
                or lane['role'] not in ('coverage', 'race') or not isinstance(lane['revision'], str)
                or not SHA.fullmatch(lane['revision'])
                or not _bounded(lane['marker'], 200) or not lane['marker'].isprintable()
                or not _number(lane['exit_code'], 0, 255)):
            raise SwarmError('lane needs a distinct ID, exact revision, role and observable outcome')
        _driver(lane['command'])
        seen.add(lane['id'].casefold())
    roles = {lane['role'] for lane in lanes}
    if roles != ({shape} if shape != 'mixed' else {'coverage', 'race'}):
        raise SwarmError('lane roles do not match the declared shape')
    arms = [lane for lane in lanes if lane['role'] == 'race']
    if arms and (len(arms) < 2 or any(
            tuple(arm[field] for field in ('command', 'marker', 'exit_code')) !=
            tuple(arms[0][field] for field in ('command', 'marker', 'exit_code')) for arm in arms)):
        raise SwarmError('a race needs at least two arms with the same frozen measurement method')


def _git(repo, *args, timeout=15):
    raw = stack_rebase._git(repo, *args, seconds=timeout)
    return stack_rebase._output(raw).decode('utf-8').strip()


def _entry(repo, lane):
    revision = lane['revision']
    path = _driver(lane['command'])
    if _git(repo, 'rev-parse', '--verify', revision + '^{commit}') != revision:
        raise SwarmError('swarm lane did not name an exact commit')
    row = _git(repo, '--literal-pathspecs', 'ls-tree', revision, '--', path).split()
    if (len(row) < 4 or row[0] not in ('100644', '100755') or row[1] != 'blob'
            or not SHA.fullmatch(row[2])):
        raise SwarmError('the driver must be a regular tracked file at the exact revision')
    return row[2]


def freeze(manifest, repo, out):
    source = Path(manifest).expanduser()
    data = _read(source)
    _manifest(data)
    repo = Path(repo).expanduser().resolve(strict=True)
    output = Path(out).expanduser().resolve()
    if output.is_relative_to(repo):
        raise SwarmError('keep swarm evidence outside the product checkout')
    drivers = {lane['id']: _entry(repo, lane) for lane in data['lanes']}
    if len({drivers[lane['id']] for lane in data['lanes'] if lane['role'] == 'race'}) > 1:
        raise SwarmError('race arms must use identical driver bytes')
    record = {**data, 'source': SOURCE, 'repo': str(repo), 'drivers': drivers}
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = output / 'plan.json'
    if path.exists() or path.is_symlink():
        if _read(path) != record:
            raise SwarmError('swarm evidence directory already contains a different approval')
    else:
        cli.write(path, record)
    return {'plan': str(path), 'sha256': cli.digest(path)}


def _load(plan, sha256):
    path = Path(plan).expanduser()
    if not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256):
        raise SwarmError('use the approved plan SHA-256')
    data = _read(path, sha256)
    if data['source'] != SOURCE or set(data['drivers']) != {lane['id'] for lane in data['lanes']}:
        raise SwarmError('invalid frozen swarm approval')
    _manifest({key: value for key, value in data.items() if key not in ('source', 'repo', 'drivers')})
    if (any(not isinstance(blob, str) or not SHA.fullmatch(blob) for blob in data['drivers'].values())
            or len({data['drivers'][lane['id']] for lane in data['lanes'] if lane['role'] == 'race'}) > 1):
        raise SwarmError('invalid frozen driver identities')
    return Path(data['repo']).resolve(strict=True), data


def _classify(run, lane, data):
    raw = run.get('execution') or {}
    if (not isinstance(raw, dict) or run.get('before') != lane['revision'] or run.get('after') != lane['revision']
            or run.get('driver') != data['drivers'][lane['id']]
            or run.get('worktree_clean') is not True or run.get('worktree_removed') is not True
            or raw.get('workspace') != run.get('worktree') or raw.get('image') != data['image']
            or raw.get('memory_mb') != data['memory_mb'] or raw.get('command') != lane['command']
            or raw.get('workspace_read_only') is not True or raw.get('container_created') is not True
            or raw.get('cleanup_confirmed') is not True or raw.get('error') or raw.get('timed_out')
            or raw.get('stdout_truncated') or raw.get('stderr_truncated')
            or not isinstance(raw.get('stdout'), str) or type(raw.get('exit_code')) is not int):
        return 'BLOCKED', None
    output = raw['stdout'].splitlines()
    if raw['exit_code'] != lane['exit_code'] or lane['marker'] not in output:
        return 'ISSUES', None
    if lane['role'] == 'race' and data['race_rule'] in ('rank-all', 'best-of'):
        records = [SCORE.fullmatch(line) for line in output if line.startswith('IBOARD_SCORE=')]
        if len(records) != 1 or records[0] is None:
            return 'BLOCKED', None
        return 'PASS', int(records[0][1])
    return 'PASS', None


def _retryable(record, status):
    execution = record.get('execution')
    return (status == 'BLOCKED' and record['worktree_removed'] and
            (execution is None or isinstance(execution, dict) and execution.get('cleanup_confirmed') is True))


def _attempt(repo, data, lane, directory, attempt, sha256, remaining, sandbox, admit):
    ident = lane['id']
    worktree = directory / f'worktree-{attempt}'
    record = {'schema': cli.SCHEMA, 'plan_sha256': sha256, 'id': ident, 'attempt': attempt,
              'revision': lane['revision'], 'method': lane['command'], 'worktree': str(worktree),
              'worktree_removed': False}
    try:
        admit()
        if remaining() < 1:
            raise SwarmError('swarm deadline reached before worker admission')
        record['driver'] = _entry(repo, lane)
        if record['driver'] != data['drivers'][ident]:
            raise SwarmError('driver changed after approval')
        _git(repo, 'worktree', 'add', '--detach', str(worktree), lane['revision'], timeout=min(30, remaining()))
        record['before'] = _git(worktree, 'rev-parse', '--verify', 'HEAD')
        if record['before'] != lane['revision'] or _git(worktree, 'status', '--porcelain', '--untracked-files=all'):
            raise SwarmError('worker did not start on the exact clean revision')
        admit()
        record['execution'] = sandbox(worktree, data['image'], lane['command'], remaining(),
                                      memory_mb=data['memory_mb'], workspace_read_only=True)
        record['after'] = _git(worktree, 'rev-parse', '--verify', 'HEAD')
        record['worktree_clean'] = not _git(worktree, 'status', '--porcelain', '--untracked-files=all')
    except ERRORS as error:
        record['error'] = str(error)[:1200]
    finally:
        try:
            if worktree.exists():
                _git(repo, 'worktree', 'remove', '--force', str(worktree), timeout=20)
            record['worktree_removed'] = not worktree.exists()
        except ERRORS as error:
            record['cleanup_error'] = str(error)[:1200]
    path = directory / f'attempt-{attempt}.json'
    record['settled_ns'] = time.monotonic_ns()
    cli.write(path, record)
    status, score = _classify(record, lane, data)
    return {'path': str(path), 'sha256': cli.digest(path)}, status, score, _retryable(record, status), record['settled_ns']


def _lane(repo, data, lane, out, sha256, remaining, sandbox, admit):
    directory = out / lane['id']
    directory.mkdir(mode=0o700)
    receipts = []
    for attempt in (1, 2):
        ref, status, score, retry, settled = _attempt(repo, data, lane, directory, attempt, sha256, remaining,
                                                      sandbox, admit)
        receipts.append(ref)
        if not retry or remaining() < 1:
            break
    return {'id': lane['id'], 'revision': lane['revision'], 'role': lane['role'],
            'status': status, 'score': score, 'attempts': receipts, 'settled_ns': settled}


def _completion(lanes):
    evidenced = sorted((row for row in lanes if row['settled_ns'] is not None),
                       key=lambda row: (row['settled_ns'], row['id']))
    return [row['id'] for row in evidenced] + [row['id'] for row in lanes if row['settled_ns'] is None]


def _aggregate(data, lanes, completion):
    coverage = [row for row in lanes if row['role'] == 'coverage']
    arms = [row for row in lanes if row['role'] == 'race']
    passed = [row for row in arms if row['status'] == 'PASS']
    missing = [row['id'] for row in lanes if row['status'] == 'BLOCKED']
    issues = [row['id'] for row in lanes if row['status'] == 'ISSUES']
    rule = data['race_rule']
    winner = None
    ranking = []
    ties = []
    race_status = 'PASS'
    if arms:
        if rule == 'first-pass':
            names = {row['id'] for row in passed}
            winner = next((name for name in completion if name in names), None)
        elif passed:
            ranking = [row['id'] for row in sorted(passed, key=lambda row: (-row['score'], row['id']))]
            best = max(row['score'] for row in passed)
            ties = sorted(row['id'] for row in passed if row['score'] == best)
            if len(ties) == 1 and (rule != 'rank-all' or not any(row['status'] == 'BLOCKED' for row in arms)):
                winner = ties.pop()
        if winner is None:
            race_status = 'BLOCKED' if ties or any(row['status'] == 'BLOCKED' for row in arms) else 'ISSUES'
    statuses = {row['status'] for row in coverage} | {race_status}
    status = 'ISSUES' if 'ISSUES' in statuses else 'BLOCKED' if 'BLOCKED' in statuses else 'PASS'
    return {'status': status, 'winner': winner, 'ranking': ranking, 'ties': ties,
            'gaps': missing, 'issues': issues}


def run(plan, sha256, out, *, sandbox=run_sandbox, seconds_limit=None, admit=lambda: None):
    repo, data = _load(plan, sha256)
    output = Path(out).expanduser().resolve()
    if output.is_relative_to(repo):
        raise SwarmError('keep swarm evidence outside the product checkout')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    deadline = time.monotonic() + min(data['seconds'], seconds_limit if seconds_limit is not None else data['seconds'])

    def remaining():
        return max(0.0, deadline - time.monotonic())

    rows = {}
    with ThreadPoolExecutor(max_workers=data['parallelism']) as pool:
        futures = {pool.submit(_lane, repo, data, lane, output, sha256, remaining, sandbox, admit): lane
                   for lane in data['lanes']}
        for future in as_completed(futures):
            lane = futures[future]
            try:
                row = future.result()
            except ERRORS as error:
                row = {'id': lane['id'], 'revision': lane['revision'], 'role': lane['role'],
                       'status': 'BLOCKED', 'score': None, 'attempts': [], 'settled_ns': None,
                       'error': str(error)[:1200]}
            rows[lane['id']] = row
    lanes = [rows[lane['id']] for lane in data['lanes']]
    completion = _completion(lanes)
    report = {'schema': cli.SCHEMA, 'source': SOURCE, 'plan_sha256': sha256, 'predicate': data['predicate'],
              'shape': data['shape'], 'race_rule': data['race_rule'], 'completion_order': completion,
              'lanes': lanes, **_aggregate(data, lanes, completion)}
    path = output / 'result.json'
    cli.write(path, report)
    return {**report, 'report': str(path), 'sha256': cli.digest(path)}


def audit(plan, sha256, report, report_sha256):
    repo, data = _load(plan, sha256)
    report = Path(report).expanduser()
    if not isinstance(report_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', report_sha256):
        raise SwarmError('approve the retained report SHA-256')
    result = _read(report, report_sha256)
    if (result['source'] != SOURCE or result['plan_sha256'] != sha256
            or any(result[key] != data[key] for key in ('shape', 'predicate', 'race_rule'))
            or len(result['lanes']) != len(data['lanes'])):
        raise SwarmError('retained report does not match its approved swarm')
    for row, lane in zip(result['lanes'], data['lanes']):
        if (any(row[key] != lane[key] for key in ('id', 'role', 'revision')) or not 0 <= len(row['attempts']) <= 2
                or _entry(repo, lane) != data['drivers'][lane['id']]):
            raise SwarmError('retained lane identity or attempt count changed')
        directory = report.resolve().parent / lane['id']
        if sorted(path.name for path in directory.glob('attempt-*.json')) != [
                f'attempt-{index}.json' for index in range(1, len(row['attempts']) + 1)]:
            raise SwarmError('the result omitted or added a worker attempt')
        status, score = 'BLOCKED', None
        settled = None
        for index, reference in enumerate(row['attempts'], 1):
            path = report.resolve().parent / lane['id'] / f'attempt-{index}.json'
            if str(path) != reference['path'] or path.resolve() != path:
                raise SwarmError('swarm receipt path is outside its retained lane')
            raw = _read(path, reference['sha256'], 500_000)
            if (raw['id'] != lane['id'] or raw['revision'] != lane['revision'] or raw['method'] != lane['command']
                    or raw['attempt'] != index or raw['plan_sha256'] != sha256
                    or raw['worktree'] != str(path.parent / f'worktree-{index}')
                    or type(raw.get('settled_ns')) is not int or raw['settled_ns'] < 0
                    or (settled is not None and raw['settled_ns'] <= settled)):
                raise SwarmError('worker receipt does not describe the approved commit and method')
            settled = raw['settled_ns']
            status, score = _classify(raw, lane, data)
            if index < len(row['attempts']) and not _retryable(raw, status):
                raise SwarmError('a settled result or an unconfirmed cleanup must not be retried')
        if (status, score, settled) != (row['status'], row['score'], row['settled_ns']):
            raise SwarmError('lane verdict disagrees with its execution evidence')
    if result['completion_order'] != _completion(result['lanes']):
        raise SwarmError('completion order disagrees with the retained worker receipts')
    verdict = _aggregate(data, result['lanes'], result['completion_order'])
    if any(result[key] != value for key, value in verdict.items()):
        raise SwarmError('combined verdict disagrees with the lane evidence')
    return {'status': verdict['status'], 'report': str(report.resolve()), 'verified': verdict}


def task_manifest(path, digest):
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise SwarmError('supply the approved task swarm manifest digest')
    data = _read(Path(path), digest)
    if (data.get('shape') != 'coverage' or not isinstance(data.get('lanes'), list)
            or any(lane['revision'] not in ('baseline', 'candidate') for lane in data['lanes'])
            or not any(lane['revision'] == 'candidate' for lane in data['lanes'])):
        raise SwarmError('a task swarm needs required coverage and at least one candidate lane')
    _manifest({**data, 'lanes': [{**lane, 'revision': '0' * 40} for lane in data['lanes']]})
    return data


def intake(data, stage):
    reference = data.get('coverage_swarm')
    if reference != stage.get('coverage_swarm'):
        raise SwarmError('coverage swarm differs from the approved task intake')
    if reference is not None:
        task_manifest(reference['path'], reference['sha256'])
    return reference


def _task_plan(data, head):
    ref = data['coverage_swarm']
    manifest = task_manifest(ref['path'], ref['sha256'])
    revisions = {'baseline': data['base_commit'], 'candidate': head}
    return {**manifest, 'lanes': [{**lane, 'revision': revisions[lane['revision']]} for lane in manifest['lanes']]}


def verify_task(data, head, out, *, seconds, admit=lambda: None, sandbox=run_sandbox):
    output = Path(out).expanduser().resolve()
    if output.is_relative_to(Path(data['repo']).resolve()):
        raise SwarmError('keep task swarm evidence outside the product checkout')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    path = output / 'manifest.json'
    cli.write(path, _task_plan(data, head))
    approval = freeze(path, data['repo'], output / 'approval')
    result = run(**approval, out=output / 'run', sandbox=sandbox, seconds_limit=seconds, admit=admit)
    if result['status'] != 'PASS':
        error = CoverageFailed if result['status'] == 'ISSUES' else SwarmError
        raise error(f'coverage swarm {result["status"].lower()}; see {result["report"]}')
    return {'plan': approval['plan'], 'plan_sha256': approval['sha256'],
            'report': result['report'], 'report_sha256': result['sha256']}


def require_task(data, head, reference):
    if data.get('coverage_swarm') is None:
        if reference is not None:
            raise SwarmError('unexpected coverage swarm evidence')
        return
    if not isinstance(reference, dict) or set(reference) != {'plan', 'plan_sha256', 'report', 'report_sha256'}:
        raise SwarmError('independent coverage swarm evidence is missing')
    repo, frozen = _load(reference['plan'], reference['plan_sha256'])
    manifest = {key: val for key, val in frozen.items() if key not in ('source', 'repo', 'drivers')}
    if repo != Path(data['repo']).resolve() or manifest != _task_plan(data, head):
        raise SwarmError('coverage swarm did not exercise the original required paths and exact revisions')
    result = audit(reference['plan'], reference['plan_sha256'], reference['report'], reference['report_sha256'])
    if result['status'] != 'PASS':
        raise SwarmError('the required coverage swarm did not pass')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    options = {'freeze': ('manifest', 'repo', 'out'), 'run': ('plan', 'sha256', 'out'),
               'audit': ('plan', 'sha256', 'report', 'report-sha256')}
    for action, fields in options.items():
        command = actions.add_parser(action)
        for field in fields:
            command.add_argument('--' + field, required=True)
    args = vars(parser.parse_args(argv))
    action = args.pop('action')
    try:
        result = {'freeze': freeze, 'run': run, 'audit': audit}[action](**args)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result.get('status', 'PASS') != 'PASS')


if __name__ == '__main__':
    sys.exit(main())
