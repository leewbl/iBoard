"""Coordinate a fixed queue of independently supervised iBoard tasks."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import time

from workbench import autonomous_run, cli, run_control
from workbench.task_lock import TaskBusy, task_lock


class QueueError(ValueError):
    pass


QUEUE_LOCK = '.autopilot-full.lock'
SOURCE = run_control.GROUP_SOURCE
ERRORS = (*autonomous_run.ERRORS, AttributeError)
SHA = re.compile(r'[0-9a-f]{64}')


def _read_pinned(path, expected):
    if not path.is_file() or path.stat().st_size > 256_000:
        raise QueueError('expected a bounded regular manifest')
    raw = path.read_bytes()
    if not isinstance(expected, str) or hashlib.sha256(raw).hexdigest() != expected:
        raise QueueError('approved manifest did not match its SHA-256')
    return json.loads(raw)


def _load(plan, sha256):
    path = Path(plan).expanduser().resolve(strict=True)
    queue = _read_pinned(path, sha256)
    if (queue.get('schema') != cli.SCHEMA or queue.get('source') != SOURCE
            or cli.read(path.with_name('ready.json')).get('sha256') != sha256):
        raise QueueError('queue enrollment is incomplete or changed')
    for row in queue['members']:
        if run_control.group(row['task']) != path:
            raise QueueError('the task is no longer enrolled in this queue')
    return path, queue


def freeze(manifest, out):
    """Approve only the independently frozen plans named by the operator."""
    manifest = Path(manifest).expanduser().resolve(strict=True)
    if not manifest.is_file() or manifest.stat().st_size > 256_000:
        raise QueueError('expected a bounded regular manifest')
    data = cli.read(manifest)
    members, count, interval = data['members'], data['parallelism'], data['interval']
    if (set(data) != {'schema', 'members', 'parallelism', 'interval'}
            or not isinstance(members, list) or not 1 <= len(members) <= 24
            or type(count) is not int or not 1 <= count <= 4
            or type(interval) is not int or not 1 <= interval <= 3600):
        raise QueueError('require one to 24 explicit plans, parallelism 1–4, and an interval 1–3600 seconds')
    root = Path(out).expanduser().resolve()
    plans, tasks, workspaces = [], set(), set()
    branches = set()
    for entry in members:
        if set(entry) != {'plan', 'sha256'}:
            raise QueueError('each queue member needs one plan and its SHA-256')
        path = (manifest.parent / Path(entry['plan']).expanduser()).resolve(strict=True)
        plan = _read_pinned(path, entry['sha256'])
        if plan.get('source') != autonomous_run.SOURCE or plan.get('predicate') not in autonomous_run.OUTCOMES:
            raise QueueError('each member must be an approved autonomous task')
        task = autonomous_run._task(plan['task'])
        binding = plan['binding']
        workspace = Path(binding['repo']).resolve(strict=True)
        repo = binding['settings']['pr_repo']
        branch = (repo.casefold(), binding['id'].casefold()) if repo else None
        if (task in tasks or any(workspace.is_relative_to(old) or old.is_relative_to(workspace)
                                for old in workspaces) or (branch is not None and branch in branches)):
            raise QueueError('queue members need separate task records, worktrees, and destination branches')
        if root.is_relative_to(workspace) or root.is_relative_to(task.parent):
            raise QueueError('keep queue evidence outside product worktrees and member task directories')
        tasks.add(task)
        workspaces.add(workspace)
        if branch is not None:
            branches.add(branch)
        plans.append({'task': str(task), 'id': binding['id'], 'plan': str(path), 'sha256': entry['sha256'],
                      'predicate': plan['predicate'], 'destination': repo, 'base': binding['base_commit']})

    queue = {'schema': cli.SCHEMA, 'source': SOURCE, 'parallelism': count, 'interval': interval, 'members': plans}
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = root / '.enrollment'
    if marker.is_symlink():
        raise QueueError('queue enrollment lock is unsafe')
    marker.touch(exist_ok=True)
    path = root / 'group.json'
    with task_lock(marker, lock_name='.enrollment.lock', reentrant=False), ExitStack() as locks:
        if path.exists():
            if cli.read(path) != queue:
                raise QueueError('queue directory already contains a different approval')
            sha = cli.digest(path)
            if path.with_name('ready.json').exists():
                _load(path, sha)
                return {'plan': str(path), 'sha256': sha, 'tasks': len(plans)}
        for task in sorted(tasks):
            locks.enter_context(task_lock(task, reentrant=False))
        for row in plans:
            task = Path(row['task'])
            plan = _read_pinned(Path(row['plan']), row['sha256'])
            record, _ = autonomous_run._assert_task(task, plan, {})
            if (record['status'] != 'paused' or cli.enforce_budget(record)
                    or autonomous_run._supported(record, row['destination']) != row['predicate']):
                raise QueueError('enrollment requires original paused tasks within their existing budget')
            link = task.with_name(run_control.GROUP_RECORD)
            if link.is_symlink() or (link.exists() and
                    (not path.exists() or cli.read(link) != {'schema': cli.SCHEMA, 'task': str(task),
                                                           'group': str(path), 'sha256': cli.digest(path)})):
                raise QueueError('a member already belongs to another queue')
        if not path.exists():
            cli.write(path, queue)
        sha = cli.digest(path)
        for task in sorted(tasks):
            cli.write(task.with_name(run_control.GROUP_RECORD),
                      {'schema': cli.SCHEMA, 'task': str(task), 'group': str(path), 'sha256': sha})
        cli.write(path.with_name('ready.json'), {'schema': cli.SCHEMA, 'sha256': sha})
    return {'plan': str(path), 'sha256': sha, 'tasks': len(plans)}


def hold(plan, sha256, reason):
    path, _ = _load(plan, sha256)
    return run_control.hold(path, reason)


def release(plan, sha256):
    path, queue = _load(plan, sha256)
    try:
        with ExitStack() as locks:
            for row in sorted(queue['members'], key=lambda member: member['task']):
                locks.enter_context(task_lock(row['task'], reentrant=False))
            return run_control.release(path, lock_name=QUEUE_LOCK)
    except TaskBusy as error:
        raise QueueError('queue members are still finishing admitted work') from error


def _snapshot(result):
    return {key: result[key] for key in ('status', 'attempts', 'proof', 'identity', 'reason',
                                        'request', 'journal', 'iterations', 'reverification') if key in result}


def _journal(path, sha256):
    result = cli.read(path) if path.exists() else {
        'schema': cli.SCHEMA, 'plan_sha256': sha256, 'members': {}, 'events': [],
    }
    if result.get('plan_sha256') != sha256:
        raise QueueError('the retained queue journal belongs to a different approval')
    return result


def show(plan, sha256):
    path, queue = _load(plan, sha256)
    journal_path = path.with_name('group-run.json')
    journal = _journal(journal_path, sha256)
    return {'schema': cli.SCHEMA, 'hold': run_control.read(path), 'journal': str(journal_path),
            'members': [{**row, **journal['members'].get(row['task'], {'status': 'not-yet-observed'})}
                        for row in queue['members']]}


def run(plan, sha256, *, once=False, reverify_stale_main=False, drive=autonomous_run.run, sleep=time.sleep,
        observe_base=None):
    path, queue = _load(plan, sha256)
    if reverify_stale_main and observe_base is None:
        from workbench import task_replay
        observe_base = task_replay.observe
    journal_path = path.with_name('group-run.json')
    with task_lock(path, lock_name=QUEUE_LOCK, reentrant=False):
        journal = _journal(journal_path, sha256)

        def save(task, result):
            current = _snapshot(result)
            if current != journal['members'].get(task):
                journal['members'][task] = current
                journal['events'].append({'at': cli.now(), 'task': task, **current})
                cli.write(journal_path, journal)

        def tick(row):
            try:
                control = run_control.read(path)
                if control and control['status'] == 'held':
                    prior = journal['members'].get(row['task'], {})
                    if prior.get('status') == 'complete':
                        return prior
                    return {'status': 'held', 'request': control['request']}
                if cli.digest(path) != sha256 or run_control.group(row['task']) != path:
                    raise QueueError('queue approval changed before the task took its slot')
                result = drive(row['plan'], row['sha256'], once=True)
                if reverify_stale_main and result['status'] == 'blocked':
                    replay = observe_base(path, sha256, row)
                    if replay:
                        result = {**result, 'reverification': replay}
                        if replay['status'] == 'held':
                            result['status'] = 'held'
                        elif replay['status'] == 'verified':
                            result['reason'] = 'candidate reverified on current main; original PR delivery is pending'
                return result
            except TaskBusy:
                return {'status': 'active'}
            except ERRORS as error:
                return {'status': 'blocked', 'reason': str(error)[:1400]}

        while True:
            with ThreadPoolExecutor(max_workers=queue['parallelism']) as pool:
                scheduled = {pool.submit(tick, row): row['task'] for row in queue['members']}
                for future in as_completed(scheduled):
                    save(scheduled[future], future.result())
            members = [{**row, **journal['members'][row['task']]} for row in queue['members']]
            states = {row['status'] for row in members}
            paused = run_control.read(path)
            if states == {'complete'}:
                status = 'complete'
            elif paused and paused['status'] == 'held':
                status = 'held'
            elif states <= {'complete', 'held', 'blocked'}:
                status = 'blocked' if 'blocked' in states else 'held'
            else:
                status = 'watching'
            result = {'schema': cli.SCHEMA, 'status': status, 'journal': str(journal_path), 'members': members}
            if once or status != 'watching':
                return result
            for _ in range(queue['interval']):
                sleep(1)
                control = run_control.read(path)
                if control and control['status'] == 'held':
                    break


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prepare = sub.add_parser('freeze')
    prepare.add_argument('--manifest', required=True)
    prepare.add_argument('--out', required=True)
    for action in ('run', 'show', 'hold', 'release'):
        command = sub.add_parser(action)
        command.add_argument('--plan', required=True)
        command.add_argument('--sha256', required=True)
        if action == 'run':
            command.add_argument('--once', action='store_true')
            command.add_argument('--reverify-stale-main', action='store_true')
        if action == 'hold':
            command.add_argument('--reason', required=True)
    args = vars(parser.parse_args(argv))
    action = args.pop('action')
    try:
        result = {'freeze': freeze, 'run': run, 'show': show, 'hold': hold, 'release': release}[action](**args)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result.get('status') == 'blocked')


if __name__ == '__main__':
    raise SystemExit(main())
