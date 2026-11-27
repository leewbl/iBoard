"""Reproduce a Benny report and retain evidence for the independent bug workflow."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time

from runner.sandbox import run_sandbox
from workbench import (benny, benny_handoff, benny_triage, browser_recording, cli, delivery, pr_watch,
                       project_triage, project_verify, start, workflow)
from workbench.benny_slack import Slack, SlackError


IMAGE = re.compile(r'sha256:[a-f0-9]{64}')
FAILURES = ['observed-failure'] * 2
PASSES = ['pass'] * 2


def _repository(repo, destination):
    path = Path(repo).expanduser().resolve(strict=True)
    remote = cli.git(path, 'remote', 'get-url', 'origin', timeout=12).removesuffix('.git')
    if remote.casefold() not in (f'https://github.com/{destination}'.casefold(), f'git@github.com:{destination}'.casefold(),
                                f'ssh://git@github.com/{destination}'.casefold()):
        raise ValueError('use the exact GitHub repository configured for this report')
    return path


def _source(slack, route, deadline):
    rules, frozen = route['rules'], route['snapshot']
    if benny.route(frozen, rules, route['selected_artifact']) != route['decision'] or (
            route['decision']['status'] != 'eligible-for-reproduction'):
        raise ValueError('Benny requires trusted bug or performance intake without a claimant or existing fix')
    slack.check_team(rules['workspace'], deadline)
    current = benny._snapshot(slack, frozen['location'], deadline)
    if current != frozen or benny.route(current, rules)['status'] != 'eligible-for-reproduction':
        raise SlackError('Benny source changed or the fix is no longer available for this report')
    return current


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value < 1:
        raise ValueError('Benny runtime budget expired')
    return value


def _limits(seconds, memory_mb):
    if type(seconds) is not int or not 10 <= seconds <= 3600 or type(memory_mb) is not int or not 128 <= memory_mb <= 8192:
        raise ValueError('Benny requires 10–3600 seconds and 128–8192 MiB')


def _output(repo, out):
    root = Path(out).expanduser().resolve()
    if root.is_relative_to(repo):
        raise ValueError('keep Benny runtime evidence outside the target checkout')
    root.mkdir(mode=0o700, parents=True)
    return root


def _visual_contract(project, journey, baseline, candidate):
    if baseline is None and candidate is None and not journey.get('recording'):
        return None
    if project != 'openclaw' or not all(journey.get(key) is True for key in ('browser', 'screenshot', 'recording')):
        raise ValueError('Benny visual acceptance needs a recorded OpenClaw browser journey')
    conditions = (baseline, candidate)
    if (any(not isinstance(text, str) or not 1 <= len(text.strip()) <= 600 or not text.strip().isprintable()
            for text in conditions) or baseline.strip().casefold() == candidate.strip().casefold()):
        raise ValueError('supply distinct visible conditions for the baseline fault and the candidate outcome')
    return {'baseline': baseline.strip(), 'candidate': candidate.strip()}


def _visual_condition(contract, side):
    visual = contract.get('visual')
    if visual is None:
        _visual_contract(contract['project'], contract['journey'], None, None)
        return None
    if not isinstance(visual, dict) or set(visual) != {'baseline', 'candidate'}:
        raise ValueError('invalid frozen Benny visual conditions')
    verified = _visual_contract(contract['project'], contract['journey'], visual['baseline'], visual['candidate'])
    if verified != visual:
        raise ValueError('frozen Benny visual conditions changed')
    return visual[side]


def _primary_report(directory, contract, attempt):
    return directory / f"{contract['journey']['id']}.primary-{attempt}.json"


def _review_path(report):
    return report.with_suffix('.media.json')


def _review_visuals(directory, runs, contract, condition):
    """Audit the two frozen primary executions and their matching visible observations."""
    if condition is None:
        if 'media_reviews' in runs:
            raise ValueError('unexpected browser reviews outside the approved conditions')
        return []
    records = runs.get('media_reviews')
    if not isinstance(records, list) or len(records) != 2:
        raise ValueError('both independent browser runs need a media review')
    proofs = []
    for attempt, record in enumerate(records, 1):
        report = _primary_report(directory, contract, attempt)
        receipt = _review_path(report)
        if not isinstance(record, dict) or record.get('report') != receipt.name:
            raise ValueError('browser review does not match the original primary execution')
        proof = browser_recording.audit_review(report, cli.digest(report), condition, receipt, record.get('sha256'))
        proofs.append((report, proof))
    return proofs


def _measure(repo, revision, pinned, destination, contract, bundle, deadline, sandbox, attester, *, side, review):
    if not isinstance(pinned, str) or not IMAGE.fullmatch(pinned):
        raise ValueError('Benny requires a source-attested image ID')
    destination.mkdir(mode=0o700)
    engine = project_verify.Verification(repo, revision, pinned, destination, _remaining(deadline), contract['memory_mb'],
                                         sandbox, attester)
    engine.report['verified_image_id'] = pinned
    try:
        marks = project_triage._probe(engine, bundle, contract['doctor'], contract['journey'], contract['independent'],
                                      contract['helper_digests'], set())
        engine.report['observations'] = marks
        required = FAILURES if side == 'baseline' else PASSES
        condition = _visual_condition(contract, side)
        if condition and marks['doctor_healthy'] and marks['primary'] == marks['oracle'] == required:
            receipts = engine.report['media_reviews'] = []
            for attempt in (1, 2):
                report = _primary_report(destination, contract, attempt)
                receipt = _review_path(report)
                review(report, cli.digest(report), condition, receipt, timeout=min(90, _remaining(deadline)))
                receipts.append({'report': receipt.name, 'sha256': cli.digest(receipt)})
            _review_visuals(destination, engine.report, contract, condition)
    finally:
        cli.write(destination/'result.json', engine.report)
    if not marks['doctor_healthy'] or 'unclassified' in [*marks['primary'], *marks['oracle']]:
        return 'environment-blocked'
    if marks['primary'] == marks['oracle'] == FAILURES:
        return 'reproduced'
    if marks['primary'] == marks['oracle'] == PASSES:
        return 'healthy'
    return 'inconclusive'


def reproduce(*, slack, route, sha256, repo, revision, image, manifest, helpers, journey, oracle,
              oracle_marker, oracle_failure_marker, out, seconds=600, memory_mb=3328, claim_seconds=600,
              baseline_visible=None, candidate_visible=None, sandbox=run_sandbox, attester=workflow._verified_image,
              review=browser_recording.review):
    _limits(seconds, memory_mb)
    if type(claim_seconds) is not int or not 0 <= claim_seconds <= 3600:
        raise ValueError('Benny ownership wait must be 0–3600 seconds')
    deadline = time.monotonic() + seconds
    route_path, record = benny.load_route(route, sha256)
    repo = _repository(repo, record['rules']['repository'])
    frozen = _source(slack, record, deadline)
    project_triage._image_reference(image)
    base = cli.sha(repo, revision, timeout=min(10, _remaining(deadline)))
    project, doctor, primary, alternate, files, digests = project_triage._inputs(
        manifest, helpers, journey, oracle, oracle_marker, oracle_failure_marker)
    visual = _visual_contract(project, primary, baseline_visible, candidate_visible)
    root = _output(repo, out)
    contract = {'schema': cli.SCHEMA, 'project': project, 'doctor': doctor, 'journey': primary,
                'independent': alternate, 'helper_digests': digests, 'memory_mb': memory_mb, 'base': base}
    if visual:
        contract['visual'] = visual
    cli.write(root/'contract.json', contract)
    project_verify._write_bundle(root/'bundle', {f'helpers/{name}': data for name, data in files.items()})
    original = frozen['permalink'] + '\n\n' + frozen['messages'][0]['text'] + '\n'
    (root/'report.txt').write_text(original, encoding='utf-8')
    report = {'schema': cli.SCHEMA, 'kind': 'benny-reproduction', 'route': {'path': str(route_path), 'sha256': sha256},
              'repository': record['rules']['repository'], 'checkout': str(repo), 'base': base, 'status': 'environment-blocked',
              'contract_sha256': cli.digest(root/'contract.json'), 'report_sha256': cli.digest(root/'report.txt'),
              'claim_seconds': claim_seconds}
    try:
        pinned = attester(repo, image, min(180, _remaining(deadline)), commit=base)
        report['image'] = pinned
        status = _measure(repo, base, pinned, root/'baseline', contract, root/'bundle', deadline, sandbox, attester,
                          side='baseline', review=review)
        report['baseline_sha256'] = cli.digest(root/'baseline/result.json')
        report['runtime_status'] = 'not-reproduced' if status == 'healthy' else status
        try:
            _source(slack, record, deadline)
        except (SlackError, ValueError):
            report['status'] = 'source-unconfirmed'
        else:
            report.update(status=report['runtime_status'], finished_at=cli.now())
    except (*project_verify.ERRORS, RuntimeError) as error:
        report['reason'] = str(error)[:400]
    cli.write(root/'result.json', report)
    return report


def _observations(directory, result, contract, commit, image, expected):
    specs = [('doctor', contract['doctor'], '', 'pass')]
    for name, check in (('primary', contract['journey']), ('oracle', contract['independent'])):
        for attempt in (1, 2):
            specs.append((name, check, f'.{name}-{attempt}', expected))
            if expected == 'observed-failure':
                specs.append(('doctor', contract['doctor'], f'.after-{name}-{attempt}', 'pass'))

    class Replay:
        memory_mb = contract['memory_mb']
        report = {'verified_image_id': image}
        revision = commit

    records = result['runs']
    if (result.get('commit') != commit or result.get('verified_image_id') != image or len(records) != len(specs)
            or result.get('observations') != {'doctor_healthy': True, 'primary': [expected] * 2, 'oracle': [expected] * 2}):
        raise ValueError('offline evidence does not show both required runs for each probe')
    containers = set()
    for item, (name, check, suffix, required) in zip(records, specs):
        path = directory/(check['id'] + suffix + '.json')
        raw = cli.read(path)
        if (item.get('report') != path.name or item.get('sha256') != cli.digest(path)
                or raw.get('commit') != commit or raw.get('check') != check['id']
                or raw.get('helper_sha256') != contract['helper_digests'][check['helper']]
                or not project_triage._complete_run(Replay, check, raw, path, containers)):
            raise ValueError('a frozen sandbox receipt or artifact no longer matches its source')
        if name == 'doctor':
            if not raw.get('passed') or raw.get('exit_code') != 0 or check['marker'] not in raw['stdout'].splitlines():
                raise ValueError('doctor did not prove a healthy execution environment')
        elif project_triage._observation(raw, check, image) != required:
            raise ValueError('sandbox evidence does not show the approved discriminating outcome')
    side = 'baseline' if expected == 'observed-failure' else 'candidate'
    _review_visuals(directory, result, contract, _visual_condition(contract, side))
    return containers


def _approved(receipt, sha256):
    path = Path(receipt).expanduser().resolve(strict=True)
    report = benny_handoff._load(path, sha256, 'benny-reproduction')
    root = path.parent
    baseline = root/'baseline/result.json'
    if (report.get('status') != 'reproduced' or report.get('runtime_status') != 'reproduced'
            or report['contract_sha256'] != cli.digest(root/'contract.json')
            or report['report_sha256'] != cli.digest(root/'report.txt')
            or report['baseline_sha256'] != cli.digest(baseline)):
        raise ValueError('Benny requires a confirmed, unchanged reproduction before proceeding')
    contract = cli.read(root/'contract.json')
    if (contract['base'] != report['base'] or not IMAGE.fullmatch(report['image'])
            or any(hashlib.sha256(project_verify._helper(root/'bundle/helpers', name)).hexdigest() != digest
                   for name, digest in contract['helper_digests'].items())):
        raise ValueError('Benny reproduction contract or approved driver changed')
    containers = _observations(baseline.parent, cli.read(baseline), contract, report['base'], report['image'],
                               'observed-failure')
    return path, report, contract, containers


def verify_candidate(*, receipt, sha256, revision, out, seconds=600, sandbox=run_sandbox,
                     attester=workflow._verified_image, derive=project_triage._derive, review=browser_recording.review):
    path, proof, contract, previous = _approved(receipt, sha256)
    _limits(seconds, contract['memory_mb'])
    deadline = time.monotonic() + seconds
    repo = _repository(proof['checkout'], proof['repository'])
    head = cli.sha(repo, revision, timeout=10)
    if head == proof['base'] or cli.git(repo, 'merge-base', proof['base'], head, timeout=10) != proof['base']:
        raise ValueError('the candidate must descend from the reproduced baseline')
    root = _output(repo, out)
    result = {'schema': cli.SCHEMA, 'kind': 'benny-candidate-proof', 'baseline': proof['base'], 'head': head,
              'reproduction': {'path': str(path), 'sha256': sha256}, 'status': 'environment-blocked'}
    try:
        rebuild = contract['project'] == 'openclaw' and bool(contract['journey'].get('browser'))
        image = derive(repo, proof['base'], head, proof['image'], _remaining(deadline), rebuild_openclaw_ui=rebuild)
        status = _measure(repo, head, image, root/'candidate', contract, path.parent/'bundle', deadline, sandbox, attester,
                          side='candidate', review=review)
        candidate = root/'candidate/result.json'
        result.update(image=image, runtime_status=status, candidate_sha256=cli.digest(candidate))
        if status == 'healthy':
            after = _observations(candidate.parent, cli.read(candidate), contract, head, image, 'pass')
            if previous & after:
                raise ValueError('candidate evidence reused a baseline container')
            _approved(path, sha256)
            _remaining(deadline)
            result['status'] = 'verified'
        else:
            result['status'] = 'still-reproduced' if status == 'reproduced' else status
    except (*project_verify.ERRORS, RuntimeError) as error:
        result['reason'] = str(error)[:400]
    cli.write(root/'result.json', result)
    return result


def _check_repair_owner(slack, proof, fix_search, deadline, tracker=None):
    if (not 1 <= len(fix_search) <= 4 or any(not benny_triage._text(term, 140) or not term.isprintable()
                                                or '"' in term or '\\' in term for term in fix_search)):
        raise ValueError('provide one to four approved precise GitHub fix-search phrases')
    if time.time() < datetime.fromisoformat(proof['finished_at']).timestamp() + proof['claim_seconds']:
        raise ValueError('the configured ownership window is still open')
    _, route = benny.load_route(proof['route']['path'], proof['route']['sha256'])
    repo = _repository(proof['checkout'], proof['repository'])
    source = _source(slack, route, deadline)
    tracker = tracker or benny_triage.Tracker()
    if isinstance(tracker, pr_watch.GitHubReader):
        tracker.set_deadline(deadline)
    candidates, searches = benny_triage._search(tracker, proof['repository'], fix_search, source['permalink'])
    if not all(item['complete'] for item in searches) or any(
            item['item_type'] == 'pull' and (item['state'] == 'open' or item['merged']) for item in candidates.values()):
        raise ValueError('possible existing fix or incomplete tracker search; use the existing-fix route or narrow the phrases')
    _source(slack, route, deadline)
    return repo, source, searches


def prepare_repair(*, slack, receipt, sha256, task_id, tasks_root, workspaces_root, seconds=1800, iterations=3,
                   launch=start.start, tracker=None, fix_search=()):
    path, proof, contract, _ = _approved(receipt, sha256)
    visual = _visual_condition(contract, 'baseline')
    if contract['journey'].get('screenshot') and not visual:
        raise ValueError('visual cases need the independent recording review before starting repair')
    _limits(seconds, contract['memory_mb'])
    if type(iterations) is not int or not 1 <= iterations <= 20:
        raise ValueError('Benny repair needs a fixed iteration budget')
    repo, source, _ = _check_repair_owner(slack, proof, fix_search, time.monotonic() + min(seconds, 90), tracker)
    binding = {'path': str(path), 'sha256': sha256, 'base': proof['base'], 'fix_search': list(fix_search)}

    def prepare(**options):
        prepared = workflow.prepare(**options, base_commit=proof['base'])
        task = Path(prepared['task'])
        data = cli.read(task)
        data['benny_reproduction'] = binding
        cli.write(task, data)
        return prepared

    prepared = launch(task_id=task_id, repo=repo, issue_file=path.parent/'report.txt', source=source['permalink'],
                      kind='bug', surface='ui' if visual else 'non-ui', iterations=iterations, seconds=seconds,
                      image=proof['image'],
                      tasks_root=tasks_root, workspaces_root=workspaces_root, prepare_only=True, require_root_cause=True,
                      source_mode='image', build_source_image=True, memory_mb=contract['memory_mb'], prepare=prepare)
    data = cli.read(Path(prepared['task']))
    if (data['base_commit'] != proof['base'] or data.get('status') != 'paused'
            or data.get('surface') != ('ui' if visual else 'non-ui')
            or data.get('benny_reproduction') != binding):
        raise ValueError('prepared repair task did not retain the approved paused baseline')
    return prepared


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('reproduce')
    commands.add_parser('verify-candidate')
    commands.add_parser('prepare-repair')
    def args(name, required):
        command = commands.choices[name]
        for key in required:
            command.add_argument('--' + key, required=True)
        command.add_argument('--seconds', type=int, default=1800 if name == 'prepare-repair' else 600)
        return command
    repro = args('reproduce', ('token-file', 'route', 'sha256', 'repo', 'revision', 'image', 'manifest', 'helpers',
                               'journey', 'oracle', 'oracle-marker', 'oracle-failure-marker', 'out'))
    repro.add_argument('--claim-seconds', type=int, default=600)
    repro.add_argument('--memory-mb', type=int, default=3328)
    repro.add_argument('--baseline-visible')
    repro.add_argument('--candidate-visible')
    args('verify-candidate', ('receipt', 'sha256', 'revision', 'out'))
    repair = args('prepare-repair', ('token-file', 'receipt', 'sha256', 'task-id', 'tasks-root', 'workspaces-root'))
    repair.add_argument('--iterations', type=int, default=3)
    repair.add_argument('--fix-search', action='append', default=[])
    options = vars(parser.parse_args(argv))
    command = options.pop('command')
    try:
        if command != 'verify-candidate':
            options['slack'] = Slack(options.pop('token_file'))
        result = {'verify-candidate': verify_candidate, 'reproduce': reproduce, 'prepare-repair': prepare_repair}[command](**options)
    except (*project_verify.ERRORS, delivery.DeliveryError, start.StartError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps({key: result[key] for key in ('status', 'task', 'base', 'head') if key in result}))
    return int(result['status'] not in ('reproduced', 'verified', 'paused'))


if __name__ == '__main__':
    raise SystemExit(main())
