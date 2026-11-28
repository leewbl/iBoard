"""Bind a fault mechanism to isolated comparisons on the original and fixed code."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

from workbench import cli, workflow


MODE = 'paired-control-v1'
INSTRUCTION = (
    'Include "root_cause": {"mechanism":{"claim":"why the fault happens","path":"source file","line":1},'
    '"probes":[{"id":"short-name","alternative":{"claim":"a plausible competing explanation",'
    '"path":"source file","line":1},"control":{"command":["..."],"marker":"observed healthy outcome",'
    '"exit_code":0}}]}. Identify up to four plausible competing explanations from the affected code and '
    'available regression history. Give each explanation a distinct control, ordered by diagnostic value. '
    'Use a tracked entrypoint on the actual product surface. Reproduction and resolution must use the exact '
    'same command. For each control, change exactly one argument after the script so the proposed cause is '
    'absent while that competing explanation is still exercised. Choose distinct failure and success markers. '
    'If a plausible competing explanation cannot be distinguished using an existing driver, return blocked.'
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
FINAL_INSTRUCTION = (
    'Confirm the same trigger now succeeds, every unchanged control is still healthy, and the diff addresses '
    'the mechanism found before any edit. Assess unexpected output and omit a PASS if the original failure '
    'is still present. Include "cause_fixed": true and a concrete "causal_explanation" of the mechanism and fix.\n'
)


class CauseError(ValueError):
    pass


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


def _spec(item):
    if (not isinstance(item, dict) or set(item) != {'command', 'marker', 'exit_code'}
            or not isinstance(item['command'], list) or not 2 <= len(item['command']) <= 40
            or any(not _text(arg, 8192) or '\0' in arg for arg in item['command'])
            or not _text(item['marker'], 200) or not item['marker'].isprintable()
            or type(item['exit_code']) is not int or not 0 <= item['exit_code'] <= 255):
        raise CauseError('causal runs need a bounded command, exit code, and observable marker')
    return item


def contract(data, plan, source_mode='workspace'):
    if source_mode not in ('workspace', 'image'):
        raise CauseError('unsupported causal source mode')
    repo, base = data['repo'], data['base_commit']
    item = plan.get('root_cause')
    if not isinstance(item, dict):
        raise CauseError('a causal task requires a mechanism and matched hypothesis probes')
    legacy = set(item) == {'mechanism', 'alternative', 'control'}
    if legacy:
        items = [{'id': 'control', 'alternative': item['alternative'], 'control': item['control']}]
    elif set(item) == {'mechanism', 'probes'} and isinstance(item['probes'], list) and 1 <= len(item['probes']) <= 4:
        items = item['probes']
    else:
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
    mechanism = _origin(repo, base, item['mechanism'])
    claims = {mechanism['claim'].strip().casefold()}
    commands = set()
    probes = {}
    for item in items:
        if (not isinstance(item, dict) or set(item) != {'id', 'alternative', 'control'}
                or not isinstance(item['id'], str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', item['id'])
                or item['id'] in probes):
            raise CauseError('each causal probe needs a unique short ID, alternative, and control')
        control = _spec(item['control'])
        second = control['command']
        if (len(first) != len(second) or first[:index + 1] != second[:index + 1]
                or sum(a != b for a, b in zip(first, second)) != 1):
            raise CauseError('causal comparison requires one changed argument on the same tracked entrypoint')
        if trigger['marker'] in control['marker']:
            raise CauseError('healthy outcomes must be distinguishable from the original fault marker')
        alternative = _origin(repo, base, item['alternative'])
        claim = alternative['claim'].strip().casefold()
        if claim in claims or tuple(second) in commands:
            raise CauseError('each competing hypothesis and its control command must be distinct')
        claims.add(claim)
        commands.add(tuple(second))
        probes[item['id']] = {'alternative': alternative, 'control': control}
    return {'base': base, 'source_mode': source_mode, 'driver': entry, 'driver_sha256': driver_sha,
            'trigger': trigger, 'fixed': fixed, 'mechanism': mechanism, 'probes': probes, 'legacy': legacy}


def references(paths):
    return [{'path': str(path), 'sha256': cli.digest(path)} for path in paths]


def prompt(proof):
    return json.dumps(proof, ensure_ascii=False)


def probe_runs(proof, evidence):
    if proof['legacy']:
        return {'control': evidence['control']}
    runs = evidence.get('probe_runs')
    if not isinstance(runs, dict) or set(runs) != set(proof['probes']):
        raise CauseError('every frozen causal probe needs a matched pair of reports')
    return runs


def _require_quote(refs, quote, label):
    if not _text(quote, 400) or not quote.isprintable():
        raise CauseError(f'independent review must quote a single output line from {label}')
    for ref in refs:
        record = cli.read(Path(ref['path']))
        if not any(quote in line for stream in ('stdout', 'stderr') for line in record.get(stream, '').splitlines()):
            raise CauseError(f'independent review quote is missing from the raw {label}')


def baseline_verdict(value, baseline=None, proof=None):
    if proof is not None and not proof['legacy']:
        if (not isinstance(value, dict) or set(value) - {'mechanism', 'trigger_quote', 'probes', 'evidence'}
                or value.get('mechanism') != 'supported' or not isinstance(value.get('probes'), dict)
                or ('evidence' in value and not _text(value['evidence'], 2000))
                or set(value['probes']) != set(proof['probes'])):
            raise CauseError('independent review must decide every competing hypothesis')
        _require_quote(baseline['trigger'], value.get('trigger_quote'), 'trigger')
        runs = probe_runs(proof, baseline)
        for name, decision in value['probes'].items():
            if (not isinstance(decision, dict) or set(decision) != {'verdict', 'reason', 'quote'}
                    or decision['verdict'] != 'ruled-out' or not _text(decision['reason'], 2000)):
                raise CauseError(f'independent review did not rule out hypothesis {name}')
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


def _pair(data, commit, kind, refs, spec, proof, verifier, *, healthy, containers):
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
    _verify_controls(data, base, evidence, proof, verifier, trigger, containers)
    return trigger


def _verify_controls(data, commit, evidence, proof, verifier, image, containers):
    runs = probe_runs(proof, evidence)
    for name, probe in proof['probes'].items():
        control = _pair(data, commit, 'runtime-log', runs[name], probe['control'], proof, verifier,
                        healthy=True, containers=containers)
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
