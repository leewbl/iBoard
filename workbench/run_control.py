"""Stop admission of new task steps and retain a cross-process hold."""

import argparse
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


def _task(path):
    return Path(path).expanduser().resolve(strict=True)


def read(task):
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


def hold(task, reason):
    task = _task(task)
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500 or not reason.isprintable():
        raise ControlError('provide a single-line hold reason of at most 500 characters')
    with task_lock(task, lock_name=CONTROL_LOCK, wait=True):
        cli.read(task)
        current = read(task)
        if current and current['status'] == 'held':
            return current
        state = {'schema': cli.SCHEMA, 'task': str(task), 'request': uuid.uuid4().hex, 'status': 'held',
                 'requested_at': cli.now(), 'reason': reason.strip()}
        cli.write(task.with_name(CONTROL_RECORD), state)
        return state


def release(task):
    task = _task(task)
    try:
        with task_lock(task, reentrant=False):
            with task_lock(task, lock_name=CONTROL_LOCK, wait=True):
                state = read(task)
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
        with task_lock(task, lock_name=CONTROL_LOCK, wait=True):
            state = read(task)
            if state and state['status'] == 'held':
                raise Held(f'operator hold {state["request"]}: {state["reason"]}')
    except (ControlError, OSError, cli.WorkbenchError) as error:
        if error_type is ControlError and isinstance(error, ControlError):
            raise
        raise error_type(str(error)) from error


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
