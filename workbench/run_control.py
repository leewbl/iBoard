"""Stop admission of new task steps and retain a cross-process hold."""

import argparse
from contextlib import contextmanager, nullcontext
import hashlib
import json
from pathlib import Path
import re
import uuid

from workbench import cli
from workbench.task_lock import task_lock, TaskBusy


class ControlError(ValueError):
    pass


class Held(ControlError):
    pass


CONTROL_LOCK = '.run-control.lock'
CONTROL_RECORD = 'operator-hold.json'
GROUP_RECORD = 'autopilot-group.json'
GROUP_SOURCE = 'skills/poteto-mode/playbooks/autopilot-full.md'


def _task(path):
    return Path(path).expanduser().resolve(strict=True)


def _read_own(task):
    task = _task(task)
    path = task.with_name(CONTROL_RECORD)
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size > 4096)):
        raise ControlError('operator hold record is unsafe')
    if not path.exists():
        return None
    try:
        state = cli.read(path)
    except (AttributeError, cli.WorkbenchError, OSError, ValueError) as error:
        raise ControlError('operator hold record could not be read') from error
    status = state.get('status')
    if (state.get('task') != str(task) or status not in ('held', 'released')
            or not isinstance(state.get('request'), str) or not re.fullmatch(r'[0-9a-f]{32}', state['request'])
            or any(not isinstance(state.get(key), str) for key in ('reason', 'requested_at'))
            or (status == 'released' and not isinstance(state.get('released_at'), str))):
        raise ControlError('operator hold record does not match this task')
    return state


def group(task):
    task = _task(task)
    link = task.with_name(GROUP_RECORD)
    if link.is_symlink():
        raise ControlError('task group membership is unsafe')
    if not link.exists():
        return None
    try:
        if not link.is_file() or link.stat().st_size > 4096:
            raise ValueError('invalid task membership record')
        binding = cli.read(link)
        path = Path(binding['group'])
        marker = path.with_name('ready.json')
        if (not path.is_absolute() or path.resolve(strict=True) != path or not path.is_file()
                or path.stat().st_size > 256_000 or marker.is_symlink() or not marker.is_file()
                or marker.stat().st_size > 4096):
            raise ValueError('invalid queue approval record')
        raw = path.read_bytes()
        plan = json.loads(raw)
        if (binding.get('task') != str(task) or hashlib.sha256(raw).hexdigest() != binding['sha256']
                or plan.get('source') != GROUP_SOURCE
                or sum(member.get('task') == str(task) for member in plan['members']) != 1
                or cli.read(marker)['sha256'] != binding['sha256']):
            raise ValueError('changed or incomplete group enrollment')
        return path
    except (OSError, ValueError, KeyError, TypeError, AttributeError, cli.WorkbenchError) as error:
        raise ControlError('task group membership is invalid or incomplete') from error


def _records(task):
    parent = group(task)
    own = _read_own(task)
    inherited = _read_own(parent) if parent else None
    if inherited:
        inherited = {**inherited, 'scope': 'group'}
    return tuple(record for record in (own, inherited) if record)


def read(task):
    records = _records(task)
    return next((item for item in records if item['status'] == 'held'), records[0] if records else None)


def released_pause(task, reason):
    return any(item['status'] == 'released' and item['request'] in reason for item in _records(task))


def hold(task, reason):
    task = _task(task)
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500 or not reason.isprintable():
        raise ControlError('provide a single-line hold reason of at most 500 characters')
    with task_lock(task, lock_name=CONTROL_LOCK, wait=True):
        cli.read(task)
        current = _read_own(task)
        if current and current['status'] == 'held':
            return current
        state = {'schema': cli.SCHEMA, 'task': str(task), 'request': uuid.uuid4().hex, 'status': 'held',
                 'requested_at': cli.now(), 'reason': reason.strip()}
        cli.write(task.with_name(CONTROL_RECORD), state)
        return state


def release(task, *, lock_name='.task.lock'):
    task = _task(task)
    try:
        with task_lock(task, lock_name=lock_name, reentrant=False):
            with task_lock(task, lock_name=CONTROL_LOCK, wait=True):
                state = _read_own(task)
                if not state:
                    raise ControlError('this task has no operator hold')
                if state['status'] == 'held':
                    state.update(status='released', released_at=cli.now())
                    cli.write(task.with_name(CONTROL_RECORD), state)
                return state
    except TaskBusy as error:
        raise ControlError('task is still finishing an admitted step; inspect before releasing the hold') from error


def admit(task, error_type=ControlError):
    """Order this admission with an operator hold using a short separate lock."""
    try:
        task = _task(task)
        parent = group(task)
        with task_lock(parent, lock_name=CONTROL_LOCK, wait=True) if parent else nullcontext():
            with task_lock(task, lock_name=CONTROL_LOCK, wait=True):
                state = read(task)
                if state and state['status'] == 'held':
                    raise Held(f'operator hold {state["request"]}: {state["reason"]}')
    except (ControlError, OSError, cli.WorkbenchError) as error:
        if error_type is ControlError and isinstance(error, ControlError):
            raise
        raise error_type(str(error)) from error


@contextmanager
def delivery_lane(task, repo):
    """Serialize the remote writes of enrolled tasks targeting the same repository."""
    parent = group(task)
    if parent is None:
        yield
        return
    slug = hashlib.sha256(repo.casefold().encode()).hexdigest()[:24]
    with task_lock(parent, lock_name=f'.delivery-{slug}.lock', wait=True):
        admit(task)
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    for action in ('hold', 'release', 'show'):
        command = actions.add_parser(action)
        command.add_argument('--task', required=True)
        if action == 'hold':
            command.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'hold':
            result = hold(args.task, args.reason)
        elif args.action == 'release':
            result = release(args.task)
        else:
            from workbench import pickup
            result = pickup.inspect(args.task)
    except (ControlError, OSError, cli.WorkbenchError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
