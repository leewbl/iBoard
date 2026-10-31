"""Restore a missing input stage for a fully recorded adoption.

Incomplete Git setup is deliberately rejected and preserved for inspection.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import stat
import uuid

from workbench import autopilot, cli, delivery
from workbench.task_lock import task_lock


class RepairError(Exception):
    pass


def _regular(path):
    if path.is_symlink() or not path.is_file():
        raise RepairError(f'unsafe or missing {path.name}')


def _under_trusted_root(path, root):
    # The configured root itself and its ancestors are trusted and normalized;
    # symlinks beneath it are not. This handles macOS /tmp -> /private/tmp.
    path = Path(path).absolute()
    root = Path(root).absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as e:
        raise RepairError('path outside trusted root') from e
    actual = root.resolve(strict=True)
    for component in relative.parts:
        if component in ('.', '..'):
            raise RepairError('invalid path component')
        actual = actual / component
        if actual.is_symlink():
            raise RepairError('symbolic link in path')
    return actual


def _common_dir(repo):
    value=Path(cli.git(repo, 'rev-parse', '--git-common-dir'))
    return (repo / value).resolve(strict=True) if not value.is_absolute() else value.resolve(strict=True)


def _same_directory(fd, path):
    try:
        expected=os.fstat(fd)
        actual=os.stat(path, follow_symlinks=False)
    except OSError as e:
        raise RepairError('task directory unavailable') from e
    if not stat.S_ISDIR(actual.st_mode) or (expected.st_dev,expected.st_ino)!=(actual.st_dev,actual.st_ino):
        raise RepairError('task directory changed')


def _expected(task, data):
    if data.get('status') != 'paused' or not isinstance(data.get('adoption'), dict):
        raise RepairError('only a paused, fully recorded adoption can be repaired')
    if (task.parent/'autopilot.json').exists() or (task.parent/'autopilot.json').is_symlink():
        raise RepairError('workflow already started')
    if (task.parent/'inputs').exists() or (task.parent/'inputs').is_symlink():
        raise RepairError('unexpected input attachments')
    info = data['adoption']
    image, pr_repo, memory = info.get('image'), info.get('pr_repo'), info.get('memory_mb')
    if not isinstance(image,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',image):
        raise RepairError('invalid image identity')
    if not isinstance(pr_repo,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',pr_repo):
        raise RepairError('invalid PR destination')
    if type(memory) is not int or not 128 <= memory <= 6144:
        raise RepairError('invalid isolation memory')
    request, base, snapshot = (task.parent / n for n in ('issue.txt','input-base.txt','legacy-task.json'))
    for p in (task, request, base, snapshot): _regular(p)
    try:
        autopilot._validate_adoption(task, data, request, image, pr_repo, 'image', None, True, memory)
    except (autopilot.AutopilotError, cli.WorkbenchError) as e:
        raise RepairError('adoption identity changed') from e
    if cli.digest(base) != info['request_digest']:
        raise RepairError('original input snapshot changed')
    if data.get('ci_skip_policy') is not None and not delivery._recorded_skip_policy(data):
        raise RepairError('CI policy evidence changed')
    repo = _under_trusted_root(data['repo'], Path(data['repo']).parent)
    if repo.is_symlink() or not repo.is_dir() or repo.resolve() != repo:
        raise RepairError('unsafe workspace path')
    if not autopilot._clean(repo) or cli.sha(repo, 'HEAD') != data['base_commit']:
        raise RepairError('workspace must be clean at the recorded baseline')
    branch = cli.git(repo, 'symbolic-ref', '--short', 'HEAD')
    if branch != f"workbench/{data['id']}":
        raise RepairError('workspace branch changed')
    candidate=info.get('candidate_commit')
    if not isinstance(candidate,str) or not re.fullmatch(r'[0-9a-f]{40}',candidate) or candidate == data['base_commit']:
        raise RepairError('candidate identity invalid')
    try:
        if cli.sha(repo, candidate) != candidate:
            raise RepairError('candidate object identity changed')
    except cli.WorkbenchError as e:
        raise RepairError('candidate object unavailable') from e
    if subprocess.run(['git','-C',str(repo),'merge-base','--is-ancestor', data['base_commit'],candidate],
                      stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30).returncode:
        raise RepairError('candidate is not a baseline descendant')
    try:
        legacy = cli.read(snapshot)
        legacy_repo = _under_trusted_root(legacy['repo'], Path(legacy['repo']).parent)
        if (legacy_repo.is_symlink() or not legacy_repo.is_dir() or legacy_repo.resolve() != legacy_repo or
                _common_dir(legacy_repo) != _common_dir(repo)):
            raise RepairError('legacy candidate repository changed')
    except (cli.WorkbenchError, KeyError, TypeError, OSError) as e:
        raise RepairError('legacy candidate repository unavailable') from e
    return {'schema': cli.SCHEMA, 'phase': 'ready', 'descriptions': [],
            'base_digest': info['request_digest'], 'request_digest': info['request_digest'],
            'input_digest': None, 'run_settings': {'image': image, 'pr_repo': pr_repo,
              'source_mode': 'image', 'candidate_image': None, 'build_source_image': True, 'memory_mb': memory}}


def repair(task_path, *, trusted_tasks_root=None):
    raw=Path(task_path).expanduser().absolute()
    root=Path(trusted_tasks_root).expanduser().absolute() if trusted_tasks_root else raw.parent.parent
    task=_under_trusted_root(raw, root)
    if task.is_symlink() or not task.is_file():
        raise RepairError('unsafe or missing task')
    root_resolved=root.resolve(strict=True)
    initial=cli.read(task)
    try:
        legacy_raw=Path(initial['adoption']['legacy_task']).absolute()
        legacy=_under_trusted_root(legacy_raw, root_resolved)
    except (KeyError,TypeError) as e:
        raise RepairError('legacy identity unavailable') from e
    _regular(legacy)
    with task_lock(legacy), task_lock(task):
        directory=os.open(task.parent,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _same_directory(directory,task.parent)
            data=cli.read(task)
            if data.get('adoption',{}).get('legacy_task') != str(legacy):
                raise RepairError('legacy identity changed during lock acquisition')
            expected=_expected(task, data)
            _same_directory(directory,task.parent)
            try:
                existing=os.open('input-stage.json',os.O_RDONLY | os.O_NOFOLLOW,dir_fd=directory)
            except FileNotFoundError:
                existing=None
            except OSError as e:
                raise RepairError('unsafe input stage') from e
            if existing is not None:
                try:
                    if not stat.S_ISREG(os.fstat(existing).st_mode):
                        raise RepairError('unsafe input stage')
                    with os.fdopen(existing,'r',encoding='utf-8') as f:
                        existing=None
                        actual=json.load(f)
                except (OSError,ValueError) as e:
                    raise RepairError('invalid retained input stage') from e
                finally:
                    if existing is not None: os.close(existing)
                if actual != expected: raise RepairError('retained input stage differs')
                _same_directory(directory,task.parent)
                return 'already present'
            payload=(json.dumps(expected,ensure_ascii=False,indent=2)+'\n').encode()
            name='.stage-repair-'+uuid.uuid4().hex
            fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
            try:
                with os.fdopen(fd,'wb') as f:
                    f.write(payload); f.flush(); os.fsync(f.fileno())
                _same_directory(directory,task.parent)
                os.link(name,'input-stage.json',src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False)
                os.fsync(directory)
                _same_directory(directory,task.parent)
            finally:
                try: os.unlink(name,dir_fd=directory)
                except FileNotFoundError: pass
            return 'restored'
        finally:
            os.close(directory)


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description='Restore a missing stage for a fully recorded legacy adoption')
    parser.add_argument('--task',required=True)
    parser.add_argument('--tasks-root',required=True,help='Trusted existing task root, including any platform alias')
    args=parser.parse_args(argv)
    try:
        result=repair(args.task,trusted_tasks_root=args.tasks_root)
    except (RepairError,cli.WorkbenchError,OSError,ValueError) as error:
        parser.exit(1, f'Cannot repair adoption stage: {type(error).__name__}: {error}\n')
    print(result)


if __name__=='__main__':
    main()
