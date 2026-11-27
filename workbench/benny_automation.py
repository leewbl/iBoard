"""Admit signed Slack events and dispatch approved Benny work outside the receiver."""

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import time

from workbench import (autopilot, benny, benny_deploy, benny_draft, benny_events, benny_handoff, benny_operations,
                       benny_repair, benny_reproduce, benny_triage, cli, delivery, project_verify, publish, start)
from workbench.benny_slack import CHANNEL, STAMP, USER, Slack, SlackWriter
from workbench.task_lock import TaskBusy, task_lock


TRIAGE_OPTIONS = {'selections', 'terms', 'routing_map', 'seconds', 'model_name', 'read_attachments', 'inspect_media'}
REPRO_OPTIONS = {'image', 'manifest', 'helpers', 'journey', 'oracle', 'oracle_marker', 'oracle_failure_marker', 'memory_mb'}
VISUAL_OPTIONS = {'baseline_visible', 'candidate_visible'}
HANDOFF_SECONDS = 90
REPRO_CHECKPOINTS = {'opening-operations', 'reproducing', 'prepare-repair', 'repair', 'reporting-operations'}
OPERATIONS_START = {'verify-existing': 'Verifying existing fix', 'eligible-for-reproduction': 'Reproducing'}
OPERATIONS_RESULT = {'confirmed': 'Existing fix verified', 'reproduced': 'Reproduced', 'waiting-claim': 'Reproduced',
                     'not-reproduced': 'Could not reproduce', 'inconclusive': 'Could not reproduce',
                     'verified': 'Fix verified'}
ERRORS = (*project_verify.ERRORS, autopilot.AutopilotError, delivery.DeliveryError, publish.PublishError, start.StartError)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _binding(repo, rules):
    project = benny_deploy.verify(repo)
    if project.get('status') != 'ready' or not {'triage', 'reproduce'} <= set(project['roles']):
        raise ValueError('both committed Benny roles must pass deployment verification')
    benny_reproduce._repository(project['repo'], rules['repository'])
    return project, {**{key: project[key] for key in ('revision', 'manifest_sha256')},
                     'rules_sha256': _hash(rules)}


def _state(path, repo):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError('Benny event state requires a private directory')
    path = path.resolve()
    if path.is_relative_to(Path(repo).resolve()):
        raise ValueError('store Benny receipts outside the target checkout')
    return _private_directory(path, parents=True)


def _private_directory(path, *, parents=False):
    if path.is_symlink():
        raise ValueError('Benny receipts must remain in private directories')
    path.mkdir(mode=0o700, parents=parents, exist_ok=True)
    info = path.stat()
    if not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Benny event state requires an owner-only directory')
    return path


def receiver(*, repo, rules, state, signing_secret, app_id, selected_roots, port=8791):
    """Bind a loopback receiver. Work dispatch happens in a separate process."""
    rules = benny._rules(rules)
    project, deployment = _binding(repo, rules)
    store = _state(state, project['repo'])
    selected = frozenset(selected_roots)
    if (not 1 <= len(selected) <= 100 or not all(isinstance(root, str) and STAMP.fullmatch(root) for root in selected)):
        raise ValueError('select one to one hundred exact Slack report roots')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status, result):
            data = json.dumps(result).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != '/slack/events':
                self._reply(404, {'ok': False})
                return
            self.connection.settimeout(2)
            lengths = self.headers.get_all('Content-Length', [])
            signed = [self.headers.get_all(name, []) for name in ('X-Slack-Request-Timestamp', 'X-Slack-Signature')]
            if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                    or len(lengths[0]) > 8 or not 1 <= int(lengths[0]) <= benny_events.MAX_BODY
                    or self.headers.get('Transfer-Encoding') or any(len(values) != 1 for values in signed)):
                self._reply(400, {'ok': False})
                return
            length = int(lengths[0])
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError('incomplete Slack delivery')
                result = benny_events.receive(body, self.headers, signing_secret, app_id=app_id, rules=rules,
                                              selected_roots=selected)
                if result['status'] == 'accepted':
                    benny_events.enqueue(store, result['event'], deployment)
            except benny_events.AuthenticationError:
                self._reply(401, {'ok': False})
            except (ValueError, socket.timeout):
                self._reply(400, {'ok': False})
            except OSError:
                self._reply(503, {'ok': False})
            else:
                response = {'challenge': result['challenge']} if result['status'] == 'challenge' else {'ok': True}
                self._reply(200, response)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def _ref(path):
    return {'path': str(path), 'sha256': cli.digest(path)}


def _policy(path, role):
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError('choose a regular Benny worker policy file')
    with path.open('rb') as stream:
        raw = stream.read(32001)
    if len(raw) > 32000:
        raise ValueError('Benny worker policy exceeds the byte limit')
    all_roles = json.loads(raw)
    if (not isinstance(all_roles, dict) or all_roles.get('schema') != cli.SCHEMA
            or not set(all_roles) <= {'schema', 'triage', 'reproduce'} or not isinstance(all_roles.get(role), dict)):
        raise ValueError('worker policy needs the selected Benny role')
    own = all_roles[role]
    permitted = (TRIAGE_OPTIONS | {'apply_verdict', 'labels', 'followup_seconds'} if role == 'triage'
                 else REPRO_OPTIONS | VISUAL_OPTIONS | {'claim_seconds', 'repair', 'operations'})
    if own.keys() - permitted:
        raise ValueError('unknown option for the selected Benny role')
    if role == 'triage' and type(own.get('apply_verdict', False)) is not bool:
        raise ValueError('triage write permission must be explicit')
    repair = own.get('repair')
    if role == 'reproduce' and repair is not None and (
            not isinstance(repair, dict) or set(repair) - {'fix_search', 'seconds', 'iterations', 'draft'}
            or not isinstance(repair.get('fix_search'), list) or type(repair.get('draft', False)) is not bool):
        raise ValueError('Benny repair requires explicit searches and draft permission')
    operations = own.get('operations')
    if 'operations' in own and (not isinstance(operations, dict) or operations.keys() != {'channel', 'identity'}
            or not benny._matches(CHANNEL, operations['channel']) or not benny._matches(USER, operations['identity'])):
        raise ValueError('Benny operations needs an explicit Slack channel and writer identity')
    own = {**own}
    for key in ('routing_map', 'manifest', 'helpers', 'oracle'):
        if key in own and own[key] is not None:
            requested = Path(own[key]).expanduser()
            own[key] = str((path.parent / requested).resolve(strict=True))
    return own, hashlib.sha256(raw).hexdigest()


def _route(event, rules, slack, directory):
    return benny.freeze(rules=rules, slack=slack, channel=event['channel'], message_ts=event['message_ts'],
                        thread_ts=event['root'] if event['message_ts'] != event['root'] else None,
                        out=directory)


def _apply_handoff(checkpoint, *, slack, writer):
    remaining = int(checkpoint['handoff_deadline'] - time.time())
    if remaining < 10:
        raise ValueError('Benny handoff time limit reached; inspect the original journal')
    plan = checkpoint['handoff']
    applied = benny_handoff.apply(plan=plan['plan'], sha256=plan['sha256'], slack=slack, writer=writer,
                                  seconds=min(remaining, HANDOFF_SECONDS))
    return {'status': applied['status'], 'triage': checkpoint['triage'], 'handoff': plan}


def _triage(route, opts, *, project, rules, slack, writer, out, checkpoint):
    result = benny_triage.triage(slack=slack, receipt=route['receipt'], sha256=route['sha256'],
                                repo=project['repo'], revision=project['revision'], tracker_repo=rules['repository'],
                                search_source=True, out=out/'triage',
                                **{key: opts[key] for key in TRIAGE_OPTIONS if key in opts})
    receipt = out/'triage/result.json'
    reference = _ref(receipt)
    if result['status'] == 'ready' and opts.get('apply_verdict'):
        labels = opts.get('labels', ()) if result['recommendation']['action'] == 'draft-new-issue' else ()
        plan = benny_handoff.prepare(triage=receipt, sha256=cli.digest(receipt), state_dir=out/'handoff',
                                    labels=labels, followup_seconds=opts.get('followup_seconds', 0))
        saved = checkpoint(phase='handoff', triage=reference, handoff=plan,
                           handoff_deadline=time.time() + HANDOFF_SECONDS)
        return _apply_handoff(saved, slack=slack, writer=writer)
    return {'status': result['status'], 'triage': reference}


def _retained_reproduction(path, route, project, prior):
    if path.resolve() != path or not path.is_file():
        raise ValueError('interrupted Benny reproduction has no complete result to resume')
    reference = _ref(path)
    if prior.get('phase') != 'reproducing' and prior.get('reproduction') != reference:
        raise ValueError('retained Benny reproduction changed before repair')
    proof = benny_handoff._load(path, reference['sha256'], 'benny-reproduction')
    if (proof.get('base') != project['revision'] or proof.get('checkout') != project['repo']
            or proof.get('route') != {'path': route['receipt'], 'sha256': route['sha256']}):
        raise ValueError('retained Benny reproduction does not match the frozen event and deployment')
    if proof.get('status') == 'reproduced':
        _, proof, _, _ = benny_reproduce._approved(path, reference['sha256'])
    elif prior.get('phase') != 'reproducing' or proof.get('status') not in (
            'not-reproduced', 'inconclusive', 'environment-blocked', 'source-unconfirmed'):
        raise ValueError('retained Benny reproduction has an invalid result for this checkpoint')
    return reference, proof


def _retained_task(task, workspace, reference, proof, repair, phase):
    if task.resolve() != task or not task.is_file() or workspace.resolve() != workspace or not workspace.is_dir():
        raise ValueError('interrupted Benny task preparation has no complete task to resume')
    data = cli.read(task)
    binding = {**reference, 'base': proof['base'], 'fix_search': repair['fix_search']}
    if (data.get('id') != task.parent.name or data.get('repo') != str(workspace)
            or data.get('benny_reproduction') != binding
            or data.get('base_commit') != proof['base']
            or (phase == 'prepare-repair' and data.get('status') != 'paused')
            or start._read_input_stage(task)['phase'] != 'ready'):
        raise ValueError('retained Benny task does not match the approved reproduction and original input')


def _report_repair_failure(operations, task):
    if operations is None:
        return
    draft = task.with_name('benny-draft.json')
    try:
        uncertain = draft.exists() and cli.read(draft).get('phase') in ('create-intent', 'draft')
        operations.status('Blocked' if uncertain else 'Fix did not land')
    except (ValueError, OSError, cli.WorkbenchError):
        # Preserve the native failure and the operations journal for later inspection.
        pass


def _reproduce(route, opts, runtime, *, project, rules, slack, out, checkpoint, prior=None, operations=None):
    if route['status'] == 'verify-existing':
        confirmed = benny.verify(slack=slack, receipt=route['receipt'], sha256=route['sha256'],
                                 out=out/'existing', **runtime)
        return {'status': confirmed['outcome'], 'existing': _ref(out/'existing/result.json')}
    path = out/'reproduction/result.json'
    if prior:
        reference, proof = _retained_reproduction(path, route, project, prior)
    else:
        checkpoint(phase='reproducing')
        proof = benny_reproduce.reproduce(slack=slack, route=route['receipt'], sha256=route['sha256'],
                                           revision=project['revision'], out=out/'reproduction',
                                           seconds=rules['verification_seconds'], **runtime,
                                           **{key: opts[key] for key in VISUAL_OPTIONS | {'claim_seconds'} if key in opts})
        reference = _ref(path)
    result = {'status': proof['status'], 'reproduction': reference}
    repair = opts.get('repair')
    if not repair or proof['status'] != 'reproduced':
        return result
    ready_at = datetime.fromisoformat(proof['finished_at']).timestamp() + proof['claim_seconds']
    if time.time() < ready_at:
        return {**result, 'status': 'waiting-claim', 'ready_at': datetime.fromtimestamp(ready_at, timezone.utc).isoformat()}
    task_id = 'benny-' + _hash(out.name)[:32]
    task = out/'tasks'/task_id/'task.json'
    phase = prior.get('phase') if prior else None
    if phase in ('prepare-repair', 'repair'):
        if prior.get('task') != str(task):
            raise ValueError('Benny checkpoint refers to a different repair task')
        _retained_task(task, out/'worktrees'/task_id, reference, proof, repair, phase)
    else:
        if operations:
            operations.status('Reproduced')
        checkpoint(phase='prepare-repair', reproduction=reference, task=str(task))
        prepared = benny_reproduce.prepare_repair(
            slack=slack, receipt=reference['path'], sha256=reference['sha256'], task_id=task_id,
            tasks_root=out/'tasks', workspaces_root=out/'worktrees', fix_search=repair['fix_search'],
            **{key: repair[key] for key in ('seconds', 'iterations') if key in repair})
        if Path(prepared['task']) != task:
            raise ValueError('Benny prepared a task outside its dispatch checkpoint')
    checkpoint(phase='repair', reproduction=reference, task=str(task))
    if operations:
        operations.status('Attempting bounded fix')
    run = benny_draft.deliver if repair.get('draft') else benny_repair.run_repair
    try:
        completed = run(task=str(task), slack=slack)
    except ERRORS:
        _report_repair_failure(operations, task)
        raise
    return {**result, 'status': completed['status'], 'task': str(task), 'repair': completed}


def _reproduction_dispatch(route, opts, *, project, rules, slack, writer, out, checkpoint, prior=None):
    opening = OPERATIONS_START.get(route['status'])
    if opening is None:
        return {'status': route['status']}
    runtime = {key: opts[key] for key in REPRO_OPTIONS if key in opts}
    required = REPRO_OPTIONS - {'memory_mb'}
    if not required <= runtime.keys():
        raise ValueError('reproduction needs the approved image and both runtime probes')
    runtime['repo'] = project['repo']
    config = opts.get('operations')
    operation_path = out/'operations.json'
    operations = (benny_operations.Operations(operation_path, workspace=rules['workspace'],
                  source_channel=rules['channel'], source_root=route['root'], repository=rules['repository'],
                  slack=slack, writer=writer, **config) if config else None)
    phase = prior.get('phase') if prior else None
    if phase == 'reporting-operations':
        result = prior['outcome']
    else:
        if not prior or phase == 'opening-operations':
            if operations:
                checkpoint(phase='opening-operations')
                operations.status(opening)
            if route['status'] != 'eligible-for-reproduction':
                checkpoint()
            prior = None
        result = _reproduce(route, opts, runtime, project=project, rules=rules, slack=slack, out=out,
                            checkpoint=checkpoint, prior=prior, operations=operations)
    if operations:
        checkpoint(phase='reporting-operations', outcome=result)
        if result['status'] == 'draft':
            operations.draft(result['repair']['url'])
        else:
            operations.status(OPERATIONS_RESULT.get(result['status'], 'Blocked'))
        result = {**result, 'operations': _ref(operation_path)}
    return result


def work(*, repo, rules, state, policy, event_id, slack, writer=None, operations_writer=None):
    """Run the named event against the frozen project and operator policy once."""
    rules = benny._rules(rules)
    project, deployment = _binding(repo, rules)
    store = _state(state, project['repo'])
    receipt = benny_events.load(store, event_id)
    event = receipt['event']
    if receipt['deployment'] != deployment or event['workspace'] != rules['workspace'] or event['channel'] != rules['channel']:
        raise ValueError('queued Benny report no longer matches the committed deployment and routing rules')
    role = event['role']
    opts, policy_digest = _policy(policy, role)
    if role == 'triage' and opts.get('apply_verdict') and writer is None:
        raise ValueError('authorized triage writes require a separate Slack writer')
    operations_writer = operations_writer or writer
    if ops := opts.get('operations'):
        if operations_writer is None or ops['channel'] == rules['channel']:
            raise ValueError('Benny operations requires a separate channel and an authorized writer')
    key = _hash([event['workspace'], event['channel'], event['root'], role])[:32]
    lock_path = store/'events'/f'{event_id}.json'
    with task_lock(lock_path, lock_name=f'.dispatch-{key}.lock'):
        group = _private_directory(_private_directory(store/'dispatch')/key)
        record_path = group/'result.json'
        if record_path.is_symlink():
            raise ValueError('Benny dispatch receipt must be a regular file')
        expected = {'schema': cli.SCHEMA, 'deployment': deployment, 'policy_sha256': policy_digest, 'role': role}
        prior = None
        if record_path.exists():
            prior = cli.read(record_path)
            if any(prior.get(name) != value for name, value in expected.items()):
                raise ValueError('Benny scope or worker policy changed after work started')
            if prior['status'] == 'running':
                can_resume = (prior.get('phase') == 'handoff' and opts.get('apply_verdict') if role == 'triage'
                              else prior.get('phase') in REPRO_CHECKPOINTS)
                if not can_resume:
                    raise ValueError('an interrupted Benny dispatch requires inspection of its preserved files')
                if prior['event'] != event_id:
                    return prior
            elif prior['status'] == 'waiting-claim':
                if prior['event'] != event_id or time.time() < datetime.fromisoformat(prior['ready_at']).timestamp():
                    return prior
            elif prior['status'] != 'awaiting-verdict' or prior['event'] == event_id:
                return prior
            else:
                prior = None
        directory = _private_directory(group/event_id)
        if prior:
            _, frozen = benny.load_route(prior['source']['path'], prior['source']['sha256'])
            route = {**frozen['decision'], 'receipt': prior['source']['path'], 'sha256': prior['source']['sha256']}
        else:
            route = _route(event, rules, slack, directory/'source')
        record = {**expected, 'event': event_id, 'source': {'path': route['receipt'], 'sha256': route['sha256']}}

        def checkpoint(**fields):
            saved = {**record, 'status': 'running', **fields}
            cli.write(record_path, saved)
            return saved

        if role == 'triage':
            if prior and prior['status'] == 'running':
                result = _apply_handoff(prior, slack=slack, writer=writer)
            else:
                checkpoint()
                result = _triage(route, opts, project=project, rules=rules, slack=slack, writer=writer, out=directory,
                                 checkpoint=checkpoint)
        else:
            result = _reproduction_dispatch({**route, 'root': event['root']}, opts, project=project, rules=rules,
                                            slack=slack, writer=operations_writer, out=directory, checkpoint=checkpoint,
                                            prior=prior)
        cli.write(record_path, {**record, **result})
        return {**record, **result}


def watch(*, repo, rules, state, policy, slack, writer=None, operations_writer=None, seconds=300, max_events=8,
          poll_seconds=2):
    """Consume queued events for a bounded admission window, including claim-window continuations."""
    if (type(seconds) is not int or not 1 <= seconds <= 3600 or type(max_events) is not int
            or not 1 <= max_events <= 100 or type(poll_seconds) is not int or not 1 <= poll_seconds <= 30):
        raise ValueError('Benny watch needs a bounded admission window, event count and poll period')
    rules = benny._rules(rules)
    project, _ = _binding(repo, rules)
    store = _state(state, project['repo'])
    inbox = _private_directory(store/'events')
    settled = _private_directory(store/'processed')
    next_check, reports = {}, {}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for path in sorted(inbox.glob('Ev*.json')):
            if time.monotonic() >= end:
                break
            ident = path.stem
            acknowledgement = settled/path.name
            if acknowledgement.is_file() and not acknowledgement.is_symlink():
                continue
            if ident not in reports and len(reports) >= max_events:
                continue
            if ident in reports and (ident not in next_check or time.time() < next_check[ident]):
                continue
            try:
                result = work(repo=repo, rules=rules, state=store, policy=policy, event_id=ident,
                              slack=slack, writer=writer, operations_writer=operations_writer)
            except TaskBusy:
                continue
            except ERRORS as error:
                reports[ident] = 'blocked:' + type(error).__name__
                next_check.pop(ident, None)
                cli.write(acknowledgement, {'schema': cli.SCHEMA, 'event': ident, 'status': reports[ident]})
                continue
            reports[ident] = result['status']
            if result['status'] == 'waiting-claim' and result['event'] == ident:
                next_check[ident] = datetime.fromisoformat(result['ready_at']).timestamp()
            else:
                next_check.pop(ident, None)
                cli.write(acknowledgement, {'schema': cli.SCHEMA, 'event': ident, 'status': reports[ident]})
        if len(reports) >= max_events and not next_check:
            break
        remaining = end - time.monotonic()
        if remaining > 0:
            time.sleep(min(poll_seconds, remaining))
    return {'status': 'finished', 'events': reports}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('serve', 'work', 'watch'):
        command = sub.add_parser(action)
        for key in ('repo', 'config', 'state'):
            command.add_argument('--' + key, required=True)
        if action == 'serve':
            for key in ('signing-secret-file', 'app-id'):
                command.add_argument('--' + key, required=True)
            command.add_argument('--selected-root', action='append', required=True)
            command.add_argument('--port', type=int, default=8791)
        else:
            for key in ('policy', 'token-file'):
                command.add_argument('--' + key, required=True)
            command.add_argument('--writer-token-file')
            command.add_argument('--operations-writer-token-file')
            if action == 'work':
                command.add_argument('--event-id', required=True)
            else:
                command.add_argument('--seconds', type=int, default=300)
                command.add_argument('--max-events', type=int, default=8)
                command.add_argument('--poll-seconds', type=int, default=2)
    args = vars(parser.parse_args(argv))
    action = args.pop('action')
    try:
        rules = benny.config(args.pop('config'))
        if action == 'serve':
            secret = benny_events.read_signing_secret(args.pop('signing_secret_file'))
            with receiver(rules=rules, signing_secret=secret, selected_roots=args.pop('selected_root'), **args) as server:
                print(json.dumps({'status': 'listening', 'port': server.server_port}), flush=True)
                server.serve_forever()
        else:
            slack = Slack(args.pop('token_file'))
            token = args.pop('writer_token_file')
            operations_token = args.pop('operations_writer_token_file')
            runner = work if action == 'work' else watch
            result = runner(rules=rules, slack=slack, writer=SlackWriter(token) if token else None,
                            operations_writer=SlackWriter(operations_token) if operations_token else None, **args)
            print(json.dumps(result, ensure_ascii=False))
    except ERRORS as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
