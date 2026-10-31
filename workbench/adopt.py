"""Rebind a legacy task as a new task and independently reverify its candidate.

The legacy directory and worktree are never changed. The new task has its own
branch and, when published, its own pull request. Old evidence is not reused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from workbench import autopilot, cli, delivery, workflow
from workbench.task_lock import task_lock


class AdoptionError(Exception):
    pass


def create(*, legacy_task, task_id, tasks_root, workspaces_root, image, pr_repo,
           memory_mb=4096):
    """Create a fresh, input-bound task; caller can then run or resume it."""
    legacy = Path(legacy_task).expanduser().resolve(strict=True)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', pr_repo):
        raise AdoptionError('a personal fork is required')
    if not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/@-]*', image):
        raise AdoptionError('invalid baseline image reference')
    if not 128 <= memory_mb <= 6144:
        raise AdoptionError('invalid memory limit')
    with task_lock(legacy):
        old = cli.read(legacy)
        if old.get('status') != 'paused' or old.get('kind') != 'bug' or old.get('surface') != 'non-ui':
            raise AdoptionError('only paused non-UI legacy bug tasks are supported')
        if any((legacy.parent / name).exists() or (legacy.parent / name).is_symlink()
               for name in ('input-stage.json', 'autopilot.json')):
            raise AdoptionError('task has retained workflow state; use normal resume')
        if (legacy.parent / 'inputs').exists() or (legacy.parent / 'inputs').is_symlink():
            raise AdoptionError('legacy attachments require explicit resnapshotting')
        request = legacy.parent / 'issue.txt'
        if request.is_symlink() or not request.is_file() or request.stat().st_size > 100_000:
            raise AdoptionError('legacy request is missing or unsafe')
        issue = request.read_text(encoding='utf-8')
        if not issue.strip():
            raise AdoptionError('legacy request is empty')
        criteria = old.get('criteria')
        if (not isinstance(criteria, list) or not criteria or
                any(not isinstance(c, dict) or not re.fullmatch(r'[A-Za-z0-9._-]+', c.get('id', '')) or
                    not isinstance(c.get('text'), str) or not c['text'].strip() or
                    type(c.get('revision')) is not int or c['revision'] < 1 for c in criteria) or
                len({c['id'] for c in criteria}) != len(criteria)):
            raise AdoptionError('legacy acceptance criteria are invalid')
        base = old.get('base_commit')
        if not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{40}', base):
            raise AdoptionError('legacy baseline is invalid')
        repo = Path(old['repo']).expanduser().resolve(strict=True)
        if cli.git(repo, 'status', '--porcelain', '--untracked-files=all'):
            raise AdoptionError('legacy worktree has uncommitted changes')
        candidate = cli.sha(repo, 'HEAD')
        ancestry = subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', base, candidate],
                                  capture_output=True, timeout=30, check=False)
        if ancestry.returncode or candidate == base:
            raise AdoptionError('candidate is not a descendant of the baseline')
        try:
            details = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True,
                                     text=True, timeout=30, check=True)
            image_id = json.loads(details.stdout)[0]['Id']
        except (OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError, TypeError) as error:
            raise AdoptionError('baseline image cannot be pinned') from error
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
            raise AdoptionError('baseline image identity is invalid')
        workflows = workflow.validate_required_ci_workflows(old.get('required_ci_workflows'), required=True)
        policy = old.get('ci_skip_policy')
        if policy is not None and not delivery._recorded_skip_policy(old):
            raise AdoptionError('legacy CI skip policy evidence is invalid')
        policy_record = None
        policy_basis = None
        if policy is not None:
            policy_record = next(event for event in old['events']
                                 if isinstance(event, dict) and event.get('type') == 'ci-skip-policy')
            if policy_record.get('mode') != 'existing':
                raise AdoptionError('legacy CI policy has no revalidatable basis')
            basis = Path(policy_record['basis']).resolve(strict=True)
            if not basis.is_file() or basis.stat().st_size > 1_000_000:
                raise AdoptionError('legacy CI policy basis is invalid')
            policy_basis = basis.read_bytes()
            if (not policy_basis or hashlib.sha256(policy_basis).hexdigest() != policy_record['basis_sha256'] or
                    policy_record['criteria_sha256'] != cli.criteria_digest(old)):
                raise AdoptionError('legacy CI policy basis or acceptance changed')
        old_digest = cli.digest(legacy)
        issue_digest = hashlib.sha256(issue.encode('utf-8')).hexdigest()
        if cli.digest(request) != issue_digest:
            raise AdoptionError('legacy request changed while being rebound')
        info = workflow.prepare(task_id=task_id, repo=repo, tasks_root=tasks_root,
            workspaces_root=workspaces_root, source=old['source'], kind=old['kind'], surface=old['surface'],
            iterations=old['limits']['iterations'], seconds=old['limits']['seconds'],
            implementer=old['implementer'], required_ci_workflows=workflows, base_commit=base)
        task = Path(info['task'])
        try:
            snapshot = task.parent / 'legacy-task.json'
            cli.write(snapshot, old)
            for name in ('issue.txt', 'input-base.txt'):
                target = task.parent / name
                target.write_text(issue, encoding='utf-8')
                target.chmod(0o600)
            data = cli.read(task)
            data['criteria'] = criteria
            data['criterion_changes'] = old.get('criterion_changes', [])
            if policy_record is not None:
                basis_snapshot = task.parent / 'legacy-ci-policy-basis'
                basis_snapshot.write_bytes(policy_basis)
                basis_snapshot.chmod(0o600)
                data['ci_skip_policy'] = policy
                data['events'].append({'at': cli.now(), 'type': 'ci-skip-policy', 'mode': 'existing',
                    'policy': policy, 'source': policy_record['source'], 'reason': policy_record['reason'],
                    'basis': str(basis_snapshot), 'basis_sha256': cli.digest(basis_snapshot),
                    'criteria_sha256': cli.criteria_digest(data), 'base_commit': base,
                    'required_ci_workflows': workflows})
            data['adoption'] = {'legacy_task': str(legacy), 'legacy_task_sha256': old_digest,
                'legacy_snapshot_sha256': cli.digest(snapshot), 'request_digest': issue_digest,
                'criteria_digest': cli.criteria_digest(data), 'base_commit': base,
                'candidate_commit': candidate, 'image': image_id, 'pr_repo': pr_repo, 'memory_mb': memory_mb,
                'provenance': 'current request rebound; original input and execution settings unavailable'}
            data['events'].append({'at': cli.now(), 'type': 'legacy-adoption-created',
                                   'legacy_task': str(legacy), 'candidate_commit': candidate})
            data['status'] = 'paused'
            data['events'].append({'at': cli.now(), 'type': 'pause',
                                   'reason': 'adoption prepared for verified execution'})
            cli.write(task, data)
            stage = {'schema': cli.SCHEMA, 'phase': 'ready', 'descriptions': [],
                'base_digest': cli.digest(task.parent / 'input-base.txt'),
                'request_digest': issue_digest, 'input_digest': None,
                'run_settings': {'image': image_id, 'pr_repo': pr_repo, 'source_mode': 'image',
                                 'candidate_image': None, 'build_source_image': True, 'memory_mb': memory_mb}}
            cli.write(task.parent / 'input-stage.json', stage)
            return info
        except Exception:
            autopilot._pause(task, 'legacy adoption preparation failed; inspect retained task')
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('legacy-task', 'id', 'tasks-root', 'workspaces-root', 'image', 'pr-repo'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--memory-mb', type=int, default=4096)
    args = parser.parse_args(argv)
    info = create(legacy_task=args.legacy_task, task_id=args.id, tasks_root=args.tasks_root,
                  workspaces_root=args.workspaces_root, image=args.image, pr_repo=args.pr_repo,
                  memory_mb=args.memory_mb)
    print(json.dumps(info, ensure_ascii=False), flush=True)
    from workbench.start import resume_task
    print(json.dumps(resume_task(Path(info['task'])), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
