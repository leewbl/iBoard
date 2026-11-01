"""Replace a pre-implementation reproduction plan without changing its acceptance contract."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from workbench import autopilot, cli
from workbench.task_lock import task_lock


class ReplanError(Exception):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def _contract(task, data, state):
    stage_path = task.parent / 'input-stage.json'
    if stage_path.is_symlink() or not stage_path.is_file():
        raise ReplanError('retained input stage is missing or unsafe')
    stage = cli.read(stage_path)
    settings = stage.get('run_settings')
    if stage.get('phase') != 'ready' or not isinstance(settings, dict):
        raise ReplanError('prepared input and retained execution settings are required')
    issue = task.parent / 'issue.txt'
    base = task.parent / 'input-base.txt'
    if any(p.is_symlink() or not p.is_file() for p in (issue, base)):
        raise ReplanError('retained request is missing or unsafe')
    request_digest = cli.digest(issue)
    input_digest = autopilot._input_digest(task)
    if (stage.get('request_digest') != request_digest or stage.get('base_digest') != cli.digest(base)
            or state.get('issue_digest') != request_digest
            or 'input_digest' not in state or state['input_digest'] != input_digest
            or stage.get('input_digest') != input_digest):
        raise ReplanError('retained request or input snapshot changed')
    if not data.get('criteria'):
        raise ReplanError('original acceptance criteria are required')
    contract = {'base_commit': data['base_commit'], 'issue_digest': request_digest,
                'input_digest': input_digest, 'criteria_digest': cli.criteria_digest(data),
                'run_settings_digest': _digest(settings)}
    if 'adoption' in data:
        if not isinstance(data['adoption'], dict):
            raise ReplanError('invalid adoption metadata')
        try:
            autopilot._validate_adoption(task, data, issue, settings['image'], settings['pr_repo'],
                settings['source_mode'], settings['candidate_image'], settings['build_source_image'],
                settings['memory_mb'])
        except (KeyError, autopilot.AutopilotError) as error:
            raise ReplanError('adoption settings or acceptance changed') from error
        # Preserve the original adoption contract shape for existing histories.
        contract['adoption_digest'] = _digest(data['adoption'])
    else:
        if data.get('kind') != 'bug' or data.get('surface') != 'non-ui':
            raise ReplanError('ordinary replacement requires a non-ui bug task')
        contract.update(kind='ordinary', base_digest=cli.digest(base),
                        stage_digest=cli.digest(stage_path))
    return contract


def validate_binding(task, data, state, *, run_settings=None):
    """Recheck a replacement at every run, including later verification/delivery."""
    revisions = state.get('plan_revisions')
    if revisions is None:
        return
    if not isinstance(revisions, list) or not revisions:
        raise ReplanError('invalid plan replacement history')
    contract = _contract(task, data, state)
    if run_settings is not None and _digest(run_settings) != contract['run_settings_digest']:
        raise ReplanError('replacement execution settings changed')
    prior = None
    for entry in revisions:
        if (not isinstance(entry, dict) or entry.get('contract') != contract
                or not isinstance(entry.get('reason'), str) or not entry['reason'].strip()
                or _digest(entry.get('previous_plan')) != entry.get('previous_plan_digest')
                or not isinstance(entry.get('new_plan_digest'), str)
                or (prior is not None and entry['previous_plan_digest'] != prior)):
            raise ReplanError('plan replacement history or bound contract changed')
        prior = entry['new_plan_digest']
    if _digest(state.get('plan')) != prior:
        raise ReplanError('active replacement plan changed')


def replace(task_path, plan, reason):
    task = Path(task_path).expanduser().resolve(strict=True)
    with task_lock(task):
        data = cli.read(task)
        state_path = task.parent / 'autopilot.json'
        if state_path.is_symlink() or (state_path.exists() and not state_path.is_file()):
            raise ReplanError('retained workflow state is unsafe')
        state_exists = state_path.exists()
        state = cli.read(state_path) if state_exists else {
            'schema': cli.SCHEMA, 'phase': 'unplanned',
            'issue_digest': cli.digest(task.parent / 'issue.txt'),
            'input_digest': autopilot._input_digest(task)}
        if (data.get('status') != 'paused' or data.get('kind') != 'bug'
                or data.get('surface') != 'non-ui' or state.get('phase') not in ('unplanned', 'plan', 'reproduce')
                or data.get('evidence') != [] or state.get('baseline_reports')
                or state.get('candidate_commit')):
            raise ReplanError('only paused unplanned or reproduction tasks without successful evidence can accept a plan')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            raise ReplanError('a bounded explicit replacement reason is required')
        if not autopilot._clean(Path(data['repo'])) or cli.sha(data['repo'], 'HEAD') != data['base_commit']:
            raise ReplanError('replacement requires the unchanged clean baseline')
        validate_binding(task, data, state)
        contract = _contract(task, data, state)
        if not isinstance(plan, dict) or plan.get('status') != 'ready':
            raise ReplanError('replacement must be a ready plan')
        if set(plan) - {'status', 'criteria', 'reproduction', 'resolution', 'tests'}:
            raise ReplanError('replacement cannot supply workflow state or evidence')
        if 'criteria' in plan and plan['criteria'] != data['criteria']:
            raise ReplanError('replacement cannot change original acceptance criteria')
        try:
            for name in ('reproduction', 'resolution', 'tests'):
                autopilot._check_spec(plan, name)
        except autopilot.AutopilotError as error:
            raise ReplanError(str(error)) from error
        if len(json.dumps(plan, ensure_ascii=False).encode()) > 1_000_000:
            raise ReplanError('replacement plan exceeds size limit')
        if state.get('phase') == 'reproduce' and _digest(plan) == _digest(state.get('plan')):
            return {'changed': False, 'phase': 'reproduce', 'plan_digest': _digest(plan)}
        revision = {'at': cli.now(), 'reason': reason.strip(), 'contract': contract,
                    'source_kind': 'explicit-supplied-plan', 'previous_phase': state['phase'],
                    'previous_plan': state.get('plan'), 'previous_plan_digest': _digest(state.get('plan')),
                    'new_plan_digest': _digest(plan)}
        state.setdefault('plan_revisions', []).append(revision)
        state['plan'] = plan
        state['phase'] = 'reproduce'
        # This single atomic publication is the canonical audit. No task status,
        # acceptance or runtime evidence is manufactured or superseded. The explicit
        # acceptance above is the reason an unplanned task enters reproduction.
        cli.write(state_path, state)
        return {'changed': True, 'phase': 'reproduce', 'plan_digest': _digest(plan)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--plan-file', required=True)
    parser.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    source = Path(args.plan_file)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 1_000_000:
        raise ReplanError('replacement plan file is missing or unsafe')
    print(json.dumps(replace(args.task, json.loads(source.read_text()), args.reason)))


if __name__ == '__main__':
    main()
