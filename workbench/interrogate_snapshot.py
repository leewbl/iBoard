"""Freeze visible worktree changes as an exact, detached Interrogate candidate."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from workbench import arena, autopilot, cli, swarm


SOURCE = 'skills/interrogate/SKILL.md'
OID = re.compile(r'[0-9a-f]{40}')
DIGEST = re.compile(r'[0-9a-f]{64}')
FILE_LIMIT = 512 * 1024
TOTAL_LIMIT = 4 * 1024 * 1024
MODES = {'100644', '100755'}


class SnapshotError(ValueError):
    pass


def _git(repo, *args, index=None, data=None, success=(0,)):
    env = {key: value for key, value in os.environ.items()
           if key not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                          'GIT_NAMESPACE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES')}
    env.update(GIT_NO_LAZY_FETCH='1', GIT_AUTHOR_NAME='iBoard', GIT_COMMITTER_NAME='iBoard',
               GIT_AUTHOR_EMAIL='snapshot@iboard.invalid', GIT_COMMITTER_EMAIL='snapshot@iboard.invalid')
    if index is not None:
        env['GIT_INDEX_FILE'] = str(index)
    command = ['git', '--no-replace-objects', '--no-optional-locks', '--literal-pathspecs', '-C', str(repo),
               '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', *args]
    result = subprocess.run(command, input=data, capture_output=True, env=env, timeout=20, check=False)
    if result.returncode not in success:
        raise SnapshotError(f'Snapshot Git operation failed: {args[0]}')
    return result.stdout


def _revision(repo, name):
    return _git(repo, 'rev-parse', '--verify', name).decode().strip()


def _exact_commit(repo, commit):
    if not isinstance(commit, str) or not OID.fullmatch(commit) or _revision(repo, commit + '^{commit}') != commit:
        raise SnapshotError('Snapshot requires an exact Git commit')


def _paths(paths):
    arena._paths(paths)
    if any(not name.isprintable() or '.git' in name.lower().split('/') for name in paths):
        raise SnapshotError('Snapshot paths must be printable and outside Git metadata')
    return paths


def _names(raw):
    return [item.decode('utf-8') for item in raw.split(b'\0') if item]


def _changed(repo, before, after=None):
    commits = [before] if after is None else [before, after]
    return _names(_git(repo, 'diff', '--name-only', '--no-renames', '--no-ext-diff', '--no-textconv',
                       '-z', *commits, '--'))


def _tree(repo, commit, paths):
    entries = {}
    for entry in _names(_git(repo, 'ls-tree', '-z', '--full-tree', commit, '--', *paths)):
        header, path = entry.split('\t', 1)
        mode, kind, oid = header.split()
        if kind != 'blob' or mode not in MODES:
            raise SnapshotError(f'Snapshot requires regular files: {path}')
        entries[path] = (mode, oid)
    return entries


def _read(repo, name):
    # Open every path component relative to the preceding directory without following symlinks.
    folder = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
    parts = name.split('/')
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            os.close(folder)
            folder = child
        file = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        try:
            info = os.fstat(file)
            if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_LIMIT:
                raise SnapshotError(f'Snapshot requires a bounded regular file: {name}')
            with os.fdopen(file, 'rb', closefd=False) as handle:
                content = handle.read(FILE_LIMIT + 1)
        finally:
            os.close(file)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SnapshotError(f'Cannot open snapshot path as a regular file: {name}') from error
    finally:
        os.close(folder)
    if len(content) > FILE_LIMIT:
        raise SnapshotError(f'Snapshot file exceeded its byte limit: {name}')
    return ('100755' if info.st_mode & stat.S_IXUSR else '100644'), content


def _index_state(repo):
    location = Path(_git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'index').decode().rstrip('\n'))
    return cli.digest(location) if location.exists() else None


def _assert_index_ready(repo):
    if _git(repo, 'ls-files', '--unmerged', '-z'):
        raise SnapshotError('Resolve index conflicts before freezing a worktree')
    if any(record.startswith('S ') for record in _names(_git(repo, 'ls-files', '-t', '-z'))):
        raise SnapshotError('Snapshot requires a full checkout')


def _audit_rows(repo, head, candidate, paths):
    before = _tree(repo, head, paths)
    after = _tree(repo, candidate, paths)
    rows = {}
    total = 0
    for name in paths:
        if name not in after:
            rows[name] = {'action': 'deleted'}
            continue
        mode, oid = after[name]
        size = int(_git(repo, 'cat-file', '-s', oid))
        total += size
        if size > FILE_LIMIT or total > TOTAL_LIMIT:
            raise SnapshotError('Snapshot exceeded its file or total byte limit')
        contents = _git(repo, 'cat-file', 'blob', oid)
        autopilot._reject_credential_artifact(name, contents)
        rows[name] = {'action': 'modified' if name in before else 'added',
                      'mode': mode, 'blob': oid, 'sha256': hashlib.sha256(contents).hexdigest()}
    return rows


def freeze(*, repo, baseline, out, files=None):
    repo = Path(repo).expanduser().resolve(strict=True)
    out = Path(out).expanduser().resolve()
    if out.exists() or any(parent.exists() and parent.samefile(repo) for parent in out.parents):
        raise SnapshotError('Select a fresh snapshot receipt outside the inspected checkout')
    if Path(_git(repo, 'rev-parse', '--show-toplevel').decode().rstrip('\n')).resolve() != repo:
        raise SnapshotError('Specify the root of the inspected worktree')
    _exact_commit(repo, baseline)
    head = _revision(repo, 'HEAD')
    _git(repo, 'merge-base', '--is-ancestor', baseline, head)
    index_before = _index_state(repo)
    _assert_index_ready(repo)
    # Git tracks ignore rules for untracked paths separately from the committed and staged diff.
    unignored = set(_names(_git(repo, 'ls-files', '--others', '--exclude-standard', '-z')))
    staged_new = set(_names(_git(repo, 'diff', '--cached', '--diff-filter=A', '--name-only',
                                '--no-renames', '--no-ext-diff', '-z', head, '--')))
    available = unignored | staged_new
    selection = 'explicit' if files is not None else 'worktree'
    paths = _paths(sorted(set(_changed(repo, head)).union(unignored)) if files is None else files)
    previous = _tree(repo, head, paths)
    observed = {}
    total = 0
    for name in paths:
        revision = _read(repo, name)
        if name not in previous and (revision is None or name not in available):
            raise SnapshotError(f'Snapshot file is missing or ignored: {name}')
        if revision is not None:
            total += len(revision[1])
            if total > TOTAL_LIMIT:
                raise SnapshotError('Snapshot exceeded its total byte limit')
            autopilot._reject_credential_artifact(name, revision[1])
        observed[name] = revision

    with tempfile.TemporaryDirectory(prefix='iboard-interrogate-index-') as tmp:
        index = Path(tmp) / 'index'
        _git(repo, 'read-tree', head, index=index)
        updates = []
        for name, revision in observed.items():
            if revision is None:
                mode, oid = '0', '0' * 40
            else:
                mode, body = revision
                oid = _git(repo, 'hash-object', '-w', '--no-filters', '--stdin', data=body).decode().strip()
            updates.append(f'{mode} {oid}\t{name}\0'.encode('utf-8'))
        _git(repo, 'update-index', '-z', '--index-info', index=index, data=b''.join(updates))
        tree = _git(repo, 'write-tree', index=index).decode().strip()
    changes = _changed(repo, head, tree)
    if not changes or not set(changes) <= set(paths):
        raise SnapshotError('Snapshot needs worktree changes limited to the selected files')
    if (any(_read(repo, name) != revision for name, revision in observed.items())
            or _revision(repo, 'HEAD') != head or _index_state(repo) != index_before
            or (selection == 'worktree' and paths != sorted(set(_changed(repo, head)).union(
                _names(_git(repo, 'ls-files', '--others', '--exclude-standard', '-z')))))):
        raise SnapshotError('Selected worktree, HEAD or index changed during snapshot')
    candidate = _git(repo, 'commit-tree', tree, '-p', head,
                     data=b'Freeze worktree for independent Interrogate review\n').decode().strip()
    # Keep the candidate available for later audit without updating a branch.
    pin = f'refs/iboard/interrogate/{candidate}'
    _git(repo, 'update-ref', '--no-deref', pin, candidate)
    receipt = {'schema': cli.SCHEMA, 'source': SOURCE, 'kind': 'interrogate-worktree', 'repo': str(repo),
               'baseline': baseline, 'head': head, 'candidate': candidate, 'tree': tree, 'selection': selection,
               'paths': paths, 'pin': pin, 'changes': _audit_rows(repo, head, candidate, changes),
               'source_index_sha256': index_before}
    out.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return {**receipt, 'snapshot': str(out), 'sha256': cli.digest(out)}


def load(reference, *, repo, baseline, candidate=None):
    if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
            or not isinstance(reference['sha256'], str) or not DIGEST.fullmatch(reference['sha256'])):
        raise SnapshotError('Supply the approved worktree snapshot path and SHA-256')
    path = Path(reference['path']).expanduser().absolute()
    data = swarm._read(path, reference['sha256'], 64_000)
    repo = Path(repo).expanduser().resolve(strict=True)
    if ((data.get('kind'), data.get('source'), data.get('repo'), data.get('baseline'))
            != ('interrogate-worktree', SOURCE, str(repo), baseline)):
        raise SnapshotError('Snapshot belongs to a different repository or baseline')
    head, frozen = data['head'], data['candidate']
    for commit in (baseline, head, frozen):
        _exact_commit(repo, commit)
    if candidate is not None and candidate != frozen:
        raise SnapshotError('Snapshot belongs to a different candidate')
    _git(repo, 'merge-base', '--is-ancestor', baseline, head)
    ancestry = _git(repo, 'rev-list', '--parents', '-n1', frozen).decode().split()
    if (ancestry != [frozen, head] or _revision(repo, frozen + '^{tree}') != data['tree']
            or data.get('pin') != f'refs/iboard/interrogate/{frozen}'
            or data.get('selection') not in ('explicit', 'worktree')):
        raise SnapshotError('Snapshot ancestry or tree changed')
    paths = _paths(data['paths'])
    changed = _changed(repo, head, frozen)
    if not changed or not set(changed) <= set(paths) or _audit_rows(repo, head, frozen, changed) != data['changes']:
        raise SnapshotError('Snapshot scope or frozen content changed')
    return data


def check_checkout(workspace, snapshot):
    """Catch checkout transformations before running any probe on the frozen bytes."""
    for name, entry in snapshot['changes'].items():
        current = _read(workspace, name)
        if entry['action'] == 'deleted':
            if current is None:
                continue
        elif current is not None and (current[0] == entry['mode']
                                     and hashlib.sha256(current[1]).hexdigest() == entry['sha256']):
            continue
        raise SnapshotError(f'Checked-out snapshot bytes or mode changed: {name}')


def candidate_options(parser):
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--candidate', help='exact committed candidate')
    source.add_argument('--snapshot', help='receipt of a frozen worktree candidate')
    parser.add_argument('--snapshot-sha256', help='approved digest for --snapshot')


def resolve_candidate(*, repo, baseline, candidate, path, sha256):
    if path is None:
        if sha256 is not None:
            raise SnapshotError('--snapshot-sha256 needs --snapshot')
        return candidate, None
    reference = {'path': str(Path(path).expanduser().absolute()), 'sha256': sha256}
    return load(reference, repo=repo, baseline=baseline)['candidate'], reference


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('repo', 'baseline', 'out'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--file', dest='files', action='append')
    args = parser.parse_args(argv)
    try:
        receipt = freeze(**vars(args))
    except (OSError, ValueError, subprocess.SubprocessError, arena.ArenaError, autopilot.AutopilotError) as error:
        parser.error(str(error))
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
