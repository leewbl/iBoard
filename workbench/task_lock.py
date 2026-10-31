"""Coordinate cooperating processes operating on one task directory."""
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
from functools import wraps
from inspect import signature
import os
from pathlib import Path


class TaskBusy(OSError):
    pass


_held = ContextVar('workbench_task_locks', default=frozenset())


@contextmanager
def task_lock(task):
    task = Path(task).expanduser().resolve(strict=True)
    if task in _held.get():
        yield
        return
    path = task.parent / '.task.lock'
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TaskBusy(f'task is already being operated on: {task}') from error
        token = _held.set(_held.get() | {task})
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
