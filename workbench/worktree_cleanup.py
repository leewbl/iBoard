"""Inventory Git worktrees and remove an explicitly approved clean, merged subset."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from workbench import cli, pr_watch, stack_rebase, swarm, worktree_usage
from workbench.task_lock import task_lock


SOURCE = 'skills/poteto-mode/playbooks/worktree-cleanup.md'
ERRORS = (*swarm.ERRORS, pr_watch.WatchError, cli.WorkbenchError)
LIVE_HOLD = 'live local process or Claude session'
LIVE_UNKNOWN = 'live local usage could not be verified'


class CleanupError(ValueError):
    pass


def _git(repo, *args, timeout=20, optional=False):
    result = stack_rebase._git(repo, '--no-optional-locks', *args, seconds=timeout)
    if optional and result.returncode == 1:
        return None
    return stack_rebase._output(result)


def _records(repo):
    raw = _git(repo, 'worktree', 'list', '--porcelain', '-z')
    records = []
    for block in raw.decode('utf-8').split('\0\0'):
        if not block:
            continue
        rows = {}
        for line in block.split('\0'):
            key, _, value = line.partition(' ')
            if key in rows:
                raise CleanupError('duplicate worktree metadata')
            rows[key] = value
        if 'worktree' not in rows:
            raise CleanupError('worktree list did not contain registered paths')
        path = Path(rows['worktree'])
        if not path.is_absolute() or not str(path).isprintable():
            raise CleanupError('worktree path is not a printable absolute path')
        records.append({'path': str(path), 'head': rows.get('HEAD'), 'branch': rows.get('branch'),
                        'primary': not records, 'bare': 'bare' in rows,
                        'locked': 'locked' in rows, 'prunable': 'prunable' in rows})
    if not records or len({row['path'] for row in records}) != len(records):
        raise CleanupError('no unambiguous Git worktree inventory')
    return records


def _absolute(path):
    path = Path(path).expanduser()
    if not path.is_absolute() or not str(path).isprintable() or '..' in path.parts:
        raise CleanupError('use a printable absolute path')
    return path


def _holds(path):
    if path is None:
        return None, []
    original = _absolute(path)
    digest = cli.digest(original)
    data = swarm._read(original, digest)
    if set(data) != {'schema', 'entries'} or not isinstance(data['entries'], list) or len(data['entries']) > 800:
        raise CleanupError('invalid worktree usage snapshot')
    holds = []
    for item in data['entries']:
        if (not isinstance(item, dict) or set(item) != {'path', 'reason'}
                or not isinstance(item['reason'], str) or not item['reason'].strip()
                or len(item['reason']) > 500 or not item['reason'].isprintable()):
            raise CleanupError('a retained checkout needs an absolute path and a reason')
        holds.append({'path': str(_absolute(item['path']).resolve()), 'reason': item['reason']})
    return digest, holds


def _registry(roots):
    records = {}
    seen = set()

    def inaccessible(error):
        raise CleanupError('a configured task registry could not be inspected') from error

    for root in roots:
        if root.is_symlink() or not root.is_dir():
            raise CleanupError(f'task registry unavailable: {root}')
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=inaccessible):
            parent = Path(directory)
            if (parent / '.git').exists():
                dirs[:] = []
                continue
            dirs[:] = sorted(name for name in dirs if not name.startswith('.') and not (parent / name).is_symlink())
            if 'task.json' not in files or parent in seen:
                continue
            seen.add(parent)
            child = parent / 'task.json'
            if child.is_symlink() or not child.is_file() or child.stat().st_size > 8_000_000:
                raise CleanupError('a configured task record cannot be inspected')
            record = cli.read(child)
            if not isinstance(record.get('repo'), str):
                raise CleanupError(f'task record has no worktree: {child}')
            checkout = str(_absolute(record['repo']).resolve())
            records.setdefault(checkout, []).append(
                {'record': str(child), 'id': record.get('id'), 'status': record.get('status')})
    return records


def _pr_heads(destination, reader):
    if destination is None:
        return []
    if not isinstance(destination, str) or not pr_watch.REPO.fullmatch(destination):
        raise CleanupError('use an exact GitHub owner/repository')
    pulls = (reader or pr_watch.GitHubReader()).open_pulls(destination)
    if not isinstance(pulls, list) or len(pulls) >= 201:
        raise CleanupError('open PR inventory is incomplete')
    if any(not isinstance(row, dict) or type(row.get('number')) is not int or row['number'] < 1
           or not isinstance(row.get('headRefName'), str) or not row['headRefName']
           or not isinstance(row.get('headRefOid'), str) or not pr_watch.SHA.fullmatch(row['headRefOid'])
           for row in pulls):
        raise CleanupError('open PR inventory is missing head identities')
    return [{key: row[key] for key in ('number', 'headRefName', 'headRefOid')} for row in pulls]


def _size(path):
    try:
        result = subprocess.check_output(['du', '-sk', str(path)], stderr=subprocess.DEVNULL, timeout=20)
        return int(result.split(None, 1)[0]) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _status(path):
    raw = _git(path, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignored=matching')
    counts = {'tracked_changes': 0, 'untracked': 0, 'ignored': 0}
    samples = {key: [] for key in counts}
    skip = False
    for record in raw.split(b'\0'):
        if skip:
            skip = False  # The second NUL field of a rename is its previous path.
            continue
        if not record:
            continue
        change, name = record[:2], record[3:].decode('utf-8', errors='replace')
        key = {b'??': 'untracked', b'!!': 'ignored'}.get(change, 'tracked_changes')
        counts[key] += 1
        if len(samples[key]) < 12:
            samples[key].append(name)
        skip = b'R' in change or b'C' in change
    return counts, samples


def _describe(repo, row, base, registry, holds, pulls, *, checkout, measure_size, usage, live):
    path = Path(row['path'])
    status = {**row, 'reasons': []}
    reasons = status['reasons']
    task_refs = [task for source, tasks in registry.items() if Path(source).is_relative_to(path) for task in tasks]
    protected = [item for item in holds
                 if Path(item['path']).is_relative_to(path) or path.is_relative_to(Path(item['path']))]
    branch = (row['branch'] or '').removeprefix('refs/heads/')
    prs = [item['number'] for item in pulls if item['headRefOid'] == row['head']
           or branch and item['headRefName'] == branch]
    status.update(tasks=task_refs, retained=protected, live_usage=live, open_prs=prs, measured_bytes=None)
    if row['primary'] or row['bare'] or checkout.is_relative_to(path) or Path.cwd().resolve().is_relative_to(path):
        reasons.append('primary or current checkout')
    if row['locked'] or row['prunable']:
        reasons.append('Git marks worktree locked or prunable')
    if task_refs:
        reasons.append('retained iBoard task or verification evidence')
    if protected:
        reasons.append('active or pinned usage')
    if live:
        reasons.append(LIVE_HOLD)
    if not row['primary'] and usage['status'] != 'available':
        reasons.append(LIVE_UNKNOWN)
    if prs:
        reasons.append('open PR')
    if not path.is_dir() or path.is_symlink() or path.resolve() != path:
        reasons.append('missing or indirect worktree')
    elif not row['bare']:
        stat = path.stat()
        status['identity'] = {'device': stat.st_dev, 'inode': stat.st_ino}
        status['modified_epoch'] = int(stat.st_mtime)
        try:
            counts, samples = _status(path)
            status.update(counts, file_samples=samples)
            if counts['tracked_changes']:
                reasons.append('tracked work in progress')
            if counts['untracked']:
                reasons.append('untracked files')
            if counts['ignored']:
                reasons.append('ignored files')
            status['merged'] = (base is not None and row['head'] is not None and
                                _git(repo, 'merge-base', '--is-ancestor', row['head'], base, optional=True) is not None)
            if not status['merged']:
                reasons.append('HEAD not proven merged into selected base')
        except ERRORS:
            reasons.append('could not verify clean state and ancestry')
        if measure_size:
            status['measured_bytes'] = _size(path)
    if not reasons:
        status['bucket'] = 'candidate'
    else:
        status['bucket'] = 'keep'
    return status


def _summary_path(path, checkouts):
    original = _absolute(path)
    if original.is_symlink():
        raise CleanupError('cleanup receipt cannot be a symbolic link')
    output = original.resolve()
    if any(output.is_relative_to(Path(row['path'])) and not row['primary'] for row in checkouts):
        raise CleanupError('keep the receipt outside linked worktrees')
    return output


def inventory(repo, base_ref='origin/HEAD', *, holds=None, tasks_roots=(), destination=None, measure_size=False,
              reader=None):
    repo = _absolute(repo).resolve(strict=True)
    if (not isinstance(base_ref, str) or not base_ref or len(base_ref) > 300
            or not base_ref.isprintable() or base_ref.startswith('-')):
        raise CleanupError('select a valid local PR base ref')
    checkouts = _records(repo)
    try:
        base = _git(repo, 'rev-parse', '--verify', '--end-of-options', base_ref + '^{commit}').decode().strip()
    except stack_rebase.RebaseError:
        base = None
    roots = [_absolute(root).resolve(strict=True) for root in tasks_roots]
    registry = _registry(roots)
    digest, keep = _holds(holds)
    prs = _pr_heads(destination, reader)
    usage, active = worktree_usage.scan([row['path'] for row in checkouts])
    entries = [_describe(repo, row, base, registry, keep, prs, checkout=repo, measure_size=measure_size,
                         usage=usage, live=active.get(row['path'], []))
               for row in checkouts]
    return {'schema': cli.SCHEMA, 'source': SOURCE, 'repo': str(repo), 'base_ref': base_ref, 'base': base,
            'at': cli.now(), 'tasks_roots': [str(root) for root in roots], 'holds_sha256': digest,
            'destination': destination, 'disk_free_bytes': shutil.disk_usage(repo).free,
            'live_usage': usage, 'worktrees': entries}


def _identity(row):
    return {key: row[key] for key in ('path', 'head', 'branch', 'identity')}


def freeze(repo, base_ref, selected, out, *, holds, tasks_roots=(), destination=None, reader=None):
    if not selected or len(selected) > 64 or len(set(selected)) != len(selected):
        raise CleanupError('select one to 64 distinct, explicitly approved worktrees')
    snapshot = inventory(repo, base_ref, holds=holds, tasks_roots=tasks_roots, destination=destination, reader=reader)
    index = {row['path']: row for row in snapshot['worktrees']}
    if snapshot['base'] is None or snapshot['holds_sha256'] is None:
        raise CleanupError('reclamation requires a known base and an explicit usage snapshot')
    identities = []
    for selected_path in selected:
        path = str(_absolute(selected_path))
        row = index.get(path)
        if row is None or row['bucket'] != 'candidate':
            reason = ', '.join(row['reasons']) if row else 'not a registered worktree'
            raise CleanupError(f'cannot approve {path}: {reason}')
        identities.append(_identity(row))
    plan = _summary_path(out, snapshot['worktrees'])
    record = {key: snapshot[key] for key in ('schema', 'source', 'repo', 'base_ref', 'base', 'tasks_roots',
                                              'holds_sha256', 'destination', 'disk_free_bytes')}
    record['selected'] = identities
    if plan.exists() or plan.is_symlink():
        raise CleanupError('cleanup approval path must be unused')
    cli.write(plan, record)
    return {'plan': str(plan), 'sha256': cli.digest(plan), 'selected': identities}


def apply(plan, sha256, *, holds, out, reader=None):
    plan = _absolute(plan)
    if not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256):
        raise CleanupError('supply the exact approved cleanup plan digest')
    data = swarm._read(plan, sha256)
    if data.get('source') != SOURCE or not data.get('holds_sha256') or not data.get('base'):
        raise CleanupError('invalid worktree cleanup approval')
    repo = _absolute(data['repo']).resolve(strict=True)
    holds = _absolute(holds)
    path = _summary_path(out, _records(repo))
    if path in (plan.resolve(), holds.resolve()) or any(path.is_relative_to(Path(item['path'])) for item in data['selected']):
        raise CleanupError('cleanup receipt cannot overwrite an approval or lie inside a selected worktree')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    common = Path(_git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip())
    anchor = common / 'HEAD'
    with task_lock(anchor, lock_name='.iboard-worktree-cleanup.lock', reentrant=False):
        before = shutil.disk_usage(repo).free
        report = (swarm._read(path) if path.exists() or path.is_symlink() else
                  {'schema': cli.SCHEMA, 'source': SOURCE, 'plan_sha256': sha256, 'repo': str(repo),
                   'base': data['base'], 'free_before_bytes': before, 'removed': {}})
        if report.get('plan_sha256') != sha256:
            raise CleanupError('cleanup receipt identifies a different approval')
        report['status'] = 'running'
        report['blocked'] = {}
        report['live_usage'] = {}
        cli.write(path, report)
        try:
            for selected in data['selected']:
                if cli.digest(plan) != sha256:
                    raise CleanupError('cleanup approval changed')
                name = selected['path']
                estimate = None if name in report['removed'] else _size(Path(name))
                check = inventory(repo, data['base_ref'], holds=holds, tasks_roots=data['tasks_roots'],
                                  destination=data['destination'], reader=reader)
                if check['base'] != data['base']:
                    raise CleanupError('PR base changed after cleanup was approved')
                current = next((row for row in check['worktrees'] if row['path'] == name), None)
                if name in report['removed']:
                    if current or Path(name).exists():
                        raise CleanupError('a previously removed path is now occupied')
                    continue
                report['live_usage'][name] = {'observation': check['live_usage'],
                                              'matches': current['live_usage'] if current else []}
                if current is None or current['bucket'] != 'candidate' or _identity(current) != selected:
                    report['blocked'][name] = current['reasons'] if current and current['reasons'] else ['worktree changed']
                    cli.write(path, report)
                    continue
                try:
                    if (cli.digest(holds) != check['holds_sha256']
                            or _git(name, 'rev-parse', '--verify', 'HEAD').decode().strip() != selected['head']
                            or any(_status(name)[0].values())):
                        raise CleanupError('usage, HEAD, or worktree files changed during preflight')
                    usage, active = worktree_usage.scan([name])
                    report['live_usage'][name] = {'observation': usage, 'matches': active.get(name, [])}
                    if usage['status'] != 'available':
                        raise CleanupError(LIVE_UNKNOWN)
                    if active.get(name):
                        raise CleanupError(LIVE_HOLD)
                    # Git repeats its own dirty and locked checks. Preserve branches and avoid force/prune.
                    _git(repo, 'worktree', 'remove', '--', name, timeout=60)
                    if Path(name).exists() or any(row['path'] == name for row in _records(repo)):
                        raise CleanupError('Git did not confirm worktree removal')
                    report['removed'][name] = {'head': selected['head'], 'branch': selected['branch'],
                                               'estimated_bytes': estimate, 'at': cli.now()}
                    if selected['branch'] and _git(repo, 'rev-parse', '--verify', selected['branch']).decode().strip() != selected['head']:
                        raise CleanupError('the original branch changed while removing the worktree')
                except ERRORS as error:
                    report['blocked'][name] = [str(error)[:1000]]
                cli.write(path, report)
        except ERRORS as error:
            report['blocked']['approval'] = [str(error)[:1000]]
        report['free_after_bytes'] = shutil.disk_usage(repo).free
        report['observed_free_bytes_delta'] = report['free_after_bytes'] - report['free_before_bytes']
        report['status'] = 'blocked' if report['blocked'] else 'complete'
        cli.write(path, report)
    return {**report, 'report': str(path), 'sha256': cli.digest(path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('inventory', 'freeze'):
        command = sub.add_parser(action)
        command.add_argument('--repo', required=True)
        command.add_argument('--base-ref', default='origin/HEAD')
        command.add_argument('--holds', required=action == 'freeze')
        command.add_argument('--tasks-root', action='append', default=[], dest='tasks_roots')
        command.add_argument('--destination')
        if action == 'inventory':
            command.add_argument('--measure-size', action='store_true')
        else:
            command.add_argument('--select', action='append', required=True, dest='selected')
            command.add_argument('--out', required=True)
    remove = sub.add_parser('apply')
    for argument in ('plan', 'sha256', 'holds', 'out'):
        remove.add_argument('--' + argument, required=True)
    args = vars(parser.parse_args(argv))
    action = args.pop('action')
    try:
        result = {'inventory': inventory, 'freeze': freeze, 'apply': apply}[action](**args)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result.get('status') == 'blocked')


if __name__ == '__main__':
    raise SystemExit(main())
