"""Read local process and Claude Code usage of registered Git worktrees."""

from collections import defaultdict
from itertools import chain
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


MAX_PROBE_BYTES = 16_000_000
MAX_OPEN_PATHS = 160_000
MAX_CLAUDE_RECORD = 64_000


def _lsof_records(raw):
    processes, items = {}, []
    pid, descriptor = None, None
    for field in raw.split(b'\0'):
        field = field.removeprefix(b'\n')
        tag, value = field[:1], field[1:]
        if tag == b'p':
            if not value.isdigit() or int(value) < 1:
                raise ValueError('invalid lsof process record')
            pid, descriptor = int(value), None
            processes[pid] = ''
        elif tag == b'c' and pid is not None:
            processes[pid] = os.fsdecode(value)
        elif tag == b'f' and pid is not None:
            descriptor = value
        elif tag == b'n' and pid is not None and descriptor is not None:
            if descriptor in (b'cwd', b'txt') or descriptor.isdigit():
                items.append((pid, 'cwd' if descriptor == b'cwd' else 'open-file', os.fsdecode(value)))
    if not processes or len(items) > MAX_OPEN_PATHS:
        raise ValueError('lsof inventory was empty or exceeded the observation limit')
    return processes, items


def _lsof():
    binary = shutil.which('lsof')
    if not binary:
        raise OSError('lsof is unavailable')
    with tempfile.TemporaryFile() as output:
        completed = subprocess.run([binary, '-nP', '-w', '-u', str(os.getuid()), '-F0pcfn'],
                                   stdout=output, stderr=subprocess.DEVNULL, timeout=15)
        if completed.returncode != 0 or output.tell() > MAX_PROBE_BYTES:
            raise OSError('lsof did not return a bounded successful inventory')
        output.seek(0)
        return _lsof_records(output.read(MAX_PROBE_BYTES))


def _procfs():
    processes, items = {}, []
    for entry in Path('/proc').iterdir():
        if not entry.name.isascii() or not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            process = int(entry.name)
            command = (entry / 'comm').read_text(errors='replace').strip()
            processes[process] = command
            cwd = entry / 'cwd'
            executable = entry / 'exe'
            descriptors = entry / 'fd'
            for resource, access in ((cwd, 'cwd'), (executable, 'open-file')):
                try:
                    items.append((process, access, os.readlink(resource)))
                except FileNotFoundError:
                    continue
            for fd in descriptors.iterdir():
                try:
                    items.append((process, 'open-file', os.readlink(fd)))
                except FileNotFoundError:
                    continue
                if len(items) > MAX_OPEN_PATHS:
                    raise ValueError('process paths exceeded the observation limit')
        except (FileNotFoundError, ProcessLookupError):
            continue  # Processes and descriptors may exit between listing and reading.
    if not processes or len(items) > MAX_OPEN_PATHS:
        raise ValueError('procfs inventory was empty or exceeded the observation limit')
    return processes, items


def _checkout(path, choices):
    if not isinstance(path, str) or not path.startswith('/'):
        return None
    path = os.path.normpath(path)
    while path:
        if path in choices:
            return path
        path, _, _ = path.rpartition('/')
    return None


def _claude_sessions(processes):
    default = Path.home() / '.claude'
    root = Path(os.getenv('CLAUDE_CONFIG_DIR') or default).expanduser() / 'sessions'
    records, unreadable = [], 0
    try:
        folder = root.stat()
    except FileNotFoundError:
        return {'status': 'not-present', 'matched_pids': 0}, records
    except OSError:
        return {'status': 'partial', 'matched_pids': 0, 'unreadable': 1}, records
    if not stat.S_ISDIR(folder.st_mode):
        return {'status': 'partial', 'matched_pids': 0, 'unreadable': 1}, records
    try:
        for file in root.iterdir():
            if file.suffix != '.json' or not re.fullmatch(r'[1-9][0-9]{0,9}', file.stem):
                continue
            pid = int(file.stem)
            if processes.get(pid, '').casefold() not in {'claude', 'claude code', 'node'}:
                continue
            try:
                with os.fdopen(os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError('expected a regular session manifest')
                    raw = source.read(MAX_CLAUDE_RECORD + 1)
                if len(raw) > MAX_CLAUDE_RECORD:
                    raise ValueError('session manifest exceeded the byte limit')
                row = json.loads(raw)
                if not isinstance(row, dict) or row.get('pid') != pid or not isinstance(row.get('cwd'), str):
                    raise ValueError('session manifest has no matching process or workspace')
            except (OSError, ValueError):
                unreadable += 1
                continue
            records.append((pid, 'claude-session', row['cwd']))
    except OSError:
        return {'status': 'partial', 'matched_pids': len(records), 'unreadable': unreadable + 1}, records
    return {'status': 'partial' if unreadable else 'available', 'matched_pids': len(records),
            'unreadable': unreadable}, records


def scan(paths):
    """Return current-user coverage and evidence scoped to the registered checkout paths."""
    backend = 'procfs' if sys.platform.startswith('linux') else 'lsof'
    summary = {'backend': backend, 'status': 'unavailable', 'scope': 'local current-user processes'}
    try:
        processes, items = (_procfs if backend == 'procfs' else _lsof)()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {**summary, 'reason': str(error)[:240]}, {}
    coverage, records = _claude_sessions(processes)
    groups = defaultdict(set)
    selected = set(paths)
    for pid, access, path in chain(items, records):
        tree = _checkout(path, selected)
        if tree:
            groups[tree].add((pid, access))
    matched = {tree: [{'pid': pid, 'access': access} for pid, access in sorted(refs)]
               for tree, refs in groups.items()}
    status = 'partial' if coverage['status'] == 'partial' else 'available'
    return {**summary, 'status': status, 'processes_observed': len(processes),
            'claude_sessions': coverage}, matched
