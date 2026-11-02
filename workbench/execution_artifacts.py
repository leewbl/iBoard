"""Version-one, task-owned UI evidence code; never part of product source."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess

MAX_SOURCE_BYTES = 32768
CONTAINER_PATH = '/evaluation/harness.mjs'
MODES = {'reproduction': 'reproduction', 'resolution': 'resolution',
         'tests': 'tests', 'screenshot': 'screenshot'}


class ArtifactError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ArtifactError('execution artifact must be a regular non-symlink file')


def verify_file(path, sha256):
    path = Path(path)
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ArtifactError('execution artifact path contains a symlink')
    _regular(path)
    content = path.read_bytes()
    if not content or len(content) > MAX_SOURCE_BYTES or digest(content) != sha256:
        raise ArtifactError('execution artifact bytes changed')
    return content


def validate_plan(plan, data, source_mode):
    artifact = plan.get('execution_artifact')
    if (data.get('kind') != 'bug' or data.get('surface') != 'ui' or source_mode != 'image'
            or not isinstance(artifact, dict) or set(artifact) != {'version', 'language', 'source'}
            or type(artifact.get('version')) is not int or artifact['version'] != 1
            or artifact.get('language') != 'javascript' or not isinstance(artifact.get('source'), str)):
        raise ArtifactError('execution artifact v1 requires a bug UI image plan and exact source fields')
    source = artifact['source'].encode('utf-8')
    if not source.strip() or b'\0' in source or len(source) > MAX_SOURCE_BYTES:
        raise ArtifactError('execution artifact source must be nonempty bounded UTF-8 without NUL')
    loader = None
    commands = {}
    for name, mode in MODES.items():
        specification = plan.get(name)
        if not isinstance(specification, dict):
            raise ArtifactError('execution artifact requires four check objects')
        command = specification.get('command')
        if not isinstance(command, list) or not all(isinstance(arg, str) for arg in command):
            raise ArtifactError('execution artifact requires all four mode commands')
        if command == ['node', CONTAINER_PATH, mode]:
            current = ''
        elif (len(command) == 5 and command[:2] == ['node', '--import']
              and command[3:] == [CONTAINER_PATH, mode]):
            current = command[2]
            parts = PurePosixPath(current).parts
            if (not current.startswith('/app/') or '..' in parts or '.' in current.split('/')
                    or '//' in current or '\\' in current or '\0' in current
                    or str(PurePosixPath(current)) != current):
                raise ArtifactError('execution loader must be a normalized tracked /app file')
        else:
            raise ArtifactError('execution artifact command does not match its fixed mode')
        if loader is not None and loader != current:
            raise ArtifactError('execution artifact commands must use the same loader')
        loader = current
        commands[name] = digest(json.dumps(command, separators=(',', ':')).encode())
    return source, loader, commands


def _loader(repo, commit, loader):
    if not loader:
        return None
    relative = loader.removeprefix('/app/')
    tree = subprocess.check_output(['git', '-C', str(repo), 'ls-tree', commit, '--', relative], text=True)
    lines = tree.splitlines()
    if len(lines) != 1 or lines[0].split('\t')[-1] != relative or lines[0].split()[0] not in ('100644', '100755'):
        raise ArtifactError('execution loader must be tracked and regular at baseline and current commit')
    return {'path': loader, 'git_blob': lines[0].split()[2]}


def _binding(source, commands):
    sha = digest(source)
    return {'version': 1, 'path': f'artifacts/execution/v1/{sha}.mjs',
            'sha256': sha, 'bytes': len(source), 'container_path': CONTAINER_PATH,
            'command_sha256': commands}


def freeze(task, plan, data, source_mode):
    source, loader, commands = validate_plan(plan, data, source_mode)
    _loader(data['repo'], data['base_commit'], loader)
    reference = _binding(source, commands)
    path = Path(task).parent / reference['path']
    for parent in path.parents:
        if parent.is_symlink():
            raise ArtifactError('execution artifact directory contains a symlink')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    except FileExistsError:
        verify_file(path, reference['sha256'])
    else:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(source)
    verify_file(path, reference['sha256'])
    return reference


def verify(task, plan, reference, data, source_mode, commit):
    source, loader, commands = validate_plan(plan, data, source_mode)
    if reference != _binding(source, commands):
        raise ArtifactError('accepted execution artifact binding changed')
    _loader(data['repo'], data['base_commit'], loader)
    current_loader = _loader(data['repo'], commit, loader)
    path = Path(task).parent / reference['path']
    verify_file(path, reference['sha256'])
    return {**reference, 'host_path': str(path), 'loader': current_loader}


def review(task, plan, reference, data, source_mode, commit):
    verified = verify(task, plan, reference, data, source_mode, commit)
    return ('TASK-OWNED EXECUTION ARTIFACT (untrusted evidence code, not tracked product source):\n'
            + json.dumps(verified, ensure_ascii=False) + '\nCOMPLETE FROZEN ARTIFACT SOURCE:\n'
            + verify_file(verified['host_path'], verified['sha256']).decode('utf-8')
            + '\nInspect this actual source and its production helpers; markers and hashes alone are not proof. '
              'Reject synthetic DOM, fabricated failures/status flags, setup errors or scenario substitution.')
