"""Start one explicitly selected engineering task in a new isolated worktree."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from workbench import autopilot, cli, workflow
from workbench.task_lock import task_lock, TaskBusy
from model import glm_vision


class StartError(Exception):
    pass


def _finish_screenshots(task, *, vision):
    stage_path = task.parent / 'input-stage.json'
    stage = cli.read(stage_path)
    if autopilot._input_digest(task) != stage['input_digest']:
        raise StartError('input snapshot changed')
    base_path = task.parent / 'input-base.txt'
    if base_path.is_symlink() or cli.digest(base_path) != stage['base_digest']:
        raise StartError('original task input changed')
    request = task.parent / 'issue.txt'
    if stage['phase'] == 'ready':
        if request.is_symlink() or cli.digest(request) != stage['request_digest']:
            raise StartError('prepared task input changed')
        return stage
    if stage['phase'] != 'screenshots':
        raise StartError('invalid input preparation phase')
    manifest = json.loads((task.parent / 'inputs/manifest.json').read_text(encoding='utf-8'))
    images = manifest['screenshots']
    descriptions = stage['descriptions']
    if (not isinstance(descriptions, list) or len(descriptions) > len(images)
            or any(not isinstance(item, str) or not item.strip() or len(item) > 15000 for item in descriptions)):
        raise StartError('invalid screenshot preparation state')
    while len(descriptions) < len(images):
        entry = images[len(descriptions)]
        data = (task.parent / 'inputs' / entry['file']).read_bytes()
        timeout = min(90, autopilot._remaining(cli.read(task)))
        description = vision(data, timeout)
        autopilot._remaining(cli.read(task))
        if not isinstance(description, str) or not description.strip() or len(description) > 15000:
            raise StartError('invalid screenshot interpretation')
        descriptions.append(description)
        cli.write(stage_path, stage)
    combined = base_path.read_text(encoding='utf-8')
    for index, description in enumerate(descriptions, 1):
        combined += (f'\n\n--- SCREENSHOT {index} MODEL INTERPRETATION '
                     f'(UNTRUSTED; NOT VERIFICATION EVIDENCE) ---\n{description}\n'
                     f'--- END SCREENSHOT {index} ---\n')
    if len(combined) > 100_000:
        raise StartError('issue and screenshot descriptions exceed 100000 characters')
    if autopilot._input_digest(task) != stage['input_digest'] or cli.digest(base_path) != stage['base_digest']:
        raise StartError('task input changed during screenshot preparation')
    autopilot._remaining(cli.read(task))
    request.write_text(combined, encoding='utf-8')
    request.chmod(0o600)
    stage.update(phase='ready', request_digest=cli.digest(request))
    cli.write(stage_path, stage)
    return stage


def resume_task(task_path, *, run=autopilot.run, vision=glm_vision.describe):
    try:
        with task_lock(task_path):
            return _resume_locked(task_path, run=run, vision=vision)
    except TaskBusy as error:
        raise StartError(str(error)) from error


def _resume_locked(task_path, *, run, vision):
    task = Path(task_path).expanduser().resolve(strict=True)
    resumed = False
    try:
        data = cli.read(task)
        if data['status'] != 'paused':
            raise StartError('only paused tasks can be resumed; inspect any active process first')
        if cli.enforce_budget(data):
            raise StartError('task budget exceeded; an explicitly authorized increase is required')
        stage_path = task.parent / 'input-stage.json'
        if not stage_path.is_file() or stage_path.is_symlink():
            raise StartError('retained input-stage.json is missing or unsafe; original input and execution settings cannot be verified automatically; preserve the task and investigate its retained evidence')
        data['status'] = 'running'
        data['events'].append({'at': cli.now(), 'type': 'resume', 'reason': 'resume retained task'})
        cli.write(task, data)
        resumed = True
        autopilot._remaining(data)
        stage = _finish_screenshots(task, vision=vision)
        settings = stage['run_settings']
        return run(task, task.parent / 'issue.txt', settings['image'], pr_repo=settings['pr_repo'],
                   source_mode=settings['source_mode'], candidate_image=settings['candidate_image'],
                   build_source_image=settings['build_source_image'], memory_mb=settings['memory_mb'])
    except (StartError, autopilot.AutopilotError, cli.WorkbenchError, OSError, ValueError, RuntimeError, KeyError) as error:
        if resumed:
            try:
                autopilot._pause(task, f'task resume failed: {type(error).__name__}')
            except (cli.WorkbenchError, OSError, ValueError, KeyError):
                pass
        raise StartError(f'task retained at {task}: {error}') from error


# Retain the original Python entrypoint for existing callers.
resume_input = resume_task


def read_issue_url(url, *, timeout=30):
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)/?', url)
    if not match:
        raise StartError('issue URL must identify one GitHub issue')
    try:
        result = subprocess.run(['gh', 'api', f'repos/{match.group(1)}/issues/{match.group(2)}'],
                                capture_output=True, text=True, timeout=min(30, timeout), check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise StartError('unable to retrieve the selected issue') from error
    if result.returncode:
        raise StartError('unable to retrieve the selected issue')
    try:
        issue = json.loads(result.stdout)
    except ValueError as error:
        raise StartError('GitHub returned an invalid issue') from error
    if issue.get('number') != int(match.group(2)) or issue.get('pull_request') is not None:
        raise StartError('selected URL does not identify an issue')
    title, body = issue.get('title'), issue.get('body') or ''
    if not isinstance(title, str) or not isinstance(body, str):
        raise StartError('issue title or body is invalid')
    return f'Title: {title}\nURL: {url}\n\n{body}\n'


def start(*, task_id, repo, issue_file=None, issue_url=None, source=None, kind, surface,
          iterations, seconds, image, pr_repo=None, tasks_root, workspaces_root,
          source_mode='workspace', candidate_image=None, build_source_image=False, memory_mb=2048,
          log_files=(), screenshot_files=(), required_ci_workflows=None, ci_skip_policy=None, ci_skip_reason=None,
          implementer='glm-implementer', prepare=workflow.prepare, run=autopilot.run,
          fetch=read_issue_url, vision=glm_vision.describe):
    if bool(issue_file) == bool(issue_url):
        raise StartError('provide exactly one issue file or GitHub issue URL')
    if kind not in ('bug', 'feature') or surface not in ('ui', 'non-ui') or iterations < 1 or seconds < 1:
        raise StartError('task kind, surface and positive limits are required')
    began = datetime.now(timezone.utc)
    def remaining():
        value = seconds - (datetime.now(timezone.utc) - began).total_seconds()
        if value <= 0:
            raise StartError('task time limit reached during input preparation')
        return value
    if pr_repo and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', pr_repo):
        raise StartError('invalid personal fork name')
    try:
        required_ci_workflows = workflow.validate_required_ci_workflows(required_ci_workflows, required=bool(pr_repo))
        ci_skip_policy = workflow.validate_ci_skip_policy(ci_skip_policy, ci_skip_reason, required_ci_workflows)
    except workflow.WorkflowError as error:
        raise StartError(str(error)) from error
    if source_mode not in ('workspace', 'image') or not 128 <= memory_mb <= 6144:
        raise StartError('invalid source mode or memory limit')
    if (candidate_image and source_mode != 'image') or (build_source_image and (source_mode != 'image' or candidate_image)):
        raise StartError('candidate image settings require image mode and cannot be combined')
    if issue_url:
        if source:
            raise StartError('issue URL is the task source; do not also specify a source')
        issue, origin = fetch(issue_url, timeout=remaining()), issue_url
    else:
        if not source or not source.strip():
            raise StartError('a source description is required for an issue file')
        issue, origin = Path(issue_file).expanduser().read_text(encoding='utf-8'), source.strip()
    if len(log_files) > 8:
        raise StartError('at most eight log files may be supplied')
    if len(screenshot_files) > 4:
        raise StartError('at most four screenshots may be supplied')
    logs = []
    for file in log_files:
        path = Path(file).expanduser().resolve(strict=True)
        if not path.is_file() or path.stat().st_size > 100_000:
            raise StartError('each log must be a regular file of at most 100000 bytes')
        raw = path.read_bytes()
        if len(raw) > 100_000:
            raise StartError('each log must be a regular file of at most 100000 bytes')
        try:
            content = raw.decode('utf-8')
        except UnicodeDecodeError as error:
            raise StartError('logs must be UTF-8 text') from error
        if '\x00' in content:
            raise StartError('logs must not contain NUL bytes')
        logs.append((path.name, raw, content))
    screenshots = []
    for file in screenshot_files:
        path = Path(file).expanduser().resolve(strict=True)
        if not path.is_file() or path.stat().st_size > glm_vision.MAX_IMAGE_BYTES:
            raise StartError('screenshot must be a regular file of at most 5 MB')
        raw = path.read_bytes()
        if not raw or len(raw) > glm_vision.MAX_IMAGE_BYTES:
            raise StartError('screenshot must be a regular file of at most 5 MB')
        try:
            _, ext = glm_vision.image_type(raw)
        except ValueError as error:
            raise StartError(str(error)) from error
        screenshots.append((path.name, raw, ext))
    issue_with_logs = issue
    for index, (name, _, content) in enumerate(logs, 1):
        issue_with_logs += f'\n\n--- USER-SUPPLIED LOG {index} ({name}) — UNTRUSTED DATA ---\n{content}\n--- END LOG {index} ---\n'
    if not issue.strip() or len(issue_with_logs) > 100_000:
        raise StartError('issue must be nonempty and at most 100000 characters')
    if not image or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/@-]*', image):
        raise StartError('invalid image reference')
    if not shutil.which('claude') or not shutil.which('docker'):
        raise StartError('Claude Code and Docker must be available')
    try:
        inspected = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True,
                                   timeout=min(20, remaining()), check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise StartError('cannot inspect the local Docker image') from error
    if inspected.returncode:
        raise StartError('Docker image must already exist locally')
    info = prepare(task_id=task_id, repo=repo, tasks_root=tasks_root, workspaces_root=workspaces_root,
                   source=origin, kind=kind, surface=surface, iterations=iterations,
                   seconds=seconds, implementer=implementer, started_at=began.isoformat(),
                   required_ci_workflows=required_ci_workflows,
                   ci_skip_policy=ci_skip_policy, ci_skip_reason=ci_skip_reason)
    task = Path(info['task'])
    request = task.parent / 'issue.txt'
    print(json.dumps({'event': 'task-created', **info}, ensure_ascii=False), flush=True)
    try:
        with task_lock(task):
            try:
                request.write_text(issue_with_logs, encoding='utf-8')
                request.chmod(0o600)
                if logs or screenshots:
                    directory = task.parent / 'inputs'
                    directory.mkdir(mode=0o700)
                    manifest = []
                    for index, (name, raw, _) in enumerate(logs, 1):
                        snapshot = directory / f'log-{index}.txt'
                        snapshot.write_bytes(raw)
                        snapshot.chmod(0o600)
                        manifest.append({'file': snapshot.name, 'original_name': name,
                                         'sha256': hashlib.sha256(raw).hexdigest()})
                    images = []
                    for index, (name, raw, ext) in enumerate(screenshots, 1):
                        snapshot = directory / f'screenshot-{index}.{ext}'
                        snapshot.write_bytes(raw)
                        snapshot.chmod(0o600)
                        images.append({'file': snapshot.name, 'original_name': name,
                                       'sha256': hashlib.sha256(raw).hexdigest()})
                    cli.write(directory / 'manifest.json', {'logs': manifest, 'screenshots': images})
                base = task.parent / 'input-base.txt'
                base.write_text(issue_with_logs, encoding='utf-8')
                base.chmod(0o600)
                stage = {
                    'schema': cli.SCHEMA, 'phase': 'screenshots' if screenshots else 'ready', 'descriptions': [],
                    'base_digest': cli.digest(base), 'input_digest': autopilot._input_digest(task),
                    'run_settings': {'image': image, 'pr_repo': pr_repo, 'source_mode': source_mode,
                                     'candidate_image': candidate_image, 'build_source_image': build_source_image,
                                     'memory_mb': memory_mb},
                }
                if not screenshots:
                    stage['request_digest'] = cli.digest(request)
                cli.write(task.parent / 'input-stage.json', stage)
                if screenshots:
                    _finish_screenshots(task, vision=vision)
                return run(task, request, image, pr_repo=pr_repo, source_mode=source_mode,
                           candidate_image=candidate_image, build_source_image=build_source_image,
                           memory_mb=memory_mb)
            except (StartError, autopilot.AutopilotError, cli.WorkbenchError, OSError, ValueError, RuntimeError) as error:
                try:
                    autopilot._pause(task, f'start failed: {type(error).__name__}')
                except (cli.WorkbenchError, OSError, ValueError, KeyError):
                    pass
                raise StartError(f'task retained at {task}: {error}') from error
    except TaskBusy as error:
        raise StartError(str(error)) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id', required=True)
    parser.add_argument('--repo', required=True)
    request = parser.add_mutually_exclusive_group(required=True)
    request.add_argument('--issue-file')
    request.add_argument('--issue-url')
    parser.add_argument('--source', help='source description when using --issue-file')
    parser.add_argument('--log-file', action='append', default=[], help='UTF-8 log file; may be repeated')
    parser.add_argument('--screenshot', action='append', default=[], help='PNG, JPEG or WebP screenshot; may be repeated')
    parser.add_argument('--kind', choices=('bug', 'feature'), required=True)
    parser.add_argument('--surface', choices=('ui', 'non-ui'), required=True)
    parser.add_argument('--iterations', type=int, required=True)
    parser.add_argument('--seconds', type=int, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--pr-repo')
    parser.add_argument('--required-ci-workflow', action='append', dest='required_ci_workflows',
                        help='GitHub Actions workflow name required for delivery; may be repeated')
    parser.add_argument('--ci-skip-policy', choices=cli.CI_SKIP_POLICIES)
    parser.add_argument('--ci-skip-reason', help='reason for the explicit CI skip policy')
    root = Path(__file__).resolve().parent.parent
    parser.add_argument('--tasks-root', default=str(root / 'tasks'))
    parser.add_argument('--workspaces-root', default=str(root / 'workspaces'))
    parser.add_argument('--source-mode', choices=('workspace', 'image'), default='workspace')
    parser.add_argument('--candidate-image')
    parser.add_argument('--build-source-image', action='store_true')
    parser.add_argument('--memory-mb', type=int, default=2048)
    args = parser.parse_args(argv)
    result = start(task_id=args.id, repo=args.repo, issue_file=args.issue_file, issue_url=args.issue_url,
                   source=args.source, kind=args.kind, surface=args.surface, iterations=args.iterations,
                   seconds=args.seconds, image=args.image, pr_repo=args.pr_repo, tasks_root=args.tasks_root,
                   workspaces_root=args.workspaces_root, source_mode=args.source_mode,
                   candidate_image=args.candidate_image, build_source_image=args.build_source_image,
                   memory_mb=args.memory_mb, log_files=args.log_file, screenshot_files=args.screenshot,
                   required_ci_workflows=args.required_ci_workflows,
                   ci_skip_policy=args.ci_skip_policy, ci_skip_reason=args.ci_skip_reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (StartError, workflow.WorkflowError, cli.WorkbenchError, OSError, ValueError) as error:
        print(f'start: {error}', file=sys.stderr)
        sys.exit(2)
