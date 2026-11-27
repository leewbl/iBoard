"""Install and verify the dormant iBoard Benny deployment in one Git project."""

import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from uuid import uuid4

from workbench import cli, prompt_profiles


PACK = Path('.iboard/automations/benny')
MANIFEST = 'manifest.json'
INSTALL_ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 32_000
SHA = re.compile(r'[0-9a-f]{40}')
DEPENDENCIES = {
    'why': ('skills/investigate/SKILL.md', 'workbench.investigate'),
    'tdd': ('skills/tdd/SKILL.md', 'workbench.tdd'),
    'unslop': ('skills/unslop/SKILL.md', 'workbench.technical_writing'),
}
PRINCIPLES = (
    'separate-before-serializing-shared-state',
    'minimize-reader-load',
    'guard-the-context-window',
    'sequence-verifiable-units',
    'fix-root-causes',
    'prove-it-works',
)
ROLES = {
    'triage': {
        'instruction': 'skills/triage-issue-reports/SKILL.md',
        'event_template': 'templates/triage-event.json',
        'trigger': 'new-top-level-report',
        'entrypoint': 'workbench.benny_triage',
    },
    'reproduce': {
        'instruction': 'skills/reproduce-and-fix-issues/SKILL.md',
        'event_template': 'templates/reproduce-event.json',
        'trigger': 'trusted-triage-reply',
        'entrypoint': 'workbench.benny_reproduce',
    },
}


class DeployError(ValueError):
    pass


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _json(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()


def _bundle():
    files = {
        'FOR_AGENTS.md': (
            '# Benny for iBoard\n\n'
            'Use [setup](skills/setup-benny/SKILL.md) to preflight the installed package. '
            'Use the two committed role instructions for triage and reproduction. '
            'Keep authorization, feature maps, routing, and credentials outside this package. '
            'Installation is dormant until the operator configures an event receiver and worker.\n'
        ).encode(),
        'skills/setup-benny/SKILL.md': (
            '# Set up the iBoard deployment\n\n'
            'Commit the package before selecting it for a receiver or a worker. '
            'Run `python3 -m workbench.benny_deploy verify --repo .` from a new checkout. '
            'Retain the confirmed revision and manifest digest. Configure the authorized Slack '
            'workspace, app, source scope and owner-only credentials separately. '
            'Choose an approved control adapter, feature map, and independent probe for each '
            'repair. Enable the receiver and worker only with those capabilities configured.\n'
        ).encode(),
        ROLES['triage']['instruction']: (
            '# Triage one selected report\n\n'
            'Keep the workspace, channel and root message from the authorized event. '
            'Read the original thread through the coordinator. Consult the configured tracker '
            'and source, classify using evidence, and prefer a confirmed existing item or repair. '
            'Retain an assessment and recommendation. Only the approved handoff can write '
            'the tracker or reply to the original thread.\n'
        ).encode(),
        ROLES['reproduce']['instruction']: (
            '# Reproduce a trusted report\n\n'
            'Confirm the verdict on the selected source thread and check ownership. '
            'Verify an existing repair when one is available. For a new repair, hold the '
            'approved baseline and acceptance criteria, use the configured offline adapter '
            'and independent probe, and retain the original observations. Keep the task '
            'and repair bounded. The dedicated delivery gate may open a draft after '
            'independent verification and an authorization recheck.\n'
        ).encode(),
        'references/native-tools.md': (
            '# iBoard tools\n\n'
            '- Report snapshots and existing fixes use `workbench.benny`.\n'
            '- Recommendations and approved replies use `workbench.benny_triage` and `workbench.benny_handoff`.\n'
            '- Offline verification and draft delivery use `workbench.benny_reproduce`, '
            '`workbench.benny_repair` and `workbench.benny_draft`.\n'
            '- The original Git revision and pack digest identify the ready deployment.\n'
        ).encode(),
    }
    for name, role in ROLES.items():
        files[role['event_template']] = _json({
            'role': name, 'trigger': role['trigger'], 'instruction': role['instruction'],
            'coordinates': ['workspace', 'channel', 'message_ts', 'root'],
        })
    files[MANIFEST] = _json({
        'schema': cli.SCHEMA, 'kind': 'iboard-benny-deployment', 'pstack_commit': prompt_profiles.PSTACK_COMMIT,
        'activation': 'operator-configured', 'roles': ROLES,
        'files': {name: _digest(content) for name, content in files.items()},
    })
    return files


def _git(repo, *args):
    run = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, timeout=10, check=False)
    if run.returncode:
        raise DeployError('Git could not locate the target checkout or committed Benny package')
    return run.stdout


def _root(repo):
    supplied = Path(repo).expanduser().absolute()
    for ancestor in (supplied, *supplied.parents):
        if ancestor.is_symlink():
            platform_alias = {Path('/tmp'): Path('/private/tmp'), Path('/var'): Path('/private/var')}
            if ancestor not in platform_alias or ancestor.resolve() != platform_alias[ancestor]:
                raise DeployError('the target repository cannot use a symbolic path')
    path = supplied.resolve(strict=True)
    top = _git(path, 'rev-parse', '--show-toplevel').decode().strip()
    if Path(top).resolve() != path:
        raise DeployError('choose the root of the target Git checkout')
    return path


@contextmanager
def _parent(repo, parts, *, create=False):
    """Open each package directory relative to the preceding descriptor."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        folder = os.open(repo, flags)
        stack.callback(os.close, folder)
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=folder)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, flags, dir_fd=folder)
            except FileNotFoundError:
                if create:
                    raise
                yield None
                return
            except OSError as error:
                raise DeployError(f'package directory is not a real directory: {part}') from error
            stack.callback(os.close, child)
            folder = child
        yield folder


def _read(folder, name):
    if folder is None:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DeployError(f'package file is not a safe regular file: {name}') from error
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise DeployError(f'package file is not a regular file: {name}')
        raw = stream.read(MAX_BYTES + 1)
    return raw


def _plan(repo, bundle):
    result = {'create': [], 'unchanged': [], 'conflicts': []}
    for name, content in bundle.items():
        path = PACK / name
        with _parent(repo, path.parts[:-1]) as folder:
            old = _read(folder, path.name)
        result['create' if old is None else 'unchanged' if old == content else 'conflicts'].append(str(path))
    return result


def preview(repo):
    repo = _root(repo)
    plan = _plan(repo, _bundle())
    return {'repo': str(repo), 'status': 'conflict' if plan['conflicts'] else 'ready-to-install', **plan}


def _publish(folder, name, content):
    if _read(folder, name) == content:
        return
    temporary = '.benny-' + uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=folder)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
        except FileExistsError:
            if _read(folder, name) != content:
                raise DeployError(f'package file appeared or changed during installation: {name}')
    finally:
        os.unlink(temporary, dir_fd=folder)


def install(repo):
    repo = _root(repo)
    bundle = _bundle()
    plan = _plan(repo, bundle)
    if plan['conflicts']:
        raise DeployError('review conflicting managed files before installation: ' + ', '.join(plan['conflicts']))
    for name, content in bundle.items():
        path = PACK / name
        if str(path) in plan['create']:
            with _parent(repo, path.parts[:-1], create=True) as folder:
                _publish(folder, path.name, content)
    installed = _plan(repo, bundle)
    if installed['create'] or installed['conflicts']:
        raise DeployError('Benny package changed while it was being installed')
    return {'repo': str(repo), 'status': 'installed', **plan,
            'manifest_sha256': _digest(bundle[MANIFEST])}


def _blob(repo, head, path):
    raw = _git(repo, 'ls-tree', '-z', head, '--', str(path))
    match = re.fullmatch(rb'100644 blob ([0-9a-f]{40})\t' + re.escape(str(path).encode()) + rb'\x00', raw)
    if not match:
        raise DeployError(f'commit the regular Benny package file: {path}')
    oid = match[1].decode()
    if int(_git(repo, 'cat-file', '-s', oid)) > MAX_BYTES:
        raise DeployError(f'committed Benny package file is too large: {path}')
    return _git(repo, 'cat-file', 'blob', oid)


def _dependency(module):
    spec = importlib.util.find_spec(module)
    if not spec or not spec.origin:
        raise DeployError(f'iBoard executable dependency is unavailable: {module}')
    path = Path(spec.origin).resolve()
    if not path.is_relative_to(INSTALL_ROOT) or not path.is_file():
        raise DeployError(f'iBoard dependency did not resolve from its installed package: {module}')
    return _digest(path.read_bytes())


def _dependencies():
    result = {}
    for name, (guide, module) in DEPENDENCIES.items():
        path = INSTALL_ROOT / guide
        if not path.is_file() or not path.resolve().is_relative_to(INSTALL_ROOT):
            raise DeployError(f'iBoard project dependency has no installed skill guide: {name}')
        result[name] = {'guide': guide, 'sha256': _digest(path.read_bytes()), 'module': module,
                        'module_sha256': _dependency(module)}
    for principle in PRINCIPLES:
        source = f'skills/principle-{principle}/SKILL.md'
        stages = prompt_profiles.source_stages(source)
        if not stages:
            raise DeployError(f'iBoard project dependency has no installed guidance: {principle}')
        result[principle] = {'source': source, 'stages': [list(stage) for stage in stages]}
    return result


def verify(repo, revision='HEAD'):
    repo = _root(repo)
    if revision != 'HEAD' and (not isinstance(revision, str) or not SHA.fullmatch(revision)):
        raise DeployError('select HEAD or a full Git commit SHA')
    head = _git(repo, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    if not SHA.fullmatch(head) or revision not in ('HEAD', head):
        raise DeployError('Benny readiness must refer to an exact commit')
    bundle = _bundle()
    for name, expected in bundle.items():
        path = PACK / name
        if _blob(repo, head, path) != expected:
            raise DeployError(f'committed Benny package differs from the installed revision: {path}')
    dependencies = _dependencies()
    roles = {name: {**role, 'module_sha256': _dependency(role['entrypoint'])} for name, role in ROLES.items()}
    if revision == 'HEAD' and _git(repo, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip() != head:
        raise DeployError('target HEAD changed during Benny verification')
    return {'repo': str(repo), 'revision': head, 'manifest_sha256': _digest(bundle[MANIFEST]),
            'status': 'ready', 'roles': roles, 'dependencies': dependencies,
            'pstack_commit': prompt_profiles.PSTACK_COMMIT}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('preview', 'install', 'verify'):
        verb = commands.add_parser(command)
        verb.add_argument('--repo', required=True)
        if command == 'verify':
            verb.add_argument('--revision', default='HEAD')
    options = vars(parser.parse_args(argv))
    action = options.pop('command')
    try:
        result = {'preview': preview, 'install': install, 'verify': verify}[action](**options)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return int(result['status'] == 'conflict')


if __name__ == '__main__':
    raise SystemExit(main())
