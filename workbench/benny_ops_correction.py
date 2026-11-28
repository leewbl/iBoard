"""Retest one reporter correction after the engineering outcome has settled."""

from pathlib import Path
import math
import re
import time

from workbench import (benny, benny_correction, benny_correction_language, benny_handoff, benny_reproduce,
                       benny_triage, cli)
from workbench.benny_slack import SlackError, STAMP
from workbench.task_lock import task_lock


KIND = 'benny-operations-correction'
BODY = {
    'reproduced': 'The approved corrected setup reproduced the reported behavior on both offline probes.',
    'not-reproduced': 'The approved corrected setup did not reproduce the reported behavior on either check.',
    'inconclusive': 'The approved corrected setup produced inconclusive verification evidence.',
    'environment-blocked': 'The verification environment blocked a conclusion for the approved corrected setup.',
    'source-unconfirmed': 'The source report changed during verification of the approved corrected setup.',
}


def _path(path):
    if path.resolve() != path:
        raise ValueError('Benny operations correction needs its canonical event file')
    return path


def _reference(path):
    return {'path': str(path), 'sha256': cli.digest(path)}


def _same_source(slack, route, notice, deadline):
    frozen = route['snapshot']
    rows = frozen['messages']
    count = rows.count(notice)
    if count > 1 or (not count and benny._ticks(notice['ts']) <= benny._ticks(rows[-1]['ts'])):
        raise ValueError('Benny operations correction lost the exact source notice')
    expected = frozen if count else {**frozen, 'messages': [*rows, notice]}
    current = benny_reproduce._source(slack, route, deadline, notice=None if count else notice)
    if (current != frozen or benny.route(expected, route['rules'], route['selected_artifact']) != route['decision']):
        raise SlackError('Benny original report changed before the operations correction')
    return expected


def _approved_outcome(event, outcome, operations):
    statuses = {'reproduced': 'Reproduced', 'verified': 'Fix verified', 'draft': 'Draft pull request opened'}
    if outcome.get('status') not in statuses or statuses[outcome['status']] != operations.get('status'):
        raise ValueError('Benny operations correction requires a settled reproduction outcome')
    initial = outcome.get('original_reproduction', outcome.get('reproduction'))
    notice, archive = outcome.get('source_notice'), outcome.get('correction_options')
    proof, original_route, row, approved = benny_correction._bound_inputs(event, initial, notice, archive)
    settled = outcome['reproduction']
    corrected = 'original_reproduction' in outcome
    settled_path = event/('correction/reproduction/result.json' if corrected else 'reproduction/result.json')
    if benny_correction._receipt_path(settled) != settled_path:
        raise ValueError('Benny operations correction needs the original settled reproduction')
    _, settled_proof, contract, _ = benny_reproduce._approved(settled_path, settled['sha256'])
    _, route = benny.load_route(settled_proof['route']['path'], settled_proof['route']['sha256'])
    scope, location = operations['scope'], original_route['snapshot']['location']
    if ((scope['workspace'], scope['source_channel'], scope['source_root'], scope['repository'])
            != (original_route['rules']['workspace'], location['channel'], location['root'], proof['repository'])
            or scope['channel'] == location['channel'] or not benny._matches(STAMP, operations.get('root'))):
        raise ValueError('Benny operations correction scope differs from the original report')
    original = {'initial': initial, 'notice': notice, 'archive': archive, 'settled': settled}
    return proof, route, row, approved, contract, original


def _choice(folder, message, text, approved, original, model, deadline):
    variant = benny_correction._named_setup({'text': text}, approved)
    if variant is not None:
        return variant, None
    guidance = approved.get('guidance')
    if not guidance:
        raise ValueError('Benny operations correction needs an approved setup name or applicability description')
    binding = {key: original[key] for key in ('initial', 'notice', 'archive')}
    def interpret(workspace, prompt, timeout, **options):
        remaining = min(timeout, int(benny_reproduce._remaining(deadline)) - 10)
        if remaining < 1:
            raise ValueError('Benny operations correction has no interpretation time left before verification')
        return (model or benny_correction_language.execute)(workspace, prompt, remaining, **options)

    reference = benny_correction_language.choose(folder/'interpretation.json', message=message, guidance=guidance,
                                               binding=binding, model=interpret)
    variant = benny_correction_language.confirmed(reference, message=message, guidance=guidance, binding=binding)
    if variant is None:
        raise ValueError('Benny operations correction did not identify a clear approved setup')
    return variant, reference


def _result(path, plan, expected_contract, approved):
    if not path.is_file():
        raise ValueError('Benny operations correction attempt has no completed receipt')
    reference = _reference(path)
    proof = benny_handoff._load(path, reference['sha256'], 'benny-reproduction')
    status = proof['status']
    if (status not in BODY or any(proof[key] != plan[key] for key in ('base', 'route', 'checkout', 'repository'))
            or proof.get('image', plan['image']) != plan['image']):
        raise ValueError('Benny operations correction result changed its admitted baseline')
    contract_path = _path(path.with_name('contract.json'))
    if cli.digest(contract_path) != proof['contract_sha256']:
        raise ValueError('Benny operations correction contract changed after verification')
    contract = cli.read(contract_path)
    benny_correction._require_contract(contract, approved['setups'][plan['variant']], 'operations-corrected')
    fields = ('base', 'memory_mb', 'visual')
    if any(contract.get(key) != expected_contract.get(key) for key in fields):
        raise ValueError('Benny operations correction changed its runtime or media acceptance')
    if status == 'reproduced':
        benny_reproduce._approved(path, reference['sha256'])
    elif status == 'not-reproduced':
        baseline = _path(path.parent/'baseline/result.json')
        if (proof.get('runtime_status') != status or proof.get('image') != plan['image']
                or cli.digest(baseline) != proof.get('baseline_sha256')):
            raise ValueError('Benny operations correction lacks its checked healthy result')
        measurable = {key: value for key, value in contract.items() if key != 'visual'}
        benny_reproduce._observations(baseline.parent, cli.read(baseline), measurable, plan['base'], plan['image'], 'pass')
    return reference, status


def confirmed(reference, *, event, outcome, operations):
    """Recheck the original and rerun evidence before using the result in Slack."""
    event = _path(Path(event))
    path = benny_correction._receipt_path(reference)
    if path != event/'operations-correction/result.json':
        raise ValueError('Benny operations correction result belongs to a different event')
    state = benny_handoff._load(path, reference['sha256'], KIND)
    if state.get('phase') != 'completed':
        raise ValueError('Benny operations correction has no completed verification receipt')
    proof, _, _, approved, media, original = _approved_outcome(event, outcome, operations)
    plan = state['plan']
    expected = {**original, 'scope': operations['scope'], 'root': operations['root'],
                **{key: proof[key] for key in ('base', 'image', 'checkout', 'repository')}}
    if any(plan.get(key) != value for key, value in expected.items()):
        raise ValueError('Benny operations correction no longer matches the original event')
    route = plan['route']
    if benny_correction._receipt_path(route) != path.parent/'source/route.json':
        raise ValueError('Benny operations correction route belongs to a different event')
    benny.load_route(route['path'], route['sha256'])
    if interpretation := plan.get('interpretation'):
        if benny_correction._receipt_path(interpretation) != path.parent/'interpretation.json':
            raise ValueError('Benny operations correction interpretation belongs to a different event')
        binding = {key: original[key] for key in ('initial', 'notice', 'archive')}
        variant = benny_correction_language.confirmed(interpretation, message=plan['message'],
                                                      guidance=approved.get('guidance', {}), binding=binding)
        if variant != plan['variant']:
            raise ValueError('Benny operations correction interpretation changed after verification')
    reproduction, status = _result(path.parent/'reproduction/result.json', plan, media, approved)
    if reproduction != state.get('reproduction') or status != state.get('status'):
        raise ValueError('Benny operations correction changed its completed evidence')
    return BODY[status]


def run(*, event, outcome, operations, message, text, runtime, verification_seconds, slack,
        runner=benny_reproduce.reproduce, model=None, expires_at=None):
    """Run at most one isolated verification without reopening the settled task."""
    event = _path(Path(event))
    proof, route, notice, approved, media, original = _approved_outcome(event, outcome, operations)
    reporter = route['snapshot']['messages'][0]
    identity = operations['scope']['identity']
    if (not isinstance(message, dict) or message.keys() != reporter.keys() or reporter['bot']
            or message.get('author') != reporter['author'] or message.get('bot') is not False
            or message.get('files') != [] or not benny._matches(STAMP, message.get('ts'))
            or benny._ticks(message['ts']) <= benny._ticks(operations['root'])
            or not benny_triage._text(message.get('text'), 1500) or not benny_triage._text(text, 1400)):
        raise ValueError('Benny operations correction requires a direct message from the original reporter')
    addressed = re.fullmatch(fr'(?:hey\s+)?<@{re.escape(identity)}>[\s,:：，]+(.+)', message['text'].strip(), re.I | re.S)
    if addressed is None or addressed[1].strip() != text:
        raise ValueError('Benny operations correction does not match its original direct message')
    benny_reproduce._limits(verification_seconds, runtime.get('memory_mb', 3328))
    if expires_at is not None and (type(expires_at) not in (int, float) or not math.isfinite(expires_at)):
        raise ValueError('Benny operations correction needs a fixed window deadline')
    folder = _path(event/'operations-correction')
    journal = _path(folder/'result.json')
    output = _path(folder/'reproduction/result.json')
    route_path = _path(folder/'source/route.json')
    with task_lock(event/'correction-options.json', lock_name='.operations-correction.lock', reentrant=False):
        state = cli.read(_path(journal)) if journal.exists() else None
        if state and (state.get('kind') != KIND or state.get('plan', {}).get('message') != message):
            raise ValueError('Benny has already admitted its one operations correction')
        available = min(30, verification_seconds) if state else verification_seconds
        if not state and expires_at is not None:
            available = min(available, expires_at - time.time())
        deadline = time.monotonic() + available
        if not state and benny_reproduce._remaining(deadline) < 10:
            raise ValueError('Benny operations correction has insufficient time in its original window')
        expected = _same_source(slack, route, notice, min(deadline, time.monotonic() + 30))
        variant, interpretation = _choice(folder, message, text, approved, original, model, deadline)
        settings = benny_correction.options(original['archive'], variant, runtime)
        recipe = approved['setups'][variant]['contract']
        if recipe['helper_digests'] == media['helper_digests']:
            raise ValueError('Benny operations correction selected the same probes as the settled setup')
        if benny_reproduce._visual_contract(recipe['project'], recipe['journey'], settings.get('baseline_visible'),
                                            settings.get('candidate_visible')) != media.get('visual'):
            raise ValueError('Benny operations correction changed its media acceptance')
        reroute = {key: value for key, value in route.items() if key != 'correction'}
        reroute['snapshot'] = expected
        if route_path.exists():
            if cli.read(route_path) != reroute:
                raise ValueError('Benny operations correction route changed after admission')
        else:
            _same_source(slack, route, notice, min(deadline, time.monotonic() + 30))
            cli.write(route_path, reroute)
        plan = {**original, 'scope': operations['scope'], 'root': operations['root'], 'message': message,
                'variant': variant, 'interpretation': interpretation, 'base': proof['base'], 'image': proof['image'],
                'checkout': proof['checkout'], 'repository': proof['repository'], 'route': _reference(route_path),
                'seconds': verification_seconds, 'expires_at': expires_at}
        if state:
            if state.get('plan') != plan or state.get('phase') not in ('running', 'completed'):
                raise ValueError('Benny has already admitted its one operations correction')
        else:
            if output.parent.exists():
                raise ValueError('Benny operations correction has an unbound reproduction attempt')
            remaining = benny_reproduce._remaining(deadline)
            if expires_at is not None:
                remaining = min(remaining, expires_at - time.time())
            seconds = int(remaining)
            benny_reproduce._limits(seconds, settings.get('memory_mb', 3328))
            state = {'schema': cli.SCHEMA, 'kind': KIND, 'plan': plan, 'phase': 'running'}
            cli.write(journal, state)
            settings['image'] = proof['image']
            runner(slack=slack, route=route_path, sha256=plan['route']['sha256'], revision=proof['base'],
                   out=output.parent, seconds=seconds, claim_seconds=0, **settings)
        reference, status = _result(output, plan, media, approved)
        completed = {**state, 'phase': 'completed', 'reproduction': reference, 'status': status}
        if state['phase'] == 'completed' and completed != state:
            raise ValueError('Benny operations correction changed its completed evidence')
        if completed != state:
            cli.write(journal, completed)
        return {'body': BODY[status], 'reference': _reference(journal)}
