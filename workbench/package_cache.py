"""Inspect and maintain exactly approved package-manager caches."""

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from workbench import cli, swarm, worktree_usage
from workbench.resource_cleanup import ERRORS, ResourceError, _parent_identity, _remove_cache, _stat, _tree
from workbench.task_lock import task_lock
from workbench.worktree_cleanup import _absolute


SOURCE = 'skills/poteto-mode/playbooks/worktree-cleanup.md#worktree-and-simulator-cleanup'
KIND = 'package-cache'
DOWNLOAD = re.compile(r'[a-f0-9]{64}--[^/]+')
MANAGERS = ('uv', 'pnpm', 'brew', 'yarn')
CACHE_SIGNATURE = b'Signature: 8a477f597d28d172789f06886806bc55'
LOCAL_ENV = {
    'COREPACK_ENABLE_NETWORK': '0',
    'HOMEBREW_NO_ANALYTICS': '1',
    'HOMEBREW_NO_AUTO_UPDATE': '1',
    'npm_config_manage_package_manager_versions': 'false',
    'pnpm_config_manage_package_manager_versions': 'false',
    'pnpm_config_update_notifier': 'false',
}


def _run(executable, *args, env=None, seconds=15):
    with tempfile.TemporaryDirectory(prefix='iboard-package-cache-') as directory:
        proc = subprocess.run([executable, *args], cwd=directory, env={**os.environ, **LOCAL_ENV, **(env or {})},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=seconds)
    if proc.returncode:
        raise ResourceError(f'{Path(executable).name} {args[0]} exited with status {proc.returncode}')
    return proc.stdout.strip(), proc.stderr.strip()


def _directory(value):
    path = _absolute(value)
    original = path
    for alias in ('var', 'tmp'):
        prefix = Path('/') / alias
        if path.is_relative_to(prefix) and prefix.is_symlink() and prefix.resolve() == Path('/private') / alias:
            path = prefix.resolve() / path.relative_to(prefix)
            break
    if path != path.resolve():
        raise ResourceError(f'cache directory is indirect: {path}')
    if len(path.parts) < 3 or path == Path.home().resolve():
        raise ResourceError(f'expected a dedicated cache directory: {original}')
    return path


def _brew_files(root):
    if root.resolve() != root or not root.is_dir():
        return []
    return [str(path) for path in sorted(root.iterdir())
            if DOWNLOAD.fullmatch(path.name) and not path.name.endswith('.incomplete')
            and stat.S_ISREG(path.lstat().st_mode)]


def _selected_download(root, select):
    path = _absolute(select)
    path = _directory(path.parent) / path.name
    if str(path) not in _brew_files(root):
        raise ResourceError('select a regular completed download reported by inventory')
    return path


def _native_tool(manager):
    path = shutil.which(manager)
    if path is None:
        raise ResourceError(f'{manager} is unavailable on PATH')
    path = Path(path).resolve(strict=True)
    return {'path': str(path), 'identity': _stat(path.stat())}


def _yarn_scope(executable, cache):
    env = {'YARN_IGNORE_PATH': '1', 'YARN_ENABLE_NETWORK': '0', 'YARN_ENABLE_TELEMETRY': '0'}
    version = _run(executable, '--version', env=env)[0]
    if not re.fullmatch(r'[1-9][0-9]*\.[0-9]+\.[0-9]+(?:[-+][\w.-]+)?', version):
        raise ResourceError('Yarn did not report a supported release version')
    if version.startswith('1.'):
        if cache is None:
            raise ResourceError('approve the explicit Yarn Classic cache root containing its version directories')
        root = Path(cache)
        if not (root / 'v6').is_dir() or any(not re.fullmatch(r'v[0-9]+', child.name)
                                             or child.is_symlink() or not child.is_dir()
                                             for child in root.iterdir()):
            raise ResourceError('Yarn Classic cache root must contain only Yarn version directories, including v6')
        return {'classic_cache': cache}, ['--no-default-rc', '--offline', '--cache-folder', cache, 'cache', 'clean'], env
    if cache is None:
        cache = str(_directory(json.loads(_run(executable, 'config', 'get', 'globalFolder', '--json', env=env)[0])))
    env.update(YARN_ENABLE_GLOBAL_CACHE='1', YARN_GLOBAL_FOLDER=cache)
    root = Path(cache) / 'cache'
    actual = json.loads(_run(executable, 'config', 'get', 'cacheFolder', '--json', env=env)[0])
    if _directory(actual) != root:
        raise ResourceError('Yarn did not bind to the selected global cache directory')
    return {'global_cache': str(root)}, ['cache', 'clean'], env


def inventory(manager, *, cache_dir=None, store_dir=None, select=None):
    if manager not in MANAGERS:
        raise ResourceError('unknown package manager')
    if (store_dir and manager != 'pnpm') or (select and manager != 'brew'):
        raise ResourceError('store-directory selection is for pnpm and download selection is for Homebrew')
    tool = _native_tool(manager)
    executable = tool['path']
    inputs = {'cache_dir': str(_directory(cache_dir)) if cache_dir else None,
              'store_dir': str(_directory(store_dir)) if store_dir else None,
              'select': str(_absolute(select)) if select else None}
    cache = inputs['cache_dir']
    env, argv, downloads = {}, [], None
    if manager == 'uv':
        if cache is None:
            cache = str(_directory(_run(executable, 'cache', 'dir')[0]))
        env = {'UV_NO_CACHE': 'false'}
        location = _run(executable, '--offline', '--no-config', 'cache', 'dir', '--cache-dir', cache, env=env)[0]
        if _directory(location) != Path(cache):
            raise ResourceError('uv did not bind to the selected cache directory')
        roots = {'cache': cache}
        argv = ['--offline', '--no-config', 'cache', 'prune', '--cache-dir', cache]
    elif manager == 'pnpm':
        if cache is None:
            configured = _run(executable, 'config', 'get', 'cache-dir')[0]
            if configured in ('', 'undefined'):
                raise ResourceError('provide the explicit pnpm metadata directory with --cache-dir')
            cache = str(_directory(configured))
        options = ['--store-dir', inputs['store_dir']] if store_dir else []
        env = {'npm_config_cache_dir': cache, 'pnpm_config_cache_dir': cache}
        if _run(executable, 'config', 'get', 'cache-dir', env=env)[0] != cache:
            raise ResourceError('pnpm did not bind to the selected metadata directory')
        store = _directory(_run(executable, 'store', 'path', *options, env=env)[0])
        base = Path(inputs['store_dir']) if store_dir else store.parent
        if store.parent != base or not re.fullmatch(r'v[0-9]+', store.name):
            raise ResourceError('pnpm failed to identify a versioned store within the selected directory')
        argv = ['store', 'prune', '--store-dir', str(base)]
        if _directory(_run(executable, 'store', 'path', *argv[2:], env=env)[0]) != store:
            raise ResourceError('pnpm store selection changed under explicit scope')
        roots = {'store': str(store), 'metadata': cache}
    elif manager == 'brew':
        if cache is not None:
            env = {'HOMEBREW_CACHE': cache}
        root = _directory(_run(executable, '--cache', env=env)[0]) / 'downloads'
        if cache is not None and root.parent != Path(cache):
            raise ResourceError('Homebrew did not bind to the selected cache directory')
        roots = {'downloads': str(root)}
        if select:
            roots = {'download': str(_selected_download(root, select))}
        else:
            downloads = _brew_files(root)
    else:
        roots, argv, env = _yarn_scope(executable, cache)
    return {'schema': cli.SCHEMA, 'source': SOURCE, 'kind': KIND, 'manager': manager, 'tool': tool, 'roots': roots,
            'downloads': downloads, 'argv': argv, 'env': env, 'inputs': inputs}


def _snapshot(roots):
    paths = [Path(path) for path in roots.values()]
    if any(left.is_relative_to(right) for left in paths for right in paths if left != right):
        raise ResourceError('package-cache roots must be separate')
    if len(set(paths)) != len(paths):
        raise ResourceError('package-cache roots must be distinct')
    result = {}
    for role, path in zip(roots, paths):
        info = path.lstat()
        expected = stat.S_ISREG if role == 'download' else stat.S_ISDIR
        if path.resolve() != path or not expected(info.st_mode):
            raise ResourceError('cache entry is indirect or has an unexpected type')
        if role == 'cache':
            marker = path / 'CACHEDIR.TAG'
            if marker.is_symlink() or not marker.is_file():
                raise ResourceError('uv cache directory is missing its cache tag')
            with marker.open('rb') as handle:
                if handle.readline(100).strip() != CACHE_SIGNATURE:
                    raise ResourceError('uv cache tag does not have the expected signature')
        if role == 'store':
            for name in ('files', 'index', 'links', 'tmp'):
                bucket = path / name
                if bucket.is_symlink() or (name in ('files', 'index') and bucket.is_dir()
                                          and any(child.is_symlink() for child in bucket.iterdir())):
                    raise ResourceError('pnpm store contains an indirect native storage bucket')
        parent = _parent_identity(path)
        fingerprint, size = _tree(path)
        if _stat(path.lstat()) != _stat(info) or _parent_identity(path) != parent:
            raise ResourceError('cache entry changed while measuring it')
        result[role] = {'identity': _stat(info), 'parent': parent, 'tree_sha256': fingerprint, 'bytes': size}
    return result


def _artifact(value, roots):
    path = _absolute(value)
    if path.is_symlink():
        raise ResourceError('package-cache evidence cannot be a symbolic link')
    path = path.resolve()
    if any(path.is_relative_to(root) for root in roots.values()):
        raise ResourceError('keep package-cache evidence outside the approved cache')
    return path


def freeze(manager, *, out, cache_dir=None, store_dir=None, select=None):
    if manager == 'brew' and not select:
        raise ResourceError('approve one exact Homebrew download from inventory')
    observed = inventory(manager, cache_dir=cache_dir, store_dir=store_dir, select=select)
    plan = _artifact(out, observed['roots'])
    if plan.exists():
        raise ResourceError('package-cache approval path must be unused')
    approval = {**observed, 'approval_path': str(plan), 'before': _snapshot(observed['roots'])}
    cli.write(plan, approval)
    return {'plan': str(plan), 'sha256': cli.digest(plan), 'roots': observed['roots']}


def _issue(data):
    if data['manager'] == 'brew':
        path = Path(data['roots']['download'])
        before = data['before']['download']
        _remove_cache(path, before['identity'], before['parent'])
        if _parent_identity(path) != before['parent'] or path.exists() or path.is_symlink():
            raise ResourceError('approved download is still present or its parent changed')
        return {'download': 0}
    _run(data['tool']['path'], *data['argv'], env=data['env'], seconds=120)
    if data['manager'] == 'yarn':
        role, value = next(iter(data['roots'].items()))
        path = Path(value)
        if _parent_identity(path) != data['before'][role]['parent'] or path.resolve() != path:
            raise ResourceError('Yarn cache scope changed during maintenance')
        if role == 'global_cache' and not path.exists():
            return {role: 0}
        if role == 'classic_cache':
            expected = {Path('v6'), Path('v6/.tmp')}
            if any(item.relative_to(path) not in expected or item.is_symlink() or not item.is_dir()
                   for item in path.rglob('*')):
                raise ResourceError('Yarn Classic cache still contains downloaded entries')
            return {role: _snapshot(data['roots'])[role]['bytes']}
        raise ResourceError('Yarn global cache still exists')
    after = _snapshot(data['roots'])
    for role, snapshot in after.items():
        before = data['before'][role]
        if snapshot['identity'][:3] != before['identity'][:3] or snapshot['parent'] != before['parent']:
            raise ResourceError('approved cache root or parent changed during maintenance')
    return {role: entry['bytes'] for role, entry in after.items()}


@contextmanager
def _cache_locks(data):
    parents = {Path(path).parent: data['before'][role]['parent'] for role, path in data['roots'].items()}
    with ExitStack() as stack:
        for parent, expected in sorted(parents.items()):
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            if [info.st_dev, info.st_ino] != expected:
                raise ResourceError('approved package-cache parent changed')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ResourceError('another cleanup is using the approved cache parent') from error
        yield


def _apply_new(plan, sha256, journal, data):
    observed = inventory(data['manager'], **data['inputs'])
    if any(observed[key] != data[key] for key in ('tool', 'roots', 'argv', 'env')):
        raise ResourceError('native tool or package-cache scope changed after approval')
    if data['manager'] != 'uv':
        paths = [str(Path(path).parent) if data['manager'] == 'brew' else path
                 for path in observed['roots'].values()]
        usage, matches = worktree_usage.scan(paths)
        if usage['status'] != 'available' or matches:
            raise ResourceError('cache usage is active or could not be verified')
    with _cache_locks(data):
        if _snapshot(observed['roots']) != data['before'] or cli.digest(plan) != sha256:
            raise ResourceError('approved package-cache contents or plan changed')
        volumes = {role: Path(path).parent for role, path in data['roots'].items()}
        report = {'schema': cli.SCHEMA, 'source': SOURCE, 'kind': KIND, 'plan_sha256': sha256, 'status': 'pending',
                  'manager': data['manager'], 'tool': data['tool'], 'roots': data['roots'], 'argv': data['argv'],
                  'started_at': cli.now(),
                  'bytes_before': {role: entry['bytes'] for role, entry in data['before'].items()},
                  'disk_free_before': {role: shutil.disk_usage(path).free for role, path in volumes.items()}}
        cli.write(journal, report)
        try:
            after = _issue(data)
            report.update(status='complete', bytes_after=after, completed_at=cli.now(),
                          observed_cache_bytes_delta=sum(after.values()) - sum(report['bytes_before'].values()),
                          disk_free_after={role: shutil.disk_usage(path).free for role, path in volumes.items()})
        except ERRORS as error:
            report['detail'] = str(error)[:1000]
        cli.write(journal, report)
    return report


def apply(plan, sha256, *, out):
    if not re.fullmatch(r'[a-f0-9]{64}', sha256):
        raise ResourceError('provide the exact approved package-cache plan digest')
    plan = _artifact(plan, {})
    data = swarm._read(plan, sha256)
    if (data.get('source'), data.get('kind'), data.get('approval_path')) != (SOURCE, KIND, str(plan)):
        raise ResourceError('use the original package-cache approval path')
    out = _artifact(out, data['roots'])
    journal = plan.with_name('.' + plan.name + '.package-cache.json')
    if out in (plan, journal):
        raise ResourceError('use a distinct receipt path')
    with task_lock(plan, lock_name='.iboard-package-cache.lock', reentrant=False):
        if out.exists() and swarm._read(out).get('plan_sha256') != sha256:
            raise ResourceError('receipt belongs to a different package-cache approval')
        if journal.exists() or journal.is_symlink():
            report = swarm._read(journal)
            if report['plan_sha256'] != sha256:
                raise ResourceError('journal belongs to a different package-cache approval')
        else:
            report = _apply_new(plan, sha256, journal, data)
        cli.write(out, report)
    return {**report, 'report': str(out), 'sha256': cli.digest(out)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('inventory', 'freeze'):
        command = sub.add_parser(action)
        command.add_argument('--manager', choices=MANAGERS, required=True)
        for option in ('cache-dir', 'store-dir', 'select'):
            command.add_argument('--' + option)
        if action == 'freeze':
            command.add_argument('--out', required=True)
    command = sub.add_parser('apply')
    for option in ('plan', 'sha256', 'out'):
        command.add_argument('--' + option, required=True)
    args = vars(parser.parse_args(argv))
    action = args.pop('action')
    try:
        result = {'inventory': inventory, 'freeze': freeze, 'apply': apply}[action](**args)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result.get('status') == 'pending')


if __name__ == '__main__':
    raise SystemExit(main())
