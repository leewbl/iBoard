"""Drive an explicitly approved task to its frozen acceptance or delivery predicate."""

import argparse
from datetime import datetime
from functools import partial
import hashlib
import json
from pathlib import Path
import re
import time

from workbench import autopilot, cli, delivery, pickup, pr_watch, run_control, start
from workbench.task_lock import TaskBusy, task_lock


class RunError(ValueError):
    pass


SOURCE = 'skills/poteto-mode/playbooks/autonomous-run.md'
SAFE_RECOVERY = {'plan', 'reproduce', 'implement', 'verify'}
OUTCOMES = {'verified', 'published', 'merged'}
SNAPSHOT_FIELDS = (
    'id', 'repo', 'kind', 'surface', 'source', 'original_source', 'base_commit', 'started_at', 'limits',
    'publish_only', 'required_ci_workflows', 'ci_skip_policy', 'local_gate', 'impact_review',
    'verification_target', 'feature_context', 'phase_binding', 'visual_baseline', 'refactor_contract',
    'criterion_changes',
)
ERRORS = (RunError, cli.WorkbenchError, start.StartError, autopilot.AutopilotError,
          delivery.DeliveryError, pr_watch.WatchError, OSError, KeyError, TypeError, ValueError)


def _task(path):
    task = Path(path).expanduser().resolve(strict=True)
    if task.name != 'task.json' or not task.is_file():
        raise RunError('select an existing iBoard task.json')
    return task


def _binding(task, data):
    stage = start._read_input_stage(task)
    if not data.get('criteria'):
        raise RunError('record explicit acceptance criteria before freezing the autonomous run')
    if any(type(data['limits'].get(key)) is not int or data['limits'][key] < 1
           for key in ('seconds', 'iterations')):
        raise RunError('autonomous execution requires positive original time and iteration limits')
    fields = {key: data.get(key) for key in SNAPSHOT_FIELDS}
    for key in ('root_cause', 'coverage_swarm'):
        if key in data:
            fields[key] = data[key]
    fields.update(criteria_sha256=cli.criteria_digest(data), base_digest=stage['base_digest'],
                  input_digest=stage['input_digest'], settings=stage['run_settings'])
    return fields, stage


def _supported(data, destination):
    if data.get('verification_target') or not destination:
        return 'verified'
    return 'published' if data.get('publish_only') else 'merged'


def freeze(task, predicate, out, *, interval=30):
    task = _task(task)
    if predicate not in OUTCOMES or type(interval) is not int or not 1 <= interval <= 3600:
        raise RunError('choose a named outcome and a heartbeat from 1 to 3600 seconds')
    root = Path(out).expanduser().resolve()
    with task_lock(task, reentrant=False):
        data = cli.read(task)
        binding, stage = _binding(task, data)
        if data['status'] != 'paused' or cli.enforce_budget(data):
            raise RunError('freeze a paused task within its existing budget')
        if predicate != _supported(data, stage['run_settings']['pr_repo']):
            raise RunError('the exit predicate does not match the task’s existing delivery authorization')
        if root.is_relative_to(Path(data['repo']).resolve()) or root.is_relative_to(task.parent / 'inputs'):
            raise RunError('keep supervision records outside the worktree and frozen input directory')
        request = stage.get('request_digest') if stage['phase'] == 'ready' else None
        plan = {'schema': cli.SCHEMA, 'source': SOURCE, 'task': str(task), 'predicate': predicate,
                'interval': interval, 'binding': binding, 'request_digest': request}
        root.mkdir(mode=0o700, parents=True)
        path = root / 'plan.json'
        cli.write(path, plan)
    return {'plan': str(path), 'sha256': cli.digest(path), 'predicate': predicate}


def _state(task):
    path = task.with_name('autopilot.json')
    return cli.read(path) if path.exists() else {'phase': 'plan'}


def _assert_task(task, plan, journal):
    data = cli.read(task)
    binding, stage = _binding(task, data)
    if binding != plan['binding']:
        raise RunError('task inputs, acceptance conditions, delivery policy, or limits changed')
    if plan['request_digest'] is not None and stage.get('request_digest') != plan['request_digest']:
        raise RunError('prepared request changed')
    if journal.get('request_digest') is not None and stage.get('request_digest') != journal['request_digest']:
        raise RunError('prepared request changed after the first supervised attempt')
    return data, stage


def _ci_pin(state, data, destination):
    pin = state.get('ci_wait')
    if pin is None:
        return None
    publication = state.get('published_pr') or {}
    expected = {'repo': destination, 'pr': publication.get('pr'), 'head': state.get('candidate_commit'),
                'base': data['base_commit']}
    if (not destination or state['phase'] != 'delivery' or pin != expected
            or type(expected['pr']) is not int or expected['pr'] < 1
            or publication.get('repo') != destination or publication.get('head') != expected['head']
            or not pickup._sha(expected['head']) or state.get('ci_republish') is not None):
        raise RunError('the pending CI observation no longer matches the recorded publication')
    return pin


def _remote(reader, destination, number, head, base, data, *, states, branch=None):
    if isinstance(reader, delivery.GitHub):
        reader.set_task_deadline(data)
    pull = pr_watch._facts(reader, destination, number)
    if (pull['state'] not in states or pull['isDraft'] or pull['isCrossRepository']
            or pull['headRefOid'] != head or (pull['state'] == 'OPEN' and pull['baseRefOid'] != base)
            or (branch is not None and (pull['headRefName'] != branch or pull['baseRefName'] != 'main'))):
        raise RunError('GitHub no longer confirms the exact requested PR and candidate')
    return {'repo': destination, 'pr': number, 'head': head, 'base': base, 'observed_base': pull['baseRefOid'],
            'state': pull['state']}


def _proof(plan, data, state, reader):
    predicate, phase = plan['predicate'], state['phase']
    destination = plan['binding']['settings']['pr_repo']
    target = data.get('verification_target')
    if predicate == 'verified' and not target:
        if phase not in ('delivery', 'complete'):
            return None
    elif phase != 'complete':
        return None

    head = state.get('candidate_commit')
    if not pickup._sha(head):
        raise RunError('the completed phase has no recorded candidate commit')
    actual, problems = cli.evidence_gate(data, head)
    if actual != head or problems:
        raise RunError('the recorded candidate no longer passes the evidence gate: ' + '; '.join(problems))
    item = delivery._independent_verification(data, head, destination=destination)
    proof = {'candidate': head, 'base': data['base_commit'], 'criteria_sha256': cli.criteria_digest(data),
             'review': {'path': item['path'], 'sha256': item['sha256']}}
    if target:
        record = state.get('verification') or {}
        if (record.get('verdict') != 'PASS' or record.get('head') != head or record.get('base') != proof['base']
                or record.get('review') != item['path'] or record.get('review_sha256') != item['sha256']
                or target.get('pr') != record.get('pr') or record.get('repo') != destination):
            raise RunError('the retained verification does not prove the exact requested PR')
        proof['remote'] = _remote(reader, destination, record['pr'], head, proof['base'], data, states={'OPEN'})
    elif predicate in ('published', 'merged'):
        record = state.get('delivery' if predicate == 'merged' else 'published_pr') or {}
        recorded_head = record.get('verified_head' if predicate == 'merged' else 'head')
        if (record.get('repo') != destination or recorded_head != head
                or type(record.get('pr')) is not int or record['pr'] < 1
                or (predicate == 'merged' and record.get('merged') is not True)):
            raise RunError('the required delivery receipt is missing or no longer matches the candidate')
        proof['remote'] = _remote(reader, destination, record['pr'], head, proof['base'], data,
                                  states={'MERGED'} if predicate == 'merged' else {'OPEN'},
                                  branch=f"workbench/{data['id']}")
    return proof


def _attached_run(plan_path, plan_digest, *, once, reader, resume, observe_ci, sleep):
    raw = plan_path.read_bytes()
    plan = json.loads(raw)
    if (hashlib.sha256(raw).hexdigest() != plan_digest or not isinstance(plan, dict)
            or plan.get('schema') != cli.SCHEMA or plan.get('source') != SOURCE
            or plan.get('predicate') not in OUTCOMES
            or type(plan.get('interval')) is not int or not 1 <= plan['interval'] <= 3600):
        raise RunError('the autonomous plan or its approved SHA-256 is invalid')
    task = _task(plan['task'])
    journal_path = plan_path.with_name('run.json')
    journal = cli.read(journal_path) if journal_path.exists() else {
        'schema': cli.SCHEMA, 'plan_sha256': plan_digest, 'attempts': 0, 'events': [],
    }
    if journal.get('plan_sha256') != plan_digest:
        raise RunError('retained supervision record belongs to a different plan')
    destination = plan['binding']['settings']['pr_repo']
    deadline = datetime.fromisoformat(plan['binding']['started_at']).timestamp() + plan['binding']['limits']['seconds']

    def record(status, **fields):
        event = {'status': status, **fields}
        before = journal.get('last_observation')
        if event != before:
            journal['events'].append({'at': cli.now(), **event})
            journal['last_observation'] = event
        if status == 'blocked':
            journal['blocked_reason'] = fields['reason']
        cli.write(journal_path, journal)
        return {'schema': cli.SCHEMA, 'predicate': plan['predicate'], 'task': str(task), 'journal': str(journal_path),
                'attempts': journal['attempts'], **event}

    def tick():
        if cli.digest(plan_path) != plan_digest:
            raise RunError('the approved plan changed during supervision')
        if time.time() >= deadline:
            return record('blocked', reason='original task deadline reached')
        held = run_control.read(task)
        if held and held['status'] == 'held':
            return record('held', request=held['request'])
        if 'blocked_reason' in journal:
            return record('blocked', reason=journal['blocked_reason'])

        # Observe a busy task without assuming that the supervisor owns its process.
        with task_lock(task, reentrant=False):
            data, stage = _assert_task(task, plan, journal)
            state = _state(task)
            proof = _proof(plan, data, state, reader)
            if proof is not None:
                journal['in_flight'] = False
                return record('complete', proof=proof, iterations=data['iterations'])
            pin = _ci_pin(state, data, destination)
            if data['status'] == 'running':
                if not journal.get('in_flight') or (state['phase'] not in SAFE_RECOVERY and pin is None):
                    raise RunError('idle task has an unresolved operation; inspect before resuming')
                expected = data['base_commit'] if state['phase'] in pickup.BASELINE else state.get('candidate_commit')
                if (cli.sha(data['repo'], 'HEAD', timeout=12) != expected
                        or cli.git(data['repo'], '--no-optional-locks', 'status', '--porcelain',
                                   '--untracked-files=all', timeout=12)):
                    raise RunError('interrupted task has changes outside its recorded checkpoint')
                autopilot._pause(task, 'recovered an interrupted supervisor at an unchanged local checkpoint')
                data = cli.read(task)
                record('recovered', phase=state['phase'])
            elif journal.get('in_flight') and not state.get('ci_wait') and state['phase'] != 'complete':
                last = data.get('events', [])[-1:]
                released = (last and last[0].get('type') == 'pause'
                            and run_control.released_pause(task, last[0].get('reason', '')))
                if not released:
                    # A killed supervisor may have left a local verified checkpoint just before reporting.
                    if not (not destination and plan['predicate'] == 'verified' and state['phase'] == 'delivery'):
                        raise RunError('previous supervised attempt stopped without a resumable receipt')
            inspection = pickup._inspect(task, 'idle', None)
            if inspection['execution'] != 'paused':
                raise RunError('unsafe task checkpoint: ' + '; '.join(inspection.get('blockers', [])))
            checkpoint = cli.digest(task)
            phase = state['phase']
        if pin:
            try:
                ready = observe_ci(task, destination, pin['pr'], pin['head'], github=reader, once=True, observe=True)
            except autopilot.CIFailure:
                ready = True   # Let the worker reacquire and verify failure evidence itself.
            if not ready:
                journal['in_flight'] = False
                return record('waiting-ci', identity=pin)

        with task_lock(task, reentrant=False):
            run_control.admit(task, RunError)
            current, stage = _assert_task(task, plan, journal)
            state = _state(task)
            if cli.digest(task) != checkpoint or state['phase'] != phase or _ci_pin(state, current, destination) != pin:
                return record('checkpoint-changed')
            if stage['phase'] == 'ready':
                journal['request_digest'] = stage['request_digest']
            journal['in_flight'] = True
            journal['attempts'] += 1
            record('resume', phase=phase, identity=pin)
            resume(task, run=partial(autopilot.run, ci_once=True, local_handoff=not destination))
            data, stage = _assert_task(task, plan, journal)
            if stage['phase'] == 'ready':
                journal['request_digest'] = stage['request_digest']
            journal['in_flight'] = False
            state = _state(task)
            proof = _proof(plan, data, state, reader)
            if proof is not None:
                return record('complete', proof=proof, iterations=data['iterations'])
            pin = _ci_pin(state, data, destination)
            if pin:
                return record('waiting-ci', identity=pin)
            raise RunError('the attempt returned without its frozen result or a CI checkpoint')

    while True:
        try:
            result = tick()
        except TaskBusy:
            result = record('active')
        except ERRORS as error:
            held = None
            if not isinstance(error, run_control.ControlError):
                try:
                    held = run_control.read(task)
                except (run_control.ControlError, OSError):
                    pass
            if held and held['status'] == 'held':
                result = record('held', request=held['request'])
            else:
                result = record('blocked', reason=str(error)[:1400])
        if once or result['status'] in ('complete', 'blocked', 'held'):
            return result
        sleep(min(plan['interval'], max(0, deadline - time.time())))


def run(plan, sha256, *, once=False, reader=None, resume=start.resume_task, observe_ci=autopilot._wait_for_ci,
        sleep=time.sleep):
    plan = Path(plan).expanduser().resolve(strict=True)
    if not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256):
        raise RunError('provide the original frozen plan SHA-256')
    with task_lock(plan, lock_name='.autonomous-run.lock', reentrant=False):
        return _attached_run(plan, sha256, once=once, reader=reader or pr_watch.GitHubReader(),
                             resume=resume, observe_ci=observe_ci, sleep=sleep)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    prepare = sub.add_parser('freeze')
    prepare.add_argument('--task', required=True)
    prepare.add_argument('--predicate', required=True, choices=sorted(OUTCOMES))
    prepare.add_argument('--out', required=True)
    prepare.add_argument('--interval', type=int, default=30)
    execute = sub.add_parser('run')
    execute.add_argument('--plan', required=True)
    execute.add_argument('--sha256', required=True)
    execute.add_argument('--once', action='store_true', help='evaluate one supervision tick')
    args = vars(parser.parse_args(argv))
    mode = args.pop('mode')
    try:
        result = freeze(**args) if mode == 'freeze' else run(**args)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result.get('status') == 'blocked')


if __name__ == '__main__':
    raise SystemExit(main())
