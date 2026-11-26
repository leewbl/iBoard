"""Execute an approved multi-phase plan with separate verification for each phase."""

import argparse
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
from pathlib import Path
import subprocess
import uuid

from workbench import autopilot, cli, delivery, phase_contract, phase_runtime, start, visual_parity, workflow
from workbench.task_lock import task_lock


class DispatchError(ValueError):
    pass


ERRORS = (OSError, ValueError, KeyError, TypeError, cli.WorkbenchError, start.StartError,
          autopilot.AutopilotError, workflow.WorkflowError, delivery.DeliveryError, subprocess.SubprocessError)


def _reference(path):
    return {'path': str(path), 'sha256': cli.digest(path)}


def _task_id(root, phase_id):
    identity = hashlib.sha256(f'{root}\n{phase_id}'.encode()).hexdigest()[:12]
    return f'phase-{identity}-{phase_id}'


def _bound_task(task, contract, entry, parent, baseline_perf):
    data = cli.read(task)
    phase = phase_contract.read(data)
    if (not phase or phase['contract']['manifest_sha256'] != contract['manifest_sha256']
            or phase['entry']['id'] != entry['id'] or phase['parent'] != parent
            or phase['baseline_perf'] != baseline_perf):
        raise DispatchError('retained task does not match the approved phase or baseline evidence')
    return data


def _verified(task, data, entry):
    state = cli.read(task.parent / 'autopilot.json')
    if state.get('phase') not in ('delivery', 'complete') or not data.get('phase_result'):
        raise DispatchError('phase has no final candidate and technical acceptance evidence')
    head, errors = cli.evidence_gate(data, state['candidate_commit'])
    if errors:
        raise DispatchError('phase task evidence gate failed: ' + '; '.join(errors))
    result = phase_contract.verify_completion(data, head)
    proof = delivery._independent_verification(data, head)
    approved = not entry['interaction'] or phase_contract.interaction_approved(data, head)
    row = {**result, 'status': 'verified' if approved else 'awaiting-operator-review',
           'task': str(task), 'result': data['phase_result'],
           'independent_review': {'path': proof['path'], 'sha256': proof['sha256']}}
    if data.get('visual_baseline'):
        row['visual_baseline'] = data['visual_baseline']
    return row


def _run(root, contract, launch, resume, measure, execute, capture):
    journal = root / 'run.json'
    state = (cli.read(journal) if journal.exists() else {'schema': cli.SCHEMA,
             'manifest_sha256': contract['manifest_sha256'], 'started_at': cli.now(), 'phases': {}})
    if state.get('manifest_sha256') != contract['manifest_sha256']:
        raise DispatchError('the approved dispatch changed after the run began')
    deadline = datetime.fromisoformat(state['started_at']).timestamp() + contract['seconds']
    cli.write(journal, state)

    def remaining():
        seconds = int(deadline - datetime.now(timezone.utc).timestamp())
        if seconds < 1:
            raise DispatchError('the overall multi-phase time limit expired')
        if cli.digest(contract['manifest_path']) != contract['manifest_sha256']:
            raise DispatchError('the approved dispatch changed during execution')
        return seconds

    def record(key, **fields):
        state['phases'].setdefault(key, {}).update(fields)
        cli.write(journal, state)

    phases = {}
    baseline = contract['plan']['baseline']
    entries = {entry['id']: entry for entry in contract['plan']['phases']}
    ancestors = {}

    def report(status):
        path = root / 'result.json'
        value = {'schema': cli.SCHEMA, 'status': status, 'baseline': baseline,
                 'manifest_sha256': contract['manifest_sha256'], 'phases': phases, 'report': str(path)}
        cli.write(path, value)
        return value

    def bound(task, key, parent):
        saved = state['phases'][key]
        data = _bound_task(task, contract, entries[key], parent, saved['baseline_perf'])
        if data.get('visual_baseline') != saved.get('visual_baseline'):
            raise DispatchError('retained phase does not match the original dispatch visual baseline')
        return data

    visual_cutoff = state.get('visual_frozen_at')
    work_started = any('task' in row for row in state['phases'].values()) or any(
        (root / 'tasks' / _task_id(root, ident) / 'task.json').exists() for ident in entries)
    has_visual = any('visual_parity' in step for step in contract['steps'].values())
    for key, step in contract['steps'].items():
        specification = step.get('visual_parity')
        if specification is None:
            continue
        task_id = _task_id(root, key)
        stage = root / 'phases' / task_id
        ref = state['phases'].get(key, {}).get('visual_baseline')
        try:
            remaining()
            if not ref:
                if work_started or visual_cutoff is not None:
                    raise DispatchError('a visual baseline is missing after component work began')
                ref = capture(contract['repo'], baseline, step['image'], Path(specification['path']),
                              stage / f'visual-baseline-{uuid.uuid4().hex}', remaining(), contract['memory_mb'])
            if not Path(ref['path']).resolve(strict=True).is_relative_to(stage):
                raise DispatchError('visual baseline escape from its phase evidence directory')
            if work_started and visual_cutoff is None:
                raise DispatchError('the original visual capture cutoff is missing after component work began')
            phase_contract.verify_visual_baseline(contract, key, ref, before=visual_cutoff or cli.now())
            record(key, visual_baseline=ref)
        except ERRORS as error:
            phases[key] = {'status': 'blocked', 'reason': str(error)[:1800]}
            return report('blocked')
    if has_visual and visual_cutoff is None:
        state['visual_frozen_at'] = cli.now()
        cli.write(journal, state)

    def recheck_dependencies(key):
        for name in entries:
            if name not in ancestors[key]:
                continue
            expected = phases[name]
            task_path = Path(expected['task'])
            parent = expected['parent']
            with task_lock(task_path):
                data = bound(task_path, name, parent)
                if {'parent': parent, **_verified(task_path, data, entries[name])} != expected:
                    raise DispatchError(f'phase dependency {name} changed before dispatch')

    for key, entry in entries.items():
        ancestors[key] = set(entry['depends_on'])
        for name in entry['depends_on']:
            ancestors[key].update(ancestors[name])
        waits = [name for name in entry['depends_on'] if phases[name]['status'] != 'verified']
        if waits:
            phases[key] = {'status': 'waiting-dependencies', 'waits': waits}
            continue
        parent = baseline if entry['base'] == 'trunk' else phases[entry['base']]['commit']
        task_id = _task_id(root, key)
        task = root / 'tasks' / task_id / 'task.json'
        stage = root / 'phases' / task_id
        step = contract['steps'][key]
        saved = state['phases'].get(key, {})
        perf = saved.get('baseline_perf')
        visual = saved.get('visual_baseline')
        try:
            if saved.get('parent', parent) != parent:
                raise DispatchError('declared phase parent changed after dispatch')
            if not perf:
                if task.exists():
                    raise DispatchError('task exists without a retained pre-implementation benchmark')
                stage.mkdir(mode=0o700, parents=True, exist_ok=True)
                perf = measure(contract, key, stage / f'trunk-{uuid.uuid4().hex}', timeout=remaining())
                record(key, parent=parent, baseline_perf=perf)
            phase_contract.verify_baseline(contract, key, perf)
            if task.exists():
                data = bound(task, key, parent)
                retained = task.parent / 'autopilot.json'
                status = cli.read(retained).get('phase') if retained.exists() else None
                if status not in ('delivery', 'complete'):
                    recheck_dependencies(key)
                    remaining()
                    task_deadline = datetime.fromisoformat(data['started_at']).timestamp() + data['limits']['seconds']
                    if task_deadline > deadline + 1:
                        raise DispatchError('phase task budget exceeds the approved overall deadline')
                    with task_lock(task):
                        bound(task, key, parent)
                        autopilot._pause(task, 'the phase dispatcher is resuming retained work')
                        resume(task, run=execute)
            else:
                stage.mkdir(mode=0o700, parents=True, exist_ok=True)
                issue = stage / 'issue.txt'
                approved_request = phase_contract.request(contract, key).encode('utf-8')
                if issue.exists():
                    if issue.is_symlink() or issue.read_bytes() != approved_request:
                        raise DispatchError('phase request changed before launch')
                else:
                    issue.write_bytes(approved_request)
                    issue.chmod(0o600)

                def drive(task_path, request_path, image, **options):
                    data = cli.read(task_path)
                    if step.get('local_gate'):
                        from workbench import local_gate

                        try:
                            local_gate._select(data, parent, **step['local_gate'])
                        except local_gate.LocalGateError as error:
                            raise DispatchError(str(error)) from error
                    phase_contract.bind(task_path, data, contract, key, parent, request_path, perf,
                                        visual_baseline=visual)
                    cli.write(task_path, data)
                    return execute(task_path, request_path, image, **options)

                record(key, parent=parent, task=str(task), request=_reference(issue))
                recheck_dependencies(key)
                launch(task_id=task_id, repo=contract['repo'], issue_file=issue,
                       source=f"iBoard multi-phase plan {contract['plan_sha256'][:12]} phase {key}",
                       kind='feature', surface='ui' if step.get('screenshot') else 'non-ui',
                       iterations=contract['iterations'], seconds=remaining(), image=step['image'],
                       tasks_root=root / 'tasks', workspaces_root=root / 'workspaces',
                       source_mode='image', build_source_image=True, memory_mb=contract['memory_mb'],
                       criteria=phase_contract.criteria(entry),
                       prepare=partial(workflow.prepare, base_commit=parent), run=drive)
            recheck_dependencies(key)
            with task_lock(task):
                data = bound(task, key, parent)
                phases[key] = {'parent': parent, **_verified(task, data, entry)}
        except ERRORS as error:
            phases[key] = {'status': 'blocked', 'task': str(task), 'parent': parent, 'reason': str(error)[:1800]}
        record(key, result=phases[key])
    statuses = {row['status'] for row in phases.values()}
    status = 'blocked' if 'blocked' in statuses else 'verified' if statuses == {'verified'} else 'waiting'
    return report(status)


def run(manifest, expected_sha256, out, *, launch=start.start, resume=start.resume_task,
        measure=phase_runtime.baseline, execute=autopilot.run, capture=visual_parity.capture_baseline):
    contract = phase_contract.load(manifest, expected_sha256)
    root = Path(out).expanduser().resolve()
    if root.is_relative_to(Path(contract['repo'])):
        raise DispatchError('keep multi-phase dispatch evidence outside the source checkout')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = root / '.dispatch'
    if lock.is_symlink():
        raise DispatchError('dispatch lock anchor cannot be a symbolic link')
    lock.touch(mode=0o600, exist_ok=True)
    with task_lock(lock, lock_name='.phase-dispatch.lock'):
        return _run(root, contract, launch, resume, measure, execute, capture)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    dispatch = commands.add_parser('run')
    for name in ('manifest', 'sha256', 'out'):
        dispatch.add_argument('--' + name, required=True)
    approve = commands.add_parser('approve')
    for name in ('task', 'candidate', 'result-sha256', 'approved-by', 'reason'):
        approve.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'approve':
            report = phase_contract.approve_interaction(args.task, expected_candidate=args.candidate,
                                                        expected_result_sha256=args.result_sha256,
                                                        approved_by=args.approved_by, reason=args.reason)
        else:
            report = run(args.manifest, args.sha256, args.out)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(args.command == 'run' and report['status'] != 'verified')


if __name__ == '__main__':
    raise SystemExit(main())
