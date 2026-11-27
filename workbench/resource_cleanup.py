"""Inventory and reclaim individually approved simulator and developer cache resources."""

import argparse
from hashlib import sha256 as digest_bytes
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

from workbench import cli, simulator_resources, swarm, worktree_usage
from workbench.task_lock import task_lock
from workbench.worktree_cleanup import _absolute


SOURCE = 'skills/poteto-mode/playbooks/worktree-cleanup.md#worktree-and-simulator-cleanup'
CACHE_ROOTS = (
    ('xcode-derived-data', 'Library/Developer/Xcode/DerivedData', True),
    ('xcode-device-support', 'Library/Developer/Xcode/iOS DeviceSupport', True),
    ('cursor-snapshot', 'Library/Application Support/Cursor/snapshots/roots', True),
    ('cursor-state-backup', 'Library/Application Support/Cursor/state.vscdb.backup', False),
    ('cursor-state-backup', 'Library/Application Support/Cursor/User/globalStorage/state.vscdb.backup', False),
)
MAX_ITEMS = 300_000
ERRORS = (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, cli.WorkbenchError)


class ResourceError(ValueError):
    pass


def _stat(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _parent_identity(path):
    parent = path.parent
    if parent.resolve() != parent:
        raise ResourceError('cache parent path is indirect')
    info = parent.stat()
    return [info.st_dev, info.st_ino]


def _tree(path):
    """Bind a selection to its entry metadata without reading file contents."""
    root = path.lstat()
    queue, checked, blocks, digest = [path], 0, 0, digest_bytes()
    while queue:
        current = queue.pop()
        info = current.lstat()
        if info.st_dev != root.st_dev:
            raise ResourceError('cache contains a mounted filesystem')
        digest.update(os.fsencode(current.relative_to(path)) + b'\0' + str(_stat(info)).encode() + b'\0')
        checked += 1
        if checked > MAX_ITEMS:
            raise ResourceError('cache entry exceeded the snapshot limit')
        blocks += info.st_blocks
        if stat.S_ISDIR(info.st_mode):
            remaining = MAX_ITEMS - checked - len(queue)
            with os.scandir(current) as entries:
                children = []
                for entry in entries:
                    if len(children) >= remaining:
                        raise ResourceError('cache entry exceeded the snapshot limit')
                    children.append(entry.name)
            queue.extend(current / name for name in sorted(children, reverse=True))
            if _stat(current.lstat()) != _stat(info):
                raise ResourceError('cache directory changed while listing it')
    if _stat(path.lstat()) != _stat(root):
        raise ResourceError('cache root changed during its snapshot')
    return digest.hexdigest(), blocks * 512


def _cache_inventory(home):
    sources, rows = [], []
    for kind, relative, children in CACHE_ROOTS:
        root = home / relative
        source = {'kind': kind, 'root': str(root), 'status': 'missing'}
        sources.append(source)
        try:
            info = root.lstat()
            expected = stat.S_ISDIR if children else stat.S_ISREG
            if root.resolve() != root or not expected(info.st_mode):
                raise ResourceError('cache root is indirect or has an unexpected type')
            if children:
                with os.scandir(root) as entries:
                    paths = sorted(Path(entry.path) for entry in entries if not entry.name.startswith('.'))
            else:
                paths = [root]
        except FileNotFoundError:
            continue
        except (OSError, ResourceError) as error:
            source.update(status='unavailable', detail=str(error)[:240])
            continue
        source['status'] = 'available'
        for path in paths:
            row = {'id': str(path), 'path': str(path), 'kind': kind, 'identity': None,
                   'estimated_bytes': None, 'reasons': []}
            try:
                current = path.lstat()
                if path.resolve() != path or not expected(current.st_mode) or current.st_dev != info.st_dev:
                    raise ResourceError('cache entry is indirect, mounted, or has an unexpected type')
                row['identity'] = _stat(current)
            except (OSError, ResourceError) as error:
                row['reasons'].append(str(error)[:240])
            rows.append(row)
    return sources, rows


def _home(value):
    return _absolute(value if value is not None else Path.home()).resolve(strict=True)


def _sim_available(snapshot, key):
    collection = key.split(':', 2)[1]
    return snapshot.get('collections', {}).get(collection, snapshot['status']) == 'available'


def _sim_tool(snapshot):
    return snapshot.get('tool_identity', snapshot['tool'])


def inventory(*, home=None, simulator=None, measure_size=False):
    home = _home(home)
    sources, rows = _cache_inventory(home)
    live, matches = worktree_usage.scan([row['path'] for row in rows]) if rows else ({'status': 'not-needed'}, {})
    for row in rows:
        row['live_usage'] = matches.get(row['path'], [])
        if row['live_usage']:
            row['reasons'].append('cache entry is in use')
        if live['status'] != 'available':
            row['reasons'].append('live cache usage could not be verified')
        if measure_size and row['identity'] is not None and not row['reasons']:
            try:
                _, row['estimated_bytes'] = _tree(Path(row['path']))
            except ERRORS as error:
                row['reasons'].append(str(error)[:240])
    sim = (simulator if simulator is not None else simulator_resources.Simulator()).inventory()
    for row in sim['resources']:
        if not _sim_available(sim, row['id']):
            row['reasons'].append('complete simulator usage could not be verified')
        rows.append(row)
    if len({row['id'] for row in rows}) != len(rows):
        raise ResourceError('ambiguous registered resource identities')
    return {'schema': cli.SCHEMA, 'source': SOURCE, 'home': str(home), 'at': cli.now(),
            'disk_free_bytes': shutil.disk_usage(home).free, 'cache_sources': sources, 'live_usage': live,
            'simulator': {key: value for key, value in sim.items() if key != 'resources'}, 'resources': rows}


def _selected(row):
    return {field: row[field] for field in ('id', 'kind', 'path', 'identity')}


def _artifact(path, home):
    target = _absolute(path)
    if target.is_symlink():
        raise ResourceError('resource cleanup artifact must not be a symbolic link')
    target = target.resolve()
    if any(target.is_relative_to(home / relative) for _, relative, _ in CACHE_ROOTS):
        raise ResourceError('keep resource cleanup artifacts outside cache roots')
    return target


def freeze(selected, out, *, home=None, simulator=None):
    if not selected or len(selected) > 64 or len(set(selected)) != len(selected):
        raise ResourceError('select one to 64 exact resource IDs')
    home = _home(home)
    observed = inventory(home=home, simulator=simulator)
    rows = {row['id']: row for row in observed['resources']}
    approved = []
    for key in selected:
        row = rows.get(key)
        if not row or row['reasons']:
            raise ResourceError(f'cannot approve {key}: ' + ('; '.join(row['reasons']) if row else 'unknown resource'))
        entry = {**_selected(row), 'estimated_bytes': row['estimated_bytes']}
        if entry['path']:
            path = Path(entry['path'])
            entry['parent_identity'] = _parent_identity(path)
            entry['tree_sha256'], entry['estimated_bytes'] = _tree(path)
            if _stat(path.lstat()) != entry['identity'] or _parent_identity(path) != entry['parent_identity']:
                raise ResourceError('cache entry changed while approving it')
        approved.append(entry)
    path = _artifact(out, home)
    if path.exists():
        raise ResourceError('resource cleanup approval path must be unused')
    record = {'schema': cli.SCHEMA, 'source': SOURCE, 'home': str(home), 'approval_path': str(path), 'approved': approved,
              'simulator_tool': _sim_tool(observed['simulator']) if any(not row['path'] for row in approved) else None}
    cli.write(path, record)
    return {'plan': str(path), 'sha256': cli.digest(path), 'approved': approved}


def _same(entry, current):
    return _selected(current) == {key: entry[key] for key in ('id', 'kind', 'path', 'identity')}


def _validate_cache(entry):
    path = Path(entry['path'])
    usage, matched = worktree_usage.scan([str(path)])
    if usage['status'] != 'available' or matched:
        raise ResourceError('cache entry is in use or usage is unknown during preflight')
    if _stat(path.lstat()) != entry['identity'] or _tree(path)[0] != entry['tree_sha256']:
        raise ResourceError('cache tree changed after approval')


def _remove_cache(path, expected, parent):
    if path.resolve() != path or _stat(path.lstat()) != expected or _parent_identity(path) != parent:
        raise ResourceError('cache path changed before removal')
    if path.is_dir():
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ResourceError('directory removal has no symlink safeguards on this Python runtime')
        shutil.rmtree(path)
    else:
        path.unlink()


def _present_cache(path):
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return False
    return True


def apply(plan, sha256, *, out, home=None, simulator=None):
    home = _home(home)
    plan, receipt = _artifact(plan, home), _artifact(out, home)
    journal = plan.with_name('.' + plan.name + '.cleanup-state.json')
    if receipt in (plan, journal) or not isinstance(sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', sha256):
        raise ResourceError('provide a distinct receipt path and the exact approved plan digest')
    data = swarm._read(plan, sha256)
    if data['source'] != SOURCE or data['home'] != str(home) or not data.get('approved'):
        raise ResourceError('resource cleanup approval is invalid for this home')
    if data.get('approval_path') != str(plan):
        raise ResourceError('use the original approval path for its cleanup journal')
    sim = simulator if simulator is not None else simulator_resources.Simulator()
    with task_lock(plan, lock_name='.iboard-resource-cleanup.lock', reentrant=False):
        if receipt.exists() and swarm._read(receipt).get('plan_sha256') != sha256:
            raise ResourceError('receipt belongs to a different approval')
        report = (swarm._read(journal) if journal.exists() or journal.is_symlink() else
                  {'schema': cli.SCHEMA, 'source': SOURCE, 'plan_sha256': sha256, 'removed': {}, 'pending': {},
                   'free_before_bytes': shutil.disk_usage(home).free})
        if report['plan_sha256'] != sha256:
            raise ResourceError('cleanup state belongs to a different approval')
        report['blocked'] = {}
        for entry in data['approved']:
            key = entry['id']
            try:
                if cli.digest(plan) != sha256:
                    raise ResourceError('resource cleanup approval changed')
                observed = inventory(home=home, simulator=sim)
                current = next((row for row in observed['resources'] if row['id'] == key), None)
                if not entry['path'] and (_sim_tool(observed['simulator']) != data['simulator_tool'] or
                                          not _sim_available(observed['simulator'], key)):
                    raise ResourceError('approved simulator tool or inventory could not be verified')
                if entry['path'] and _parent_identity(Path(entry['path'])) != entry['parent_identity']:
                    raise ResourceError('cache parent changed after approval')
                present = current is not None or bool(entry['path'] and _present_cache(entry['path']))
                if key in report['removed']:
                    if present:
                        raise ResourceError('previously removed resource is now present again')
                    continue
                if key in report['pending']:
                    if present:
                        continue  # Observe prior issuance without issuing another removal.
                else:
                    if not current or current['reasons'] or not _same(entry, current):
                        raise ResourceError('resource changed or is in use after approval')
                    if entry['path']:
                        _validate_cache(entry)
                    report['pending'][key] = {'issued_at': cli.now()}
                    cli.write(journal, report)
                    if entry['path']:
                        path = Path(entry['path'])
                        _remove_cache(path, entry['identity'], entry['parent_identity'])
                        if _parent_identity(path) != entry['parent_identity'] or _present_cache(path):
                            raise ResourceError('cache path is still present after removal')
                    else:
                        sim.remove(current)
                        after = sim.inventory()
                        if not _sim_available(after, key) or _sim_tool(after) != data['simulator_tool']:
                            raise ResourceError('simulator removal outcome could not be verified')
                        if any(row['id'] == key for row in after['resources']):
                            continue
                report['pending'].pop(key)
                report['removed'][key] = {'kind': entry['kind'], 'estimated_bytes': entry['estimated_bytes'], 'at': cli.now()}
            except ERRORS as error:
                report['blocked'][key] = str(error)[:1000]
            cli.write(journal, report)
        report['free_after_bytes'] = shutil.disk_usage(home).free
        report['observed_free_bytes_delta'] = report['free_after_bytes'] - report['free_before_bytes']
        report['status'] = 'blocked' if report['blocked'] else 'pending' if report['pending'] else 'complete'
        cli.write(journal, report)
        cli.write(receipt, report)
    return {**report, 'report': str(receipt), 'sha256': cli.digest(receipt)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('inventory').add_argument('--measure-size', action='store_true')
    approve = sub.add_parser('freeze')
    approve.add_argument('--select', dest='selected', action='append', required=True)
    approve.add_argument('--out', required=True)
    action = sub.add_parser('apply')
    for name in ('plan', 'sha256', 'out'):
        action.add_argument('--' + name, required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop('action')
    try:
        result = {'inventory': inventory, 'freeze': freeze, 'apply': apply}[command](**args)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result.get('status') in ('blocked', 'pending'))


if __name__ == '__main__':
    raise SystemExit(main())
