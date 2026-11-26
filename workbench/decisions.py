"""Record and audit a task's append-only pstack decision log."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import quote, unquote
import uuid

from workbench import cli
from workbench.task_lock import observe_task_lock, task_lock


PSTACK_SOURCE = 'skills/show-me-your-work/SKILL.md'
FIELDS = ('ts', 'phase', 'decision', 'why', 'evidence', 'result')
HEADER = '\t'.join(FIELDS) + '\n'
COMMIT = re.compile(r'(?:[a-f0-9]{40}|[a-f0-9]{64})')
RUN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
PR = re.compile(r'https://github\.com/[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*/pull/[1-9][0-9]*')


class DecisionError(Exception):
    pass


def _open(task, flags):
    path = task.parent / 'decisions.tsv'
    fd = os.open(path, flags | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise DecisionError('decision log must be a regular file')
    return fd


def read(task):
    try:
        with os.fdopen(_open(task, os.O_RDONLY), encoding='utf-8', newline='') as stream:
            contents = stream.read()
    except FileNotFoundError:
        return []
    if not contents:
        return []
    lines = contents.split('\n')
    if lines.pop() != '' or not lines or lines.pop(0) != HEADER.rstrip('\n'):
        raise DecisionError('decision log has an invalid header or incomplete final row')
    rows = []
    for index, line in enumerate(lines, 2):
        cells = line.split('\t')
        if len(cells) != len(FIELDS) or any(not cell.strip() or '\r' in cell or '\0' in cell for cell in cells):
            raise DecisionError(f'invalid decision log row {index}')
        row = dict(zip(FIELDS, cells))
        if (datetime.fromisoformat(row['ts']).tzinfo is None or
                (row['phase'] == 'start' and not _marked_run(row))):
            raise DecisionError(f'invalid timestamp or run marker on row {index}')
        rows.append(row)
    return rows


def _cell(value):
    if not isinstance(value, str) or '\0' in value or len(value) > 8192:
        raise DecisionError('decision fields must be text under 8193 characters without NUL')
    cleaned = ' '.join(re.split(r'[\r\n\t]+', value)).strip()
    if not cleaned:
        raise DecisionError('decision fields must not be empty')
    return "'" + cleaned if cleaned[0] in '=+-@' else cleaned


def _append(task, cells):
    line = '\t'.join((cli.now(), *cells)) + '\n'
    # The task lock protects the header and semantic run markers across writers.
    with os.fdopen(_open(task, os.O_WRONLY | os.O_CREAT | os.O_APPEND), 'wb') as stream:
        header = HEADER if os.fstat(stream.fileno()).st_size == 0 else ''
        stream.write((header + line).encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())


def _marked_run(row):
    marker = row['evidence'].removeprefix('run:')
    return marker if marker != row['evidence'] and RUN.fullmatch(marker) else None


def _ensure_run(task, rows, run):
    if not RUN.fullmatch(run):
        raise DecisionError('invalid decision run identifier')
    active = next((_marked_run(row) for row in reversed(rows) if row['phase'] == 'start'), None)
    if active == run:
        return rows
    reason = (f'Previous decisions retained from {rows[0]["ts"]} through {rows[-1]["ts"]}.'
              if rows else 'This task has no earlier decision rows.')
    _append(task, ('start', 'Started a decision run.', reason, f'run:{run}', 'Run identified.'))


def begin(task):
    run = uuid.uuid4().hex
    with task_lock(task):
        _ensure_run(task, read(task), run)
    return run


def artifact(task, path):
    path = Path(path)
    if not path.is_absolute():
        path = task.parent / path
    resolved = path.resolve(strict=True)
    if (path.is_symlink() or not resolved.is_relative_to(task.parent) or not resolved.is_file()
            or resolved.name in {'decisions.tsv', '.task.lock'}):
        raise DecisionError('decision artifact must be a regular file in the task directory')
    relative = resolved.relative_to(task.parent).as_posix()
    return f'artifact:{quote(relative)}#sha256={cli.digest(resolved)}'


def _proof(task, workspace, reference, commits):
    if reference.startswith('artifact:'):
        path, marker, digest = reference.removeprefix('artifact:').rpartition('#sha256=')
        relative = Path(unquote(path))
        if not marker or not re.fullmatch(r'[a-f0-9]{64}', digest) or relative.is_absolute() or '..' in relative.parts:
            return 'invalid-reference'
        try:
            return 'hash-matches' if artifact(task, relative) == reference else 'artifact-changed'
        except (DecisionError, cli.WorkbenchError, OSError, ValueError):
            return 'artifact-unavailable'
    if reference.startswith('commit:') and COMMIT.fullmatch(commit := reference.removeprefix('commit:')):
        if commit not in commits:
            try:
                commits[commit] = ('commit-resolves' if cli.sha(workspace, commit, timeout=10) == commit
                                   else 'commit-unavailable')
            except (OSError, cli.WorkbenchError):
                commits[commit] = 'commit-unavailable'
        return commits[commit]
    if PR.fullmatch(reference):
        return 'remote-not-checked'
    if reference == 'record:task.json':
        return 'mutable-record'
    return 'invalid-reference'


def record(task, run, phase, decision, why, evidence, result):
    cells = tuple(_cell(value) for value in (phase, decision, why, evidence, result))
    if cells[0] == 'start':
        raise DecisionError('start is reserved for run markers')
    if not (evidence.startswith(('artifact:', 'commit:')) or PR.fullmatch(evidence)
            or evidence == 'record:task.json'):
        raise DecisionError('use a task artifact, commit, or exact GitHub PR as evidence')
    with task_lock(task):
        _ensure_run(task, read(task), run)
        _append(task, cells)


def audit(task, *, run=None):
    task = Path(task).expanduser().resolve(strict=True)
    with observe_task_lock(task) as lock:
        if lock == 'held':
            raise DecisionError('task is active; audit the log after the current operation')
        data = cli.read(task)
        rows = read(task)
        current_run, results, commits = None, [], {}
        for number, row in enumerate(rows, 2):
            if row['phase'] == 'start':
                current_run = _marked_run(row)
            if run is not None and current_run != run:
                continue
            status = (_proof(task, data['repo'], row['evidence'], commits) if row['phase'] != 'start'
                      else 'run-identified')
            results.append({'row':number, 'run':current_run, **row, 'evidence_status':status})
    bad = {'invalid-reference', 'artifact-changed', 'artifact-unavailable', 'commit-unavailable'}
    if run is not None and not results:
        raise DecisionError('decision run does not exist in the selected task')
    unresolved = [result['row'] for result in results if result['evidence_status'] in bad]
    return {'task':data['id'], 'lock':lock, 'rows':results, 'unresolved_rows':unresolved}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('audit', help='Resolve the evidence pointers in the selected task log')
    inspect.add_argument('--task', required=True)
    inspect.add_argument('--run')
    append = commands.add_parser('record', help='Append a decision under an explicitly chosen run')
    for key in ('task', 'run', 'phase', 'decision', 'why', 'result'):
        append.add_argument('--' + key, required=True)
    source = append.add_mutually_exclusive_group(required=True)
    source.add_argument('--artifact', help='Path to a retained artifact within the task directory')
    source.add_argument('--commit', help='Full commit SHA in the task workspace')
    source.add_argument('--pr', help='Canonical GitHub pull request URL')
    args = parser.parse_args(argv)
    try:
        task = Path(args.task).expanduser().resolve(strict=True)
        if args.command == 'audit':
            report = audit(task, run=args.run)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1 if report['unresolved_rows'] else 0
        data = cli.read(task)
        if args.artifact is not None:
            evidence = artifact(task, args.artifact)
        elif args.commit is not None:
            evidence = 'commit:' + args.commit
        else:
            evidence = args.pr
        if _proof(task, data['repo'], evidence, {}) not in {'hash-matches', 'commit-resolves', 'remote-not-checked'}:
            parser.error('the selected decision evidence is unavailable or invalid')
        record(task, args.run, args.phase, args.decision, args.why, evidence, args.result)
        return 0
    except (DecisionError, OSError, ValueError, cli.WorkbenchError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
