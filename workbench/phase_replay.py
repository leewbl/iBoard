"""Replay a verified phase on a new base without changing the original branch."""

import hashlib
import json
from pathlib import Path
import re
import time

from workbench import arena, cli, stack_rebase


class ReplayError(ValueError):
    pass


COMMIT = re.compile(r'[a-f0-9]{40}')
SHA256 = re.compile(r'[a-f0-9]{64}')


def _deadline(timeout):
    if type(timeout) is not int or not 1 <= timeout <= 2_147_483_647:
        raise ReplayError('phase replay requires a positive timeout')
    deadline = time.monotonic() + timeout

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise ReplayError('phase replay time limit reached')
        return min(120, seconds)

    return remaining


def _git(repo, *args, remaining, stdin=None):
    return stack_rebase._output(stack_rebase._git(repo, *args, seconds=remaining(), stdin=stdin))


def _operands(repo, trunk, old_base, old_head, new_base, files):
    repo = Path(repo).expanduser().resolve(strict=True)
    revisions = {'trunk': trunk, 'old_base': old_base, 'old_head': old_head, 'new_base': new_base}
    if any(not isinstance(value, str) or not COMMIT.fullmatch(value) for value in revisions.values()):
        raise ReplayError('phase replay requires exact Git revision IDs')
    if old_base == new_base:
        raise ReplayError('phase replay requires a changed base')
    return repo, {**revisions, 'files': sorted(arena._paths(files))}


def _ancestry(repo, inputs, remaining):
    for key in ('trunk', 'old_base', 'old_head', 'new_base'):
        revision = inputs[key]
        actual = _git(repo, 'rev-parse', '--verify', revision + '^{commit}', remaining=remaining)
        if actual.decode().strip() != revision:
            raise ReplayError('phase replay revision did not identify the exact commit')
    for key in ('old_base', 'new_base'):
        result = stack_rebase._git(repo, 'merge-base', '--is-ancestor', inputs['trunk'], inputs[key],
                                   seconds=remaining())
        if result.returncode == 1:
            raise ReplayError(f'phase {key} does not descend from the approved trunk')
        stack_rebase._output(result)


def _paths(raw):
    return {path.decode('utf-8') for path in raw.split(b'\0') if path}


def _patch(repo, base, head, remaining):
    args = ('--no-ext-diff', '--no-textconv', '--no-color', '--no-renames', '--binary',
            '--full-index', '--diff-algorithm=myers', '--no-indent-heuristic')
    raw = _git(repo, 'diff', *args, '--unified=3', base, head, '--', remaining=remaining)
    ids = _git(repo, 'patch-id', '--stable', remaining=remaining, stdin=raw).split()
    if len(ids) != 2 or not COMMIT.fullmatch(ids[0].decode('ascii')):
        raise ReplayError('phase change has no stable patch identity')
    # Stable patch-id alone ignores whitespace in changed lines. Keep exact bytes
    # while ignoring hunk positions and changed blob IDs after replay.
    delta = _git(repo, 'diff', *args, '--unified=0', base, head, '--', remaining=remaining)
    exact = []
    for line in delta.split(b'\n'):
        if line.startswith(b'index '):
            continue
        if line.startswith(b'@@ '):
            line = re.sub(rb'^@@ -[0-9]+(,[0-9]+)? \+[0-9]+(,[0-9]+)? @@.*$', rb'@@\1 \2@@', line)
        exact.append(line)
    return ids[0].decode('ascii'), hashlib.sha256(b'\n'.join(exact)).hexdigest()


def _shape(repo, base, head, files, remaining):
    rows = _git(repo, 'rev-list', '--first-parent', '--parents', f'{base}..{head}',
                remaining=remaining).decode().splitlines()
    history = [row.split() for row in rows]
    if not history or any(len(row) != 2 for row in history) or history[-1][1] != base:
        raise ReplayError('phase replay requires nonempty linear history at its exact base')
    net = _paths(_git(repo, 'diff', '--name-only', '-z', '--no-renames', base, head, '--', remaining=remaining))
    touched = _paths(_git(repo, 'log', '--format=', '--name-only', '-z', '--no-renames', f'{base}..{head}',
                          '--', remaining=remaining))
    if not net or not (net | touched) <= set(files):
        raise ReplayError('phase change is empty or includes paths outside the approved phase')
    stable, exact = _patch(repo, base, head, remaining)
    commits = [_patch(repo, parent, commit, remaining) for commit, parent in reversed(history)]
    return {'patch_id': stable, 'patch_sha256': exact, 'changed': sorted(net),
            'commit_patches': [{'patch_id': item[0], 'patch_sha256': item[1]} for item in commits]}


def _reference(path):
    return {'report': str(path), 'sha256': cli.digest(path)}


def prepare(repo, *, trunk, old_base, old_head, new_base, files, out, timeout):
    """Create a replay receipt, retaining failed attempts and conflict worktrees."""
    repo, inputs = _operands(repo, trunk, old_base, old_head, new_base, files)
    remaining = _deadline(timeout)
    directory = Path(out).expanduser().resolve()
    if directory.is_relative_to(repo):
        raise ReplayError('keep replay evidence outside the source checkout')
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    path = directory / 'result.json'
    worktree = directory / 'source'
    report = {'schema': cli.SCHEMA, 'status': 'blocked', **inputs, 'worktree': str(worktree)}
    try:
        _ancestry(repo, inputs, remaining)
        original = _shape(repo, old_base, old_head, inputs['files'], remaining)
        _git(repo, 'worktree', 'add', '--detach', str(worktree), old_head, remaining=remaining)
        replay = stack_rebase._git(worktree, '-c', 'user.name=iBoard Phase Replay',
                                  '-c', 'user.email=phase-replay@example.invalid',
                                  '-c', 'notes.rewrite.rebase=false', 'rebase', '--merge',
                                  '--no-rebase-merges', '--no-autostash', '--reapply-cherry-picks',
                                  '--committer-date-is-author-date', '--no-keep-empty', '--empty=stop',
                                  '--onto', new_base, old_base, seconds=remaining())
        log = directory / 'rebase.log'
        log.write_bytes(replay.stdout + replay.stderr)
        log.chmod(0o600)
        report['log'] = {'path': str(log), 'sha256': cli.digest(log)}
        stack_rebase._output(replay)
        candidate = _git(worktree, 'rev-parse', '--verify', 'HEAD^{commit}', remaining=remaining).decode().strip()
        if _git(worktree, 'status', '--porcelain', '--untracked-files=all', remaining=remaining):
            raise ReplayError('replay worktree is not clean after Git finished')
        if original != _shape(repo, new_base, candidate, inputs['files'], remaining):
            raise ReplayError('replay changed the verified phase patch or commit sequence')
        report.update(status='replayed', candidate=candidate, **original)
    except (OSError, ReplayError, stack_rebase.RebaseError, cli.WorkbenchError) as error:
        report['reason'] = str(error)[:1800] or 'Git could not verify the replay ancestry or patch'
    cli.write(path, report)
    return {**report, **_reference(path)}


def validate(reference, *, repo, trunk, old_base, old_head, new_base, files):
    """Check a retained replay against current Git objects without editing a worktree."""
    try:
        repo, inputs = _operands(repo, trunk, old_base, old_head, new_base, files)
        if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
                or not isinstance(reference['sha256'], str) or not SHA256.fullmatch(reference['sha256'])):
            raise ReplayError('phase replay receipt requires a path and SHA-256')
        path = Path(reference['path']).expanduser()
        if path.name != 'result.json' or path.is_symlink() or not path.is_file() or path != path.resolve():
            raise ReplayError('phase replay receipt must use its exact absolute result path')
        with path.open('rb') as stream:
            raw = stream.read(128_001)
        if len(raw) > 128_000 or hashlib.sha256(raw).hexdigest() != reference['sha256']:
            raise ReplayError('phase replay receipt digest changed')
        receipt = json.loads(raw)
        if (not isinstance(receipt, dict) or receipt.get('schema') != cli.SCHEMA
                or receipt.get('status') != 'replayed' or any(receipt.get(k) != v for k, v in inputs.items())
                or receipt.get('worktree') != str(path.parent / 'source')):
            raise ReplayError('phase replay receipt does not match the authorized revisions and scope')
        candidate = receipt.get('candidate')
        if not isinstance(candidate, str) or not COMMIT.fullmatch(candidate):
            raise ReplayError('phase replay result needs its exact candidate commit')
        log = path.parent / 'rebase.log'
        if (log.is_symlink() or not log.is_file()
                or receipt.get('log') != {'path': str(log), 'sha256': cli.digest(log)}):
            raise ReplayError('phase replay log changed after the run')
        remaining = _deadline(60)
        _ancestry(repo, inputs, remaining)
        old = _shape(repo, old_base, old_head, inputs['files'], remaining)
        current = _shape(repo, new_base, candidate, inputs['files'], remaining)
        if old != current or any(receipt.get(k) != v for k, v in old.items()):
            raise ReplayError('phase replay no longer matches the exact verified patch sequence')
        return receipt
    except (OSError, stack_rebase.RebaseError, cli.WorkbenchError, TypeError) as error:
        raise ReplayError(f'could not validate phase replay: {error}') from error
