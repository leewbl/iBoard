"""Bind a fault mechanism to isolated comparisons on the original and fixed code."""

import hashlib
import json
from collections import Counter
from pathlib import Path, PurePosixPath
import re
import subprocess

from workbench import cli, workflow


MODE = 'paired-control-v1'
INSTRUCTION = (
    'For fixed controls use "root_cause": {"mechanism":{"claim":"why the fault happens","path":"source file","line":1},'
    '"probes":[{"id":"short-name","alternative":{"claim":"a plausible competing explanation",'
    '"path":"source file","line":1},"control":{"command":["..."],"marker":"observed healthy outcome",'
    '"exit_code":0}}]}. Identify up to four plausible competing explanations from the affected code and '
    'available regression history. Give each explanation a distinct control, ordered by diagnostic value. '
    'Use a tracked entrypoint on the actual product surface. Reproduction and resolution must use the exact '
    'same command. For each control, change exactly one argument after the script so the proposed cause is '
    'absent while that competing explanation is still exercised. Choose distinct failure and success markers. '
    'If a plausible competing explanation cannot be distinguished using an existing driver, return blocked.'
)
SEARCH_INSTRUCTION = (
    'For competing hypotheses with different predicted observations, prefer a decision matrix instead. '
    'Use root_cause={"hypotheses":[{"id":"hypothesis","claim":"...","path":"source","line":1}],'
    '"investigation":[{"id":"probe","command":["..."],"outcomes":{"state":{"exit_code":0,"marker":"..."}},'
    '"predictions":{"hypothesis":"state"},"candidate":{"exit_code":0,"marker":"expected healthy observation"}}]}. '
    'Provide two to five hypotheses and up to six probes. Each probe changes exactly one argument to the '
    'same tracked driver. Provide two to four mutually exclusive observable outcomes per probe and predict '
    'one for every hypothesis. Distinguish every pair of hypotheses across the available probes. Ground '
    'predictions in the affected subsystem and available regression history. The coordinator runs the most '
    'distinguishing unused probe for the surviving hypotheses. Return blocked when real outcomes cannot be '
    'distinguished. Use the fixed controls schema for a simple matched comparison. '
)
BASELINE_INSTRUCTION = (
    'Check the source anchors and the trigger/control output from both independent executions. '
    'Confirm that the control removes only the proposed cause while the competing explanation remains '
    'exercised. If justified, include "cause_verdict":{"mechanism":"supported",'
    '"alternative":"ruled-out","reason":"why the contrast distinguishes these explanations"}. '
    'Return blocked for instrumentation that merely prints fabricated observations, or an inconclusive comparison.\n'
)
PROBE_INSTRUCTION = (
    'Check the driver, source anchors, and trigger output from both executions. For each probe, verify that '
    'only the proposed cause is removed and the named alternative is still exercised. Explain why the '
    'observation would differ if the alternative were true. A printed PASS marker alone proves nothing. '
    'Return "cause_verdict":{"mechanism":"supported","trigger_quote":"exact excerpt in both trigger outputs",'
    '"probes":{"probe-id":{"verdict":"ruled-out",'
    '"reason":"why this controlled run rules it out","quote":"exact single-line excerpt from both control outputs"}}}. '
    'You may add a short string "evidence" to cause_verdict explaining why the trigger supports the mechanism. '
    'Give a verdict for every probe ID. Return blocked if the trigger or any control is inconclusive, '
    'or if any plausible competing mechanism remains.\n'
)
SEARCH_REVIEW = (
    'Independently inspect the driver, code anchors and original observations from both model-independent '
    'runs. Follow the ordered trace. Explain why each observed result contradicts every hypothesis removed '
    'in that step and supports the survivor. Check the predicted outcomes against actual behavior and '
    'return blocked for missing, ambiguous, fabricated or semantically inadequate evidence. Return '
    'cause_verdict={"mechanism":"supported","survivor":"surviving hypothesis ID","trigger_quote":"exact '
    'excerpt from both trigger runs","probes":{"executed probe ID":{"verdict":"ruled-out","reason":"why '
    'the observation excludes the listed hypotheses","quote":"exact excerpt from both probe runs",'
    '"eliminated":["hypothesis IDs excluded at this step"]}}}. Give a verdict only for each executed probe. '
    'For each probe, copy only the IDs from its own eliminated array in the trace, never IDs excluded '
    'on an earlier round. Confirm each exclusion semantically against the raw output. '
    'Copy a literal substring of an output line into each quote field. Move any interpretation, '
    'exit-code annotation or parenthetical explanation into reason, outside the quote. '
    'You may add one short "evidence" string to explain why the surviving mechanism accounts for the fault.\n'
)
FINAL_INSTRUCTION = (
    'Confirm the same trigger now succeeds, every unchanged control is still healthy, and the diff addresses '
    'the mechanism found before any edit. Assess unexpected output and omit a PASS if the original failure '
    'is still present. Include "cause_fixed": true and a concrete "causal_explanation" of the mechanism and fix.\n'
)


class CauseError(ValueError):
    pass


class ReviewFormatError(CauseError):
    """The review provided invalid fields or text that is not a literal quote."""


def require_intake(data, stage):
    mode = data.get('root_cause')
    if mode != stage.get('root_cause') or (mode is not None and (mode != MODE or data.get('kind') != 'bug')):
        raise CauseError('task no longer matches the approved causal verification intake')
    return mode is not None


def _text(value, maximum=700):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _blob(repo, revision, path):
    entry = cli.git(repo, '--literal-pathspecs', 'ls-tree', revision, '--', path, timeout=12).split()
    if not entry or entry[0] not in ('100644', '100755'):
        raise CauseError('causal source must be a regular tracked file')
    object_name = f'{revision}:{path}'
    size = cli.git(repo, 'cat-file', '-s', object_name, timeout=12)
    if int(size) > 512_000 or cli.git(repo, 'cat-file', '-t', object_name, timeout=12) != 'blob':
        raise CauseError('causal source must be a bounded tracked file')
    try:
        return subprocess.run(['git', '-C', str(repo), 'cat-file', 'blob', object_name],
                              check=True, capture_output=True, timeout=12).stdout
    except subprocess.SubprocessError as error:
        raise CauseError('causal source is unavailable') from error


def _origin(repo, base, value):
    if not isinstance(value, dict) or set(value) != {'claim', 'path', 'line'} or not _text(value['claim']):
        raise CauseError('causal hypotheses need a claim and a committed source line')
    path, line = value['path'], value['line']
    if (not _text(path, 400) or PurePosixPath(path).is_absolute() or '\\' in path
            or any(part in ('', '.', '..', '.git') for part in path.split('/'))
            or not path.isprintable() or type(line) is not int or line < 1):
        raise CauseError('invalid causal hypothesis source location')
    raw = _blob(repo, base, path)
    decoded = raw.decode('utf-8')
    lines = decoded.splitlines()
    if '\0' in decoded or len(lines) < line or not _text(lines[line - 1], 1500):
        raise CauseError('causal hypothesis source line does not exist')
    return {**value, 'source': lines[line - 1], 'blob_sha256': hashlib.sha256(raw).hexdigest()}


def _id(value):
    return isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,31}', value)


def _outcome(item):
    return (isinstance(item, dict) and set(item) == {'exit_code', 'marker'}
            and _text(item['marker'], 200) and item['marker'].isprintable()
            and type(item['exit_code']) is int and 0 <= item['exit_code'] <= 255)


def _command(value):
    return (isinstance(value, list) and 2 <= len(value) <= 40
            and all(_text(arg, 8192) and '\0' not in arg for arg in value))


def _spec(item):
    if (not isinstance(item, dict) or not _command(item.get('command'))
            or not _outcome({key: value for key, value in item.items() if key != 'command'})):
        raise CauseError('causal runs need a bounded command, exit code, and observable marker')
    return item


def _matched(first, second, index):
    if (not _command(second) or len(first) != len(second) or first[:index + 1] != second[:index + 1]
            or sum(a != b for a, b in zip(first, second)) != 1):
        raise CauseError('causal comparison requires one changed argument on the same tracked entrypoint')


def _search_contract(item, proof, repo, base, index):
    hypotheses = item['hypotheses']
    if not isinstance(hypotheses, list) or not 2 <= len(hypotheses) <= 5:
        raise CauseError('adaptive investigation requires two to five distinct causal hypotheses')
    anchors = {}
    claims = set()
    for row in hypotheses:
        if (not isinstance(row, dict) or not _id(row.get('id')) or row['id'] in anchors):
            raise CauseError('each hypothesis requires a unique short ID')
        anchor = _origin(repo, base, {key: value for key, value in row.items() if key != 'id'})
        claim = anchor['claim'].strip().casefold()
        if claim in claims:
            raise CauseError('each causal hypothesis claim must be distinct')
        claims.add(claim)
        anchors[row['id']] = anchor
    investigation = item['investigation']
    if not isinstance(investigation, list) or not 1 <= len(investigation) <= 6:
        raise CauseError('adaptive investigation requires one to six bounded probes')
    probes = {}
    commands = set()
    for row in investigation:
        if (not isinstance(row, dict) or set(row) != {'id', 'command', 'outcomes', 'predictions', 'candidate'}
                or not _id(row['id']) or row['id'] in probes):
            raise CauseError('each investigation probe needs an ID, a command, outcomes, predictions and candidate check')
        command, outcomes, predictions, candidate = (row[key] for key in ('command', 'outcomes', 'predictions', 'candidate'))
        _matched(proof['trigger']['command'], command, index)
        if tuple(command) in commands:
            raise CauseError('each investigation probe must use a distinct control command')
        commands.add(tuple(command))
        if (not isinstance(outcomes, dict) or not 2 <= len(outcomes) <= 4
                or any(not _id(name) or not _outcome(value) for name, value in outcomes.items())
                or len({(value['exit_code'], value['marker']) for value in outcomes.values()}) != len(outcomes)
                or not isinstance(predictions, dict) or set(predictions) != set(anchors)
                or any(not _id(value) or value not in outcomes for value in predictions.values())
                or len(set(predictions.values())) < 2):
            raise CauseError('probe outcomes must be distinct and predict a real split over all hypotheses')
        if any(left['exit_code'] == right['exit_code'] and left['marker'] in right['marker']
               for left_name, left in outcomes.items() for right_name, right in outcomes.items()
               if left_name != right_name):
            raise CauseError('probing outcomes with the same exit code need non-overlapping markers')
        if not _outcome(candidate) or proof['trigger']['marker'] in candidate['marker']:
            raise CauseError('candidate probe must describe a distinct healthy outcome')
        probes[row['id']] = {'command': command, 'outcomes': outcomes, 'predictions': predictions,
                             'control': {'command': command, **candidate}}
    if len({tuple(probe['predictions'][name] for probe in probes.values()) for name in anchors}) != len(anchors):
        raise CauseError('the investigation cannot distinguish every pair of hypotheses')
    return {**proof, 'hypotheses': anchors, 'probes': probes}


def contract(data, plan, source_mode='workspace'):
    if source_mode not in ('workspace', 'image'):
        raise CauseError('unsupported causal source mode')
    repo, base = data['repo'], data['base_commit']
    item = plan.get('root_cause')
    if not isinstance(item, dict):
        raise CauseError('a causal task requires a mechanism and matched hypothesis probes')
    keys = set(item)
    legacy = keys == {'mechanism', 'alternative', 'control'}
    adaptive = keys == {'hypotheses', 'investigation'}
    if legacy:
        items = [{'id': 'control', 'alternative': item['alternative'], 'control': item['control']}]
    elif keys == {'mechanism', 'probes'} and isinstance(item['probes'], list) and 1 <= len(item['probes']) <= 4:
        items = item['probes']
    elif not adaptive:
        raise CauseError('a causal task requires a mechanism and one to four matched hypothesis probes')
    trigger, fixed = [_spec(plan[key]) for key in ('reproduction', 'resolution')]
    first = trigger['command']
    prefix = '/app/' if source_mode == 'image' else '/workspace/'
    index = 0 if first[0].startswith(prefix) else 1
    script = first[index]
    mapped = first.copy()
    if script.startswith(prefix):
        mapped[index] = '/workspace/' + script.removeprefix(prefix)
    if not script.startswith(prefix) or not workflow._workspace_command(mapped) or fixed['command'] != first:
        raise CauseError('causal comparison requires one changed argument on the same tracked entrypoint')
    entry = script.removeprefix(prefix)
    if not entry or any(part in ('', '.', '..', '.git') for part in entry.split('/')) or '\\' in entry:
        raise CauseError('invalid causal driver path')
    driver_sha = hashlib.sha256(_blob(repo, base, entry)).hexdigest()
    if trigger['marker'] in fixed['marker']:
        raise CauseError('healthy outcomes must be distinguishable from the original fault marker')
    proof = {'base': base, 'source_mode': source_mode, 'driver': entry, 'driver_sha256': driver_sha,
             'trigger': trigger, 'fixed': fixed, 'legacy': legacy}
    if adaptive:
        return _search_contract(item, proof, repo, base, index)
    mechanism = _origin(repo, base, item['mechanism'])
    claims = {mechanism['claim'].strip().casefold()}
    commands = set()
    probes = {}
    for item in items:
        if (not isinstance(item, dict) or set(item) != {'id', 'alternative', 'control'}
                or not _id(item['id']) or item['id'] in probes):
            raise CauseError('each causal probe needs a unique short ID, alternative, and control')
        control = _spec(item['control'])
        second = control['command']
        _matched(first, second, index)
        if trigger['marker'] in control['marker']:
            raise CauseError('healthy outcomes must be distinguishable from the original fault marker')
        alternative = _origin(repo, base, item['alternative'])
        claim = alternative['claim'].strip().casefold()
        if claim in claims or tuple(second) in commands:
            raise CauseError('each competing hypothesis and its control command must be distinct')
        claims.add(claim)
        commands.add(tuple(second))
        probes[item['id']] = {'alternative': alternative, 'control': control}
    return {**proof, 'mechanism': mechanism, 'probes': probes}


def references(paths):
    return [{'path': str(path), 'sha256': cli.digest(path)} for path in paths]


def prompt(proof):
    return json.dumps(proof, ensure_ascii=False)


def next_probe(proof, remaining, used):
    """Choose the probe that minimizes the largest possible survivor set."""
    best = None
    best_split = 0
    for name, probe in proof['probes'].items():
        if name in used:
            continue
        counts = Counter(probe['predictions'][hypothesis] for hypothesis in remaining)
        split = len(remaining) - max(counts.values())
        if split > best_split:
            best, best_split = name, split
    if best is None:
        raise CauseError('no remaining probe can distinguish the surviving hypotheses')
    return best


def search_step(proof, name, outcome, prior):
    remaining = prior[-1]['remaining'] if prior else list(proof['hypotheses'])
    if len(remaining) < 2 or name != next_probe(proof, remaining, {step['probe'] for step in prior}):
        raise CauseError('causal search did not use the strongest available split')
    predictions = proof['probes'][name]['predictions']
    surviving = [hypothesis for hypothesis in remaining if predictions[hypothesis] == outcome]
    if not surviving:
        raise CauseError(f'observation {outcome} contradicts every remaining hypothesis for probe {name}')
    return {'probe': name, 'outcome': outcome, 'before': remaining,
            'eliminated': [hypothesis for hypothesis in remaining if hypothesis not in surviving],
            'remaining': surviving}


def search_trace(proof, baseline):
    trace = baseline.get('search_trace')
    if not isinstance(trace, list) or not 1 <= len(trace) < len(proof['hypotheses']):
        raise CauseError('causal search trace is missing or exceeds the hypothesis bound')
    prior = []
    for step in trace:
        if (not isinstance(step, dict) or not _id(step.get('probe')) or not _id(step.get('outcome'))
                or step != search_step(proof, step['probe'], step['outcome'], prior)):
            raise CauseError('the preserved causal elimination trace changed')
        prior.append(step)
    if len(prior[-1]['remaining']) != 1:
        raise CauseError('causal search did not establish a single surviving hypothesis')
    return prior


def search_review_shape(proof, baseline):
    trace = search_trace(proof, baseline)
    shape = {'status': 'ready', 'verdict': 'PASS', 'cause_verdict': {
        'mechanism': 'supported', 'survivor': trace[-1]['remaining'][0], 'trigger_quote': '<literal output>',
        'probes': {step['probe']: {'verdict': 'ruled-out', 'reason': '<which behavior excludes each hypothesis>',
                                  'quote': '<literal output>', 'eliminated': step['eliminated']}
                   for step in trace},
    }}
    return ('\nRESPONSE SHAPE: If the evidence supports PASS, replace the example reason and quote values '
            'and return one JSON object of this exact shape. Put probes inside cause_verdict. '
            'Otherwise return blocked.\n' + prompt(shape))


def probe_runs(proof, evidence):
    if proof['legacy']:
        return {'control': evidence['control']}
    runs = evidence.get('probe_runs')
    names = set(proof['probes'])
    if 'hypotheses' in proof:
        names = {step['probe'] for step in search_trace(proof, evidence.get('baseline', evidence))}
    if not isinstance(runs, dict) or set(runs) != names:
        raise CauseError('every frozen causal probe needs a matched pair of reports')
    return runs


def _require_quote(refs, quote, label):
    if not _text(quote, 400) or not quote.isprintable():
        raise ReviewFormatError(f'independent review must quote a single output line from {label}')
    for ref in refs:
        record = cli.read(Path(ref['path']))
        if not any(quote in line for stream in ('stdout', 'stderr') for line in record.get(stream, '').splitlines()):
            raise ReviewFormatError(f'independent review quote is missing from the raw {label}')


def baseline_verdict(value, baseline=None, proof=None):
    if proof is not None and not proof['legacy']:
        trace = search_trace(proof, baseline) if 'hypotheses' in proof else None
        fields = {'mechanism', 'trigger_quote', 'probes', 'evidence'}
        if trace:
            fields.add('survivor')
        runs = probe_runs(proof, baseline)
        if (not isinstance(value, dict) or set(value) - fields or not isinstance(value.get('probes'), dict)
                or ('evidence' in value and not _text(value['evidence'], 2000))):
            raise ReviewFormatError('cause_verdict must contain its own probes map and the requested review fields')
        if value.get('mechanism') != 'supported' or set(value['probes']) != set(runs):
            raise CauseError('independent review must decide every competing hypothesis')
        eliminated = {}
        if trace:
            if value.get('survivor') != trace[-1]['remaining'][0]:
                raise CauseError('independent review did not establish the surviving hypothesis')
            eliminated = {step['probe']: set(step['eliminated']) for step in trace}
        _require_quote(baseline['trigger'], value.get('trigger_quote'), 'trigger')
        fields = {'verdict', 'reason', 'quote', 'eliminated'} if trace else {'verdict', 'reason', 'quote'}
        for name, decision in value['probes'].items():
            if (not isinstance(decision, dict) or set(decision) != fields
                    or decision['verdict'] != 'ruled-out' or not _text(decision['reason'], 2000)):
                raise CauseError(f'independent review did not rule out hypothesis {name}')
            if trace:
                removed = decision['eliminated']
                if (not isinstance(removed, list) or any(not _id(item) for item in removed)
                        or len(removed) != len(eliminated[name]) or set(removed) != eliminated[name]):
                    raise CauseError(f'independent review did not account for every exclusion at probe {name}')
            _require_quote(runs[name], decision['quote'], f'probe {name}')
        return value
    if (not isinstance(value, dict) or set(value) != {'mechanism', 'alternative', 'reason'}
            or value['mechanism'] != 'supported' or value['alternative'] != 'ruled-out'
            or not _text(value['reason'], 2000)):
        raise CauseError('independent review did not establish the mechanism and rule out the alternative')
    return value


def require_baseline_review(baseline, proof):
    from workbench import autopilot

    receipt = baseline['review_receipt']
    path = Path(receipt['path'])
    if (path.is_symlink() or path.parent != Path(baseline['trigger'][1]['path']).parent
            or not path.name.startswith('cause-baseline-') or not path.is_file()
            or path.stat().st_size > 1_000_000 or cli.digest(path) != receipt['sha256']):
        raise CauseError('the preserved baseline verdict receipt changed')
    raw = json.loads(path.read_bytes())
    if raw.get('exit_code') != 0 or raw.get('timed_out'):
        raise CauseError('the preserved baseline review did not complete')
    try:
        review = autopilot._result(raw['stdout'])
    except autopilot.AutopilotError as error:
        raise CauseError('the preserved baseline verdict is invalid') from error
    if (review.get('verdict') != 'PASS'
            or baseline_verdict(review.get('cause_verdict'), baseline, proof) != baseline['verdict']):
        raise CauseError('the baseline review does not confirm this causal verdict')


def explanation(review):
    reason = review.get('causal_explanation')
    if review.get('cause_fixed') is not True or not _text(reason, 2000):
        raise CauseError('independent review did not confirm that the candidate addresses the proven mechanism')
    return reason


def _pair(data, commit, kind, refs, spec, proof, verifier, *, healthy, containers, observed=None):
    from workbench import delivery

    if (not isinstance(refs, list) or len(refs) != 2 or refs[0] == refs[1]
            or not all(isinstance(ref, dict) for ref in refs) or data['implementer'] == verifier):
        raise CauseError('both independent sandbox identities are required for the causal comparison')
    environments = []
    for actor, ref in zip((data['implementer'], verifier), refs):
        entry = next((e for e in data['evidence'] if (not e.get('superseded')
                      and e.get('path') == ref.get('path') and e.get('sha256') == ref.get('sha256')
                      and e.get('kind') == kind and e.get('commit') == commit and e.get('actor') == actor)), None)
        if (entry is None or cli.digest(entry['path']) != entry['sha256']
                or not delivery._valid_run(entry, actor, kind, commit, data['repo'])):
            raise CauseError('causal comparison sandbox receipt is absent, altered, or unrelated')
        record = json.loads(Path(entry['path']).read_bytes())
        if not proof['legacy']:
            container = record.get('create_stdout')
            if (not isinstance(container, str) or not re.fullmatch(r'[0-9a-f]{64}', container.strip())
                    or container.strip() in containers
                    or record.get('create_stdout_truncated') is not False
                    or record.get('workspace_read_only') is not True):
                raise CauseError('each causal observation requires a fresh read-only Docker container')
            containers.add(container.strip())
        output = record['stdout'] + '\n' + record.get('stderr', '')
        if (record.get('source_mode') != proof['source_mode'] or record.get('command') != spec['command']
                or record.get('expected_marker') != spec['marker'] or record.get('expected_exit') != spec['exit_code']
                or record.get('stdout_truncated') or record.get('stderr_truncated')
                or spec['marker'] not in output or (healthy and proof['trigger']['marker'] in output)):
            raise CauseError('observed output does not prove the causal comparison')
        if observed is not None:
            outcome, options = observed
            if record.get('observed_outcome') != outcome or workflow.matching_observation(options, record) != outcome:
                raise CauseError('archived execution does not establish the recorded causal observation')
        environments.append(record.get('verified_image_id') or record.get('image'))
    if (not all(isinstance(image, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', image) for image in environments)
            or environments[0] != environments[1]):
        raise CauseError('causal comparison did not use a common sandbox image')
    return environments[0]


def verify_baseline(data, evidence, proof, verifier):
    base = data['base_commit']
    containers = set()
    trigger = _pair(data, base, 'reproduction', evidence['trigger'], proof['trigger'], proof, verifier,
                    healthy=False, containers=containers)
    _verify_controls(data, base, evidence, proof, verifier, trigger, containers, baseline=True)
    return trigger


def _verify_controls(data, commit, evidence, proof, verifier, image, containers, *, baseline=False):
    runs = probe_runs(proof, evidence)
    outcomes = ({step['probe']: step['outcome'] for step in search_trace(proof, evidence)}
                if baseline and 'hypotheses' in proof else {})
    for name, refs in runs.items():
        probe = proof['probes'][name]
        observed = None
        spec = probe['control']
        if name in outcomes:
            outcome, options = outcomes[name], probe['outcomes']
            spec = {'command': probe['command'], **options[outcome]}
            observed = (outcome, options)
        control = _pair(data, commit, 'runtime-log', refs, spec, proof, verifier,
                        healthy=observed is None, containers=containers, observed=observed)
        if image != control:
            raise CauseError(f'causal trigger and probe {name} used different images')


def verify_driver(data, head, proof):
    if hashlib.sha256(_blob(data['repo'], head, proof['driver'])).hexdigest() != proof['driver_sha256']:
        raise CauseError('the candidate changed the frozen causal driver')


def validate(data, head, record, verifier):
    if data.get('root_cause') != MODE:
        if record is not None or data.get('root_cause') is not None:
            raise CauseError('unexpected causal review for this task')
        return
    if not isinstance(record, dict) or record.get('head') != head or not _text(record.get('explanation'), 2000):
        raise CauseError('candidate has no matching independent causal review')
    baseline = record['baseline']
    plan = baseline['plan']
    proof = contract(data, plan, baseline['source_mode'])
    baseline_image = verify_baseline(data, baseline, proof, verifier)
    require_baseline_review(baseline, proof)
    containers = set()
    fixed = _pair(data, head, 'resolution', record['trigger'], proof['fixed'], proof, verifier,
                  healthy=True, containers=containers)
    if proof['source_mode'] == 'workspace' and fixed != baseline_image:
        raise CauseError('causal comparison used an unexpected sandbox image')
    _verify_controls(data, head, record, proof, verifier, fixed, containers)
    verify_driver(data, head, proof)
