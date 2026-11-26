"""Coordinate cooperating processes operating on one task directory."""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
from functools import wraps
from inspect import signature
import os
from pathlib import Path
import stat


class TaskBusy(OSError):
    pass


_held = ContextVar('workbench_task_locks', default=frozenset())


@contextmanager
def observe_task_lock(task):
    """Observe an existing lock and keep idle records stable during inspection."""
    path = task.parent / '.task.lock'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        yield 'not-recorded'
        return
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('task lock is not a regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield 'held'
        else:
            yield 'idle'
    finally:
        os.close(fd)


@contextmanager
def task_lock(task, *, lock_name='.task.lock'):
    task = Path(task).expanduser().resolve(strict=True)
    lock_key = (task, lock_name)
    if lock_key in _held.get():
        yield
        return
    path = task.parent / lock_name
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TaskBusy(f'task is already being operated on: {task}') from error
        token = _held.set(_held.get() | {lock_key})
        try:
            yield
        finally:
            _held.reset(token)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def locked_task(parameter):
    """Lock around a public task operation, including calls nested in autopilot."""
    def decorate(function):
        sig = signature(function)
        @wraps(function)
        def wrapped(*args, **kwargs):
            task = sig.bind(*args, **kwargs).arguments[parameter]
            with task_lock(task):
                return function(*args, **kwargs)
        return wrapped
    return decorate
