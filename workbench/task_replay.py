"""Independently verify a queued candidate after replay onto an exact new main."""

import argparse
import json
from pathlib import Path
import re
import sys

from workbench import (autonomous_run, autopilot, autopilot_full, cli, delivery, local_gate, model_profiles,
                       phase_replay, run_control, stack_rebase, start, workflow)
from workbench.task_lock import task_lock


class ReplayError(ValueError):
    pass


ERRORS = (*autonomous_run.ERRORS, local_gate.LocalGateError, stack_rebase.RebaseError)
CONTRACT_FIELDS = ('id', 'kind', 'surface', 'source', 'original_source', 'implementer', 'started_at',
                   'limits', 'iterations', 'criteria', 'criterion_changes', 'impact_review',
                   'required_ci_workflows', 'regression_test', 'publish_only', 'root_cause', 'coverage_swarm',
                   'model_profile')


def _regular(path):
    if path.is_symlink() or not path.is_file():
        raise ReplayError(f'missing or unsafe replay input: {path.name}')
    return path


def _git(repo, *args, timeout):
    return stack_rebase._output(stack_rebase._git(repo, *args, seconds=min(120, timeout)))


def _paths(repo, base, head, timeout):
    try:
        raw = _git(repo, 'log', '--format=', '--name-only', '-z', '--no-renames',
                   f'{base}..{head}', '--', timeout=timeout)
        return sorted(phase_replay._paths(raw))
    except stack_rebase.RebaseError as error:
        raise ReplayError(str(error)) from error


def _fetch_main(repo, destination, commit, timeout):
    present = stack_rebase._git(repo, 'cat-file', '-e', commit + '^{commit}', seconds=min(15, timeout))
    if present.returncode:
        _git(repo, 'fetch', '--no-tags', '--no-write-fetch-head', '--no-recurse-submodules',
             f'https://github.com/{destination}.git', 'refs/heads/main', timeout=timeout)
    if cli.sha(repo, commit, timeout=min(15, timeout)) != commit:
        raise ReplayError('the observed main commit is unavailable locally')


def _snapshot(task, data, stage, state, member):
    old_head = state.get('candidate_commit')
    if (data['status'] != 'paused' or state.get('phase') != 'delivery'
            or not isinstance(old_head, str) or not phase_replay.COMMIT.fullmatch(old_head)
            or stage['phase'] != 'ready' or state.get('issue_digest') != stage['request_digest']
            or state.get('input_digest') != stage['input_digest'] or not isinstance(state.get('plan'), dict)):
        raise ReplayError('replay requires a paused and previously verified delivery candidate')
    settings = stage['run_settings']
    model_profiles.intake(task, data, stage)
    if (data['kind'] not in ('bug', 'feature') or any(data.get(key) for key in (
            'verification_target', 'adoption', 'phase_binding', 'feature_context', 'refactor_contract',
            'visual_baseline', 'ci_skip_policy', 'baseline_replay', 'local_gate_reverify', 'performance_contract',
            'blast_radius'))
            or settings['candidate_image'] or (settings['source_mode'] == 'image' and not settings['build_source_image'])
            or not member['destination'] or member['predicate'] not in ('published', 'merged')):
        raise ReplayError('this task needs an explicit replay adapter for its existing gates')
    head, problems = cli.evidence_gate(data, old_head)
    if head != old_head or problems:
        raise ReplayError('original local acceptance no longer passes: ' + '; '.join(problems))
    review = delivery._independent_verification(data, head, destination=member['destination'])
    plan = state['plan']
    required = ['resolution', 'tests'] + (['reproduction'] if data['kind'] == 'bug' else [])
    if data['surface'] == 'ui':
        required.append('screenshot')
    for name in required:
        autopilot._check_spec(plan, name)
    return {'task': str(task), 'task_sha256': cli.digest(task),
            'state_sha256': cli.digest(task.with_name('autopilot.json')),
            'plan_sha256': member['sha256'], 'destination': member['destination'],
            'base': data['base_commit'], 'head': head, 'settings': settings,
            'review': {'path': review['path'], 'sha256': review['sha256']}}


def _copy_inputs(original, child):
    names = ('issue.txt', 'input-base.txt', 'input-stage.json')
    if cli.read(original).get('model_profile') is not None:
        names += (model_profiles.FILE,)
    for name in names:
        (child.parent / name).write_bytes(_regular(original.parent / name).read_bytes())
        (child.parent / name).chmod(0o600)
    inputs = original.parent / 'inputs'
    if inputs.exists():
        manifest = _regular(inputs / 'manifest.json')
        files = json.loads(manifest.read_bytes())
        root = child.parent / 'inputs'
        root.mkdir(mode=0o700)
        names = [item['file'] for item in files['logs'] + files.get('screenshots', [])]
        if any(not re.fullmatch(r'(?:log-[1-8]\.txt|screenshot-[1-4]\.(?:png|jpg|webp))', name) for name in names):
            raise ReplayError('invalid original input attachment name')
        for name in ['manifest.json', *names]:
            path = root / name
            path.write_bytes(_regular(inputs / name).read_bytes())
            path.chmod(0o600)


def validate_child(task, data, state):
    """Recheck the frozen replay identity without accepting the prior verdict."""
    binding = data.get('baseline_replay')
    if not isinstance(binding, dict) or binding != state.get('baseline_replay'):
        raise ReplayError('baseline replay binding changed')
    original = Path(binding['origin']['task'])
    old = cli.read(_regular(original))
    old_state = cli.read(_regular(original.with_name('autopilot.json')))
    source_review = binding['origin']['review']
    if (cli.digest(original) != binding['origin']['task_sha256']
            or cli.digest(original.with_name('autopilot.json')) != binding['origin']['state_sha256']
            or cli.digest(_regular(Path(source_review['path']))) != source_review['sha256']
            or any(data.get(key) != old.get(key) for key in CONTRACT_FIELDS)
            or data['base_commit'] != binding['base'] or state['plan'] != old_state['plan']
            or start._read_input_stage(task) != start._read_input_stage(original)
            or (state['phase'] not in ('reproduce', 'implement', 'adopting', 'verify', 'delivery'))
            or (state['phase'] in ('verify', 'delivery') and state.get('candidate_commit') != binding['candidate'])):
        raise ReplayError('replayed candidate lost its original inputs, checks, or limits')
    for key in ('mode', 'files', 'reason'):
        if (data.get('local_gate') or {}).get(key) != (old.get('local_gate') or {}).get(key):
            raise ReplayError('replayed candidate changed the original local check policy')
    reference = binding['patch']
    receipt = phase_replay.validate(reference, repo=data['repo'], **binding['operands'])
    if (receipt['candidate'] != binding['candidate'] or receipt['new_base'] != binding['base']
            or receipt['old_head'] != binding['origin']['head']
            or receipt['old_base'] != binding['origin']['base']
            or str(task.parent / 'worktree') != data['repo']):
        raise ReplayError('replayed worktree no longer matches the exact source patch')


def _prepare(root, original, old, old_state, stage, binding):
    child = root / 'task.json'
    workspace = root / 'worktree'
    _git(old['repo'], 'worktree', 'add', '--detach', str(workspace), binding['base'],
         timeout=autopilot._budget_remaining(old))
    _copy_inputs(original, child)
    if start._read_input_stage(child) != stage:
        raise ReplayError('copied inputs changed while preparing the replay')
    data = {'schema': cli.SCHEMA, **{key: old[key] for key in CONTRACT_FIELDS if key in old},
            'repo': str(workspace), 'base_commit': binding['base'], 'baseline_replay': binding,
            'status': 'paused', 'events': [{'at': cli.now(), 'type': 'baseline-replay', 'binding': binding}],
            'evidence': []}
    if old.get('local_gate'):
        policy = old['local_gate']
        local_gate._select(data, binding['candidate'], policy['files'], policy['reason'])
    state = {'schema': cli.SCHEMA, 'phase': 'reproduce' if data['kind'] == 'bug' else 'implement',
             'issue_digest': stage['request_digest'], 'input_digest': stage['input_digest'],
             'plan': old_state['plan'], 'baseline_replay': binding}
    if data.get('model_profile') is not None:
        model_profiles.intake(child, data, stage)
        state['model_profile'] = data['model_profile']
    cli.write(child.with_name('autopilot.json'), state)
    cli.write(child, data)
    return child


def _verify(root, original, member, queue_sha, base, *, fetch, execute, resume, owner):
    review_path = root / 'result.json'
    original_plan = autopilot_full._read_pinned(Path(member['plan']), member['sha256'])
    old, stage = autonomous_run._assert_task(original, original_plan, {})
    old_state = cli.read(_regular(original.with_name('autopilot.json')))
    snapshot = _snapshot(original, old, stage, old_state, member)
    operands = {'trunk': snapshot['base'], 'old_base': snapshot['base'], 'old_head': snapshot['head'],
                'new_base': base, 'files': _paths(old['repo'], snapshot['base'], snapshot['head'],
                                                autopilot._budget_remaining(old))}
    child = root / 'task.json'
    origin = {'queue_sha256': queue_sha, **snapshot}
    patch_path = root / 'patch' / 'result.json'

    def result(status, **details):
        report = {'schema': cli.SCHEMA, 'status': status, 'origin': origin, 'base': base, **details}
        cli.write(review_path, report)
        return {**report, 'report': str(review_path), 'sha256': cli.digest(review_path)}

    def active():
        run_control.admit(original, ReplayError)
        autopilot._budget_remaining(old)

    try:
        active()
        if owner is not None and owner.branch(member['destination'], 'main') != base:
            raise ReplayError('the destination main changed before baseline replay')
        if patch_path.exists():
            patch = cli.read(_regular(patch_path))
        else:
            if fetch:
                fetch(old['repo'], member['destination'], base, autopilot._budget_remaining(old))
            patch = phase_replay.prepare(old['repo'], **operands, out=patch_path.parent,
                                         timeout=autopilot._budget_remaining(old))
        if patch['status'] != 'replayed':
            return result('blocked', reason=patch.get('reason', 'Git replay blocked'), patch=str(patch_path))
        reference = {'path': str(patch_path), 'sha256': cli.digest(patch_path)}
        binding = {'origin': origin, 'base': base, 'candidate': patch['candidate'], 'patch': reference,
                   'operands': operands}
        active()
        if not child.exists():
            child = _prepare(root, original, old, old_state, stage, binding)
        with task_lock(child, reentrant=False):
            state_path = child.with_name('autopilot.json')
            state = cli.read(_regular(state_path))
            data = cli.read(_regular(child))
            if data.get('baseline_replay') != binding:
                raise ReplayError('retained replay belongs to another candidate or queue approval')
            validate_child(child, data, state)
            if state['phase'] != 'delivery':
                if data['status'] == 'running':
                    if (not autopilot._clean(Path(data['repo'])) or
                            cli.sha(data['repo'], 'HEAD') not in (
                                (base, binding['candidate']) if state['phase'] == 'adopting' else
                                (binding['candidate'],) if state['phase'] == 'verify' else (base,))):
                        raise ReplayError('interrupted replay worktree has unrecorded changes')
                    autopilot._pause(child, 'resuming a clean replay checkpoint')
                active()

                def guarded_run(task, issue, image, **options):
                    def guarded_model(*args, **kwargs):
                        active()
                        return autopilot.execute_model(*args, **kwargs)

                    def guarded_check(*args, **kwargs):
                        active()
                        return workflow.run_check(*args, **kwargs)

                    return execute(task, issue, image, phase_active=active, model=guarded_model,
                                   check=guarded_check, **options)

                resume(child, run=guarded_run)
            data, state = cli.read(child), cli.read(state_path)
            validate_child(child, data, state)
            head, problems = cli.evidence_gate(data, binding['candidate'])
            if state['phase'] != 'delivery' or head != binding['candidate'] or problems:
                raise ReplayError('fresh replay acceptance failed: ' + '; '.join(problems))
            current_review = delivery._independent_verification(data, head, destination=member['destination'])
            active()
            if owner is not None and owner.branch(member['destination'], 'main') != base:
                raise ReplayError('the destination main changed after fresh verification')
            return result('verified', task=str(child), candidate=head, patch=reference,
                          review={'path': current_review['path'], 'sha256': current_review['sha256']},
                          criteria_sha256=cli.criteria_digest(data))
    except ERRORS as error:
        held = run_control.read(original)
        return result('held' if held and held['status'] == 'held' else 'blocked',
                      reason=str(error)[:1800], task=str(child))


def verify(plan, sha256, task, base, *, out, owner=None, fetch=None, execute=autopilot.run, resume=start.resume_task):
    path, queue = autopilot_full._load(plan, sha256)
    original = autonomous_run._task(task)
    member = next((row for row in queue['members'] if row['task'] == str(original)), None)
    if not member or not isinstance(base, str) or not phase_replay.COMMIT.fullmatch(base) or base == member['base']:
        raise ReplayError('select an enrolled task and exact changed main SHA')
    root = Path(out).expanduser().resolve()
    if (root.is_relative_to(original.parent)
            or any(root.is_relative_to(Path(cli.read(Path(row['task']))['repo']).resolve()) for row in queue['members'])):
        raise ReplayError('keep replay records separate from member worktrees and task directories')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = root / '.replay'
    if marker.is_symlink():
        raise ReplayError('replay lock must use a regular file')
    marker.touch(mode=0o600, exist_ok=True)
    with task_lock(marker, lock_name='.replay.lock', reentrant=False), task_lock(original, reentrant=False):
        path = root / 'replay-input.json'
        expected = {'schema': cli.SCHEMA, 'queue_sha256': sha256, 'task': str(original), 'base': base}
        if path.exists() or path.is_symlink():
            if cli.read(_regular(path)) != expected:
                raise ReplayError('replay directory is already bound to another task, queue, or base')
        else:
            cli.write(path, expected)
        if isinstance(owner, delivery.GitHub):
            owner.set_task_deadline(cli.read(original))
        return _verify(root, original, member, sha256, base, fetch=fetch, owner=owner,
                       execute=execute, resume=resume)


def observe(plan, sha256, member, *, owner=None, verify_on_base=verify):
    """Start fresh verification when the selected fork really moved past the approved base."""
    destination = member['destination']
    task = Path(member['task'])
    if not destination or member['predicate'] not in ('published', 'merged'):
        return None
    with task_lock(task, reentrant=False):
        old = cli.read(task)
        state = autonomous_run._state(task)
        if old['status'] != 'paused' or state['phase'] != 'delivery':
            return None
        run_control.admit(task, ReplayError)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', destination):
            raise ReplayError('invalid destination for main observation')
        owner = owner or delivery.GitHub()
        if isinstance(owner, delivery.GitHub):
            owner.set_task_deadline(old)
        base = owner.branch(destination, 'main')
    if base == member['base']:
        return None
    if not isinstance(base, str) or not phase_replay.COMMIT.fullmatch(base):
        raise ReplayError('cannot confirm the exact new main')
    parent = Path(plan).parent / 'replays' / member['id']
    if not (parent / base).exists() and parent.exists() and len(list(parent.iterdir())) >= 2:
        raise ReplayError('main advanced again after two replay attempts; inspect the queue')
    return verify_on_base(plan, sha256, task, base, out=parent / base, owner=owner, fetch=_fetch_main)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'sha256', 'task', 'base', 'out'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify(**vars(args))
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result['status'] == 'blocked')


if __name__ == '__main__':
    sys.exit(main())
