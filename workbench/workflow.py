#!/usr/bin/env python3
"""Prepare isolated tasks and record actual Docker-backed verification runs.

The evidence gate checks completeness only; independent semantic verification and
the delivery guard must still evaluate the actual issue and raw test output.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid

from workbench import cli
from workbench.task_lock import locked_task
from runner.sandbox import SandboxError, run_sandbox


class WorkflowError(Exception):
    pass


def _workspace_command(command):
    """Require an explicit workspace entrypoint; inline code cannot attest its source."""
    if not command:
        return False
    executable = command[0]
    if executable.startswith('/workspace/'):
        return '..' not in Path(executable).parts
    if executable in ('node', 'python', 'python3', 'sh', 'bash', 'ruby') and len(command) > 1:
        script = command[1]
        return script.startswith('/workspace/') and '..' not in Path(script).parts
    return False


def _verify_workspace_entry(repo, command):
    if not _workspace_command(command):
        raise WorkflowError('workspace source requires a /workspace script entrypoint; inline code is not accepted')
    entry = command[0] if command[0].startswith('/workspace/') else command[1]
    relative = entry.removeprefix('/workspace/')
    source = (Path(repo) / relative).resolve()
    if not source.is_relative_to(Path(repo).resolve()) or not source.is_file():
        raise WorkflowError('workspace entrypoint is missing or escapes the worktree')
    tracked = subprocess.run(['git', '-C', str(repo), 'ls-files', '--error-unmatch', '--', relative],
                             capture_output=True, text=True)
    if tracked.returncode:
        raise WorkflowError('workspace entrypoint must be tracked in the verified commit')
    return source


def _verified_image(repo, image, timeout, *, commit=None):
    """Compare every tracked blob with /app in a pinned image without running its code.

    docker cp streams the image tar; tracked files must exist and match in full.
    Images assembled from incomplete source checkouts are intentionally rejected.
    """
    if os.environ.get('DOCKER_HOST', '').startswith(('tcp://', 'ssh://')):
        raise WorkflowError('remote Docker hosts are not supported')
    def call(*args):
        try:
            result = subprocess.run(['docker', *args], capture_output=True, text=True,
                                    timeout=min(timeout, 30), check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkflowError('Docker image inspection failed') from exc
        if result.returncode:
            raise WorkflowError('Docker image inspection failed')
        return result.stdout.strip()
    if not os.environ.get('DOCKER_HOST'):
        host = call('context', 'inspect', '--format', '{{(index .Endpoints "docker").Host}}')
        if not host.startswith('unix://'):
            raise WorkflowError('only a local Docker context is supported')
    details = json.loads(call('image', 'inspect', image))
    if not isinstance(details, list) or len(details) != 1:
        raise WorkflowError('cannot resolve image')
    details = details[0]
    image_id = details.get('Id', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id) or details.get('Config', {}).get('Volumes'):
        raise WorkflowError('untrusted image identity or declared volume')
    command = ['ls-tree', '-r', '-z', '--full-tree', commit] if commit else ['ls-files', '--stage', '-z']
    index = subprocess.run(['git', '-C', str(repo), *command],
                           capture_output=True, check=True).stdout
    expected = {}
    for entry in index.split(b'\0'):
        if not entry:
            continue
        header, path = entry.split(b'\t', 1)
        if commit:
            mode, object_type, blob = header.decode().split()
            stage = '0' if object_type == 'blob' else 'unsupported'
        else:
            mode, blob, stage = header.decode().split()
        if stage != '0' or mode not in ('100644', '100755', '120000') or len(blob) != 40:
            raise WorkflowError('unsupported index entry or hash algorithm')
        expected[os.fsdecode(path)] = (mode, blob)
    if not expected:
        raise WorkflowError('cannot bind an empty tracked source tree')
    container = call('create', '--network=none', '--entrypoint', '/bin/true', image_id)
    if not re.fullmatch(r'[0-9a-f]{64}', container):
        raise WorkflowError('invalid Docker container identity')
    process = None
    timer = None
    try:
        process = subprocess.Popen(['docker', 'cp', f'{container}:/app', '-'], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        timer = threading.Timer(timeout, process.kill)
        timer.start()
        found = set()
        with tarfile.open(fileobj=process.stdout, mode='r|*') as archive:
            for member in archive:
                name = member.name.removeprefix('./')
                if name == 'app':
                    continue
                if not name.startswith('app/'):
                    continue
                name = name[4:]
                if name not in expected:
                    continue
                if name in found:
                    raise WorkflowError('duplicate source path in image')
                mode, digest = expected[name]
                if mode == '120000' and member.issym():
                    content = os.fsencode(member.linkname)
                    actual = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
                elif mode != '120000' and member.isfile():
                    if bool(member.mode & 0o111) != (mode == '100755'):
                        raise WorkflowError('image source mode differs from commit')
                    stream = archive.extractfile(member)
                    digestor = hashlib.sha1(b'blob ' + str(member.size).encode() + b'\0')
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digestor.update(chunk)
                    actual = digestor.hexdigest()
                else:
                    raise WorkflowError('image source type differs from commit')
                if actual != digest:
                    raise WorkflowError('image source differs from commit')
                found.add(name)
        if process.wait(timeout=5) or found != expected.keys():
            raise WorkflowError('image source incomplete or extraction failed')
    except (tarfile.TarError, OSError, subprocess.TimeoutExpired) as exc:
        raise WorkflowError('image source verification failed') from exc
    finally:
        if timer:
            timer.cancel()
        if process and process.poll() is None:
            process.kill()
            process.wait()
        if process and process.stdout:
            process.stdout.close()
        call('rm', '--force', container)
    return image_id


def build_source_image(repo, base_commit, base_image, timeout):
    """Overlay a clean committed tree on a verified local baseline image.

    Only changed tracked paths are overlaid on the fully verified baseline.
    This does not install dependencies or compile the project. Actual checks
    still decide whether the resulting image is usable.
    """
    repo = Path(repo).resolve(strict=True)
    deadline = time.monotonic() + timeout
    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise WorkflowError('candidate image build exceeded task time limit')
        return left
    if _git(repo, 'status', '--porcelain', '--untracked-files=all'):
        raise WorkflowError('image build requires a clean worktree')
    head = cli.sha(repo, 'HEAD')
    base = _git(repo, 'rev-parse', '--verify', f'{base_commit}^{{commit}}')
    if subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', base, head]).returncode:
        raise WorkflowError('candidate must descend from the verified baseline')
    image_id = _verified_image(repo, base_image, min(remaining(), 300), commit=base)
    log_path = None
    try:
        inspected = subprocess.run(['docker', 'image', 'inspect', image_id], capture_output=True,
                                   text=True, timeout=min(remaining(), 30), check=True)
        if json.loads(inspected.stdout)[0].get('Config', {}).get('OnBuild'):
            raise WorkflowError('baseline image has ONBUILD instructions')
        deleted = subprocess.run(['git', '-C', str(repo), 'diff', '--no-renames', '--diff-filter=D',
                                  '--name-only', '-z', base, head], capture_output=True, check=True).stdout
        paths = [os.fsdecode(p) for p in deleted.split(b'\0') if p]
        if any('\n' in p or '\r' in p or '\x00' in p for p in paths):
            raise WorkflowError('unsupported deleted source path')
        changed = subprocess.run(['git', '-C', str(repo), 'diff', '--no-renames', '--diff-filter=d',
                                  '--name-only', '-z', base, head], capture_output=True, check=True).stdout
        overlays = [os.fsdecode(p) for p in changed.split(b'\0') if p]
        recipe = f'FROM {image_id}\n'
        if paths:
            recipe += 'RUN rm -f -- ' + ' '.join(shlex.quote('/app/' + p) for p in paths) + '\n'
        if overlays:
            recipe += 'COPY source/ /app/\n'
        tag = 'workbench-candidate:' + uuid.uuid4().hex
        with tempfile.TemporaryFile() as archive_file, tempfile.NamedTemporaryFile(
                prefix='iboard-image-build-', suffix='.log', delete=False) as log:
            log_path = Path(log.name)
            if overlays:
                archive_env = os.environ.copy()
                archive_env['GIT_LITERAL_PATHSPECS'] = '1'
                subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', '--prefix=source/',
                                head, '--', *overlays], stdout=archive_file, stderr=log,
                               timeout=remaining(), check=True, env=archive_env)
            else:
                with tarfile.open(fileobj=archive_file, mode='w'):
                    pass
            archive_file.seek(0)
            with tarfile.open(fileobj=archive_file, mode='a') as archive:
                content = recipe.encode()
                info = tarfile.TarInfo('Dockerfile')
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            archive_file.seek(0)
            env = os.environ.copy()
            env['DOCKER_BUILDKIT'] = '0'
            env.pop('BUILDKIT_HOST', None)
            subprocess.run(['docker', 'build', '--network=none', '--pull=false', '--tag', tag, '-'],
                           stdin=archive_file, stdout=log, stderr=log, timeout=remaining(), check=True, env=env)
        if cli.sha(repo, 'HEAD') != head or _git(repo, 'status', '--porcelain', '--untracked-files=all'):
            raise WorkflowError('candidate changed during image build')
        result = _verified_image(repo, tag, min(remaining(), 300))
        log_path.unlink()
        return result
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, tarfile.TarError, ValueError) as error:
        details = f' (exit {error.returncode})' if isinstance(error, subprocess.CalledProcessError) else ''
        if log_path is not None:
            # NamedTemporaryFile is owner-only. Retain tool output, never the environment.
            details += f'; Docker/git output saved at {log_path}'
        raise WorkflowError(f'candidate source image build failed: {type(error).__name__}{details}') from error


def _git(repo: Path, *args: str, timeout=None) -> str:
    try:
        process = subprocess.run(['git', '-C', str(repo), *args], text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise WorkflowError('Git operation exceeded task time limit; inspect retained worktree') from error
    if process.returncode:
        raise WorkflowError(f"git {' '.join(args)}: {process.stderr.strip()}")
    return process.stdout.strip()


def validate_required_ci_workflows(value, *, required=False):
    if value is None and not required:
        return None
    if (not isinstance(value, list) or not value or len(value) > 10 or
            any(not isinstance(name, str) or not name.strip() or name != name.strip() or
                name.startswith('-') or len(name) > 100 for name in value) or
            len(set(value)) != len(value)):
        raise WorkflowError('required_ci_workflows must be a nonempty list of unique workflow names')
    return list(value)


def validate_ci_skip_policy(policy, reason, required):
    if policy is None and reason is None:
        return None
    if (policy not in cli.CI_SKIP_POLICIES or not isinstance(reason, str) or not reason.strip() or
            not isinstance(required, list) or 'CI' not in required):
        raise WorkflowError('CI skip policy requires a reason and CI as a required workflow')
    return policy


def prepare(*, task_id, repo, tasks_root, workspaces_root, source, kind, surface,
            iterations, seconds, implementer, started_at=None, required_ci_workflows=None,
            ci_skip_policy=None, ci_skip_reason=None, base_commit=None):
    """Create a new task directory and a separate branch/worktree.

    Existing worktrees, task directories, and branches are never reused or reset.
    """
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', task_id):
        raise WorkflowError('invalid task ID')
    if kind not in ('bug', 'feature') or surface not in ('ui', 'non-ui'):
        raise WorkflowError('invalid task kind or surface')
    if (not source.strip() or not implementer.strip() or
            (iterations is not None and (type(iterations) is not int or iterations < 1)) or
            (seconds is not None and (type(seconds) is not int or seconds < 1))):
        raise WorkflowError('source, implementer and positive limits are required')
    required_ci_workflows = validate_required_ci_workflows(required_ci_workflows)
    ci_skip_policy = validate_ci_skip_policy(ci_skip_policy, ci_skip_reason, required_ci_workflows)
    began = datetime.fromisoformat(started_at) if started_at else datetime.now(timezone.utc)
    if began.tzinfo is None or began > datetime.now(timezone.utc):
        raise WorkflowError('invalid task start time')
    def remaining():
        if seconds is None:
            return 86400
        value = seconds - (datetime.now(timezone.utc) - began).total_seconds()
        if value <= 0:
            raise WorkflowError('task time limit reached during preparation')
        return value
    repo = Path(repo).expanduser().resolve(strict=True)
    if _git(repo, 'status', '--porcelain', '--untracked-files=all', timeout=remaining()):
        raise WorkflowError('source repository has uncommitted files')
    if base_commit is not None and not re.fullmatch(r'[0-9a-f]{40}', base_commit):
        raise WorkflowError('baseline must be an explicit commit SHA')
    base = _git(repo, 'rev-parse', '--verify', f'{base_commit or "HEAD"}^{{commit}}', timeout=remaining())
    tasks_root = Path(tasks_root).expanduser().resolve()
    workspaces_root = Path(workspaces_root).expanduser().resolve()
    tasks_root.mkdir(parents=True, exist_ok=True)
    workspaces_root.mkdir(parents=True, exist_ok=True)
    task_dir, workspace = tasks_root / task_id, workspaces_root / task_id
    branch = f'workbench/{task_id}'
    if workspace.exists() or task_dir.exists():
        raise WorkflowError('task or workspace already exists; use resume rather than overwrite')
    try:
        existing = subprocess.run(['git', '-C', str(repo), 'show-ref', '--verify', '--quiet',
                                   f'refs/heads/{branch}'], timeout=remaining())
    except subprocess.TimeoutExpired as error:
        raise WorkflowError('task time limit reached during branch inspection') from error
    if existing.returncode == 0:
        raise WorkflowError('task branch already exists')
    task_dir.mkdir(mode=0o700)
    try:
        _git(repo, 'worktree', 'add', '-b', branch, str(workspace), base, timeout=remaining())
        args = argparse.Namespace(task=str(task_dir / 'task.json'), id=task_id,
                                  repo=str(workspace), kind=kind, source=source,
                                  implementer=implementer, surface=surface,
                                  iterations=iterations, seconds=seconds, started_at=began.isoformat())
        remaining()
        with redirect_stdout(io.StringIO()):
            cli.init(args)
        if required_ci_workflows is not None:
            data = cli.read(task_dir / 'task.json')
            data['required_ci_workflows'] = required_ci_workflows
            if ci_skip_policy is not None:
                data['ci_skip_policy'] = ci_skip_policy
                data['events'].append({'at': cli.now(), 'type': 'ci-skip-policy',
                    'mode': 'creation', 'policy': ci_skip_policy, 'source': 'task-creation', 'reason': ci_skip_reason.strip(),
                    'base_commit': data['base_commit'], 'required_ci_workflows': list(required_ci_workflows)})
            cli.write(task_dir / 'task.json', data)
    except Exception:
        # Keep a partially created workspace for recovery; never delete it on error.
        raise
    return {'task': str(task_dir / 'task.json'), 'workspace': str(workspace),
            'branch': branch, 'base_commit': base}


@locked_task('task')
def run_check(*, task, kind, image, command, timeout, expect_exit, marker, actor,
              description, runner=run_sandbox, memory_mb=2048, source_mode='workspace'):
    """Run and archive a real test result; success does not establish semantics.

    Reproduction must run on the baseline and assert an explicit observable
    marker; a fault can occur even when a command exits successfully.
    """
    task = Path(task).expanduser().resolve(strict=True)
    data = cli.read(task)
    if data['status'] != 'running' or cli.enforce_budget(data):
        raise WorkflowError('task is stopped or its budget is exhausted')
    if not data['criteria']:
        raise WorkflowError('record acceptance criteria before running checks')
    if kind not in ('reproduction', 'resolution', 'test-result', 'runtime-log', 'feature-doc', 'screenshot'):
        raise WorkflowError('unsupported check evidence kind')
    if not command or timeout <= 0 or not description.strip() or not actor.strip():
        raise WorkflowError('command, actor, description and positive timeout are required')
    head = cli.sha(data['repo'], 'HEAD')
    if cli.git(data['repo'], 'status', '--porcelain', '--untracked-files=all'):
        raise WorkflowError('source worktree must be clean before the check')
    if source_mode == 'workspace' or kind == 'screenshot':
        _verify_workspace_entry(data['repo'], command)
    if source_mode == 'workspace':
        selected_image = image
    elif source_mode == 'image':
        selected_image = _verified_image(Path(data['repo']), image, min(timeout, 300))
    else:
        raise WorkflowError('unknown source mode')
    if kind == 'reproduction':
        if data['kind'] != 'bug' or head != data['base_commit']:
            raise WorkflowError('reproduction must run on the bug baseline commit')
        if not marker:
            raise WorkflowError('reproduction needs an observable failure marker')
    remaining = timeout if data['limits']['seconds'] is None else data['limits']['seconds'] - (
        datetime.now(timezone.utc) - datetime.fromisoformat(data['started_at'])).total_seconds()
    if remaining <= 0:
        raise WorkflowError('time limit reached')
    report_path = task.parent / f'{kind}-{uuid.uuid4().hex}.json'
    screenshot_path = task.parent / f'screenshot-{uuid.uuid4().hex}.png' if kind == 'screenshot' else None
    runner_options = {'source_root': '/app'} if source_mode == 'image' else {}
    if screenshot_path is not None:
        runner_options.update(memory_mb=memory_mb, screenshot_output=screenshot_path)
    elif memory_mb != 2048:
        runner_options['memory_mb'] = memory_mb
    try:
        report = runner(data['repo'], selected_image, command, min(timeout, remaining), **runner_options)
    except (SandboxError, OSError, ValueError) as exc:
        report = {'error': str(exc), 'exit_code': None, 'cleanup_confirmed': False}
    # Always save actual result, including failures, before enforcing any gate.
    report.update({'kind': kind, 'commit': head, 'actor': actor, 'description': description,
                   'expected_exit': expect_exit, 'expected_marker': marker, 'source_mode': source_mode,
                   'verified_image_id': selected_image if source_mode == 'image' else None,
                   'source_verified_commit': head,
                   'source_limit': 'Image /app matched every tracked Git blob before execution' if source_mode == 'image'
                     else 'Entry script is in /workspace; downstream execution still requires semantic review'})
    cli.write(report_path, report)
    output = str(report.get('stdout', '')) + '\n' + str(report.get('stderr', ''))
    if (report.get('error') or report.get('timed_out') or not report.get('cleanup_confirmed')
            or report.get('exit_code') != expect_exit or (marker and marker not in output)
            or (screenshot_path is not None and (not screenshot_path.is_file()
                or report.get('screenshot_sha256') != cli.digest(screenshot_path)))):
        raise WorkflowError(f'check failed; raw report saved at {report_path}')
    if cli.enforce_budget(data):
        raise WorkflowError(f'task budget expired during check; raw report saved at {report_path}')
    if cli.sha(data['repo'], 'HEAD') != head or cli.git(data['repo'], 'status', '--porcelain', '--untracked-files=all'):
        raise WorkflowError(f'HEAD or worktree changed during check; raw report saved at {report_path}')
    args = argparse.Namespace(file=str(screenshot_path or report_path), kind=kind, commit=head, actor=actor,
                              description=description, criterion=[])
    cli.evidence_add(args, data)
    if screenshot_path is not None:
        data['evidence'][-1]['execution_report'] = str(report_path)
        data['evidence'][-1]['execution_report_sha256'] = cli.digest(report_path)
    cli.write(task, data)
    result = {'report': str(report_path), 'commit': head, 'kind': kind}
    if screenshot_path is not None:
        result['screenshot'] = str(screenshot_path)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('prepare')
    for name in ('id', 'repo', 'tasks-root', 'workspaces-root', 'source', 'implementer'):
        create.add_argument(f'--{name}', required=True)
    create.add_argument('--kind', choices=('bug', 'feature'), required=True)
    create.add_argument('--surface', choices=('ui', 'non-ui'), required=True)
    create.add_argument('--iterations', type=int, required=True)
    create.add_argument('--seconds', type=int, required=True)
    create.add_argument('--required-ci-workflow', action='append', dest='required_ci_workflows',
                        help='GitHub Actions workflow name required for delivery; may be repeated')
    create.add_argument('--ci-skip-policy', choices=cli.CI_SKIP_POLICIES)
    create.add_argument('--ci-skip-reason', help='reason for the explicit CI skip policy')
    check = commands.add_parser('check')
    for name in ('task', 'kind', 'image', 'actor', 'description'):
        check.add_argument(f'--{name}', required=True)
    check.add_argument('--timeout', type=float, required=True)
    check.add_argument('--memory-mb', type=int, default=2048)
    check.add_argument('--source-mode', choices=('workspace', 'image'), default='workspace')
    check.add_argument('--expect-exit', type=int, default=0)
    check.add_argument('--marker', default='')
    check.add_argument('test_command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        result = prepare(task_id=args.id, repo=args.repo, tasks_root=args.tasks_root,
                         workspaces_root=args.workspaces_root, source=args.source,
                         kind=args.kind, surface=args.surface, iterations=args.iterations,
                         seconds=args.seconds, implementer=args.implementer,
                         required_ci_workflows=args.required_ci_workflows,
                         ci_skip_policy=args.ci_skip_policy, ci_skip_reason=args.ci_skip_reason)
    else:
        command = args.test_command[1:] if args.test_command[:1] == ['--'] else args.test_command
        result = run_check(task=args.task, kind=args.kind, image=args.image, command=command,
                           timeout=args.timeout, expect_exit=args.expect_exit, marker=args.marker,
                           actor=args.actor, description=args.description, memory_mb=args.memory_mb,
                           source_mode=args.source_mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (WorkflowError, cli.WorkbenchError, OSError, ValueError) as error:
        print(f'workflow: {error}', file=sys.stderr)
        sys.exit(2)
