"""Bind a fault mechanism to an isolated comparison on the original and fixed code."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

from workbench import cli, workflow


MODE = 'paired-control-v1'
INSTRUCTION = (
    'Include "root_cause": {"mechanism":{"claim":"why the fault happens","path":"source file","line":1},'
    '"alternative":{"claim":"a plausible competing explanation","path":"source file","line":1},'
    '"control":{"command":["..."],"marker":"observed healthy outcome","exit_code":0}}. '
    'Use a tracked entrypoint on the actual product surface. Reproduction and resolution must use the exact '
    'same command. Change exactly one argument after the script for the control so the proposed cause is '
    'absent while the competing explanation is still exercised. Choose distinct failure and success markers. '
    'If no meaningful comparison is possible, return blocked.'
)
BASELINE_INSTRUCTION = (
    'Check the source anchors and the trigger/control output from both independent executions. '
    'Confirm that the control removes only the proposed cause while the competing explanation remains '
    'exercised. If justified, include "cause_verdict":{"mechanism":"supported",'
    '"alternative":"ruled-out","reason":"why the contrast distinguishes these explanations"}. '
    'Return blocked for instrumentation that merely prints fabricated observations, or an inconclusive comparison.\n'
)
FINAL_INSTRUCTION = (
    'Confirm the same trigger now succeeds, the unchanged control is still healthy, and the diff addresses '
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
    if not isinstance(item, dict) or set(item) != {'mechanism', 'alternative', 'control'}:
        raise CauseError('a causal task requires a mechanism, an alternative, and a matched control')
    trigger, fixed, control = [_spec(value) for value in (plan['reproduction'], plan['resolution'], item['control'])]
    first, second = trigger['command'], control['command']
    prefix = '/app/' if source_mode == 'image' else '/workspace/'
    index = 0 if first[0].startswith(prefix) else 1
    script = first[index]
    mapped = first.copy()
    if script.startswith(prefix):
        mapped[index] = '/workspace/' + script.removeprefix(prefix)
    if (not script.startswith(prefix) or not workflow._workspace_command(mapped)
            or len(first) != len(second) or first[:index + 1] != second[:index + 1]
            or sum(a != b for a, b in zip(first, second)) != 1 or fixed['command'] != first):
        raise CauseError('causal comparison requires one changed argument on the same tracked entrypoint')
    entry = script.removeprefix(prefix)
    if not entry or any(part in ('', '.', '..', '.git') for part in entry.split('/')) or '\\' in entry:
        raise CauseError('invalid causal driver path')
    driver_sha = hashlib.sha256(_blob(repo, base, entry)).hexdigest()
    if trigger['marker'] in fixed['marker'] or trigger['marker'] in control['marker']:
        raise CauseError('healthy outcomes must be distinguishable from the original fault marker')
    mechanism, alternative = [_origin(repo, base, item[key]) for key in ('mechanism', 'alternative')]
    if mechanism['claim'].casefold() == alternative['claim'].casefold():
        raise CauseError('the competing hypothesis must be distinct')
    return {'base': base, 'source_mode': source_mode, 'driver': entry, 'driver_sha256': driver_sha,
            'trigger': trigger, 'fixed': fixed, 'control': control, 'mechanism': mechanism, 'alternative': alternative}


def references(paths):
    return [{'path': str(path), 'sha256': cli.digest(path)} for path in paths]


def prompt(proof):
    return json.dumps(proof, ensure_ascii=False)


def baseline_verdict(value):
    if (not isinstance(value, dict) or set(value) != {'mechanism', 'alternative', 'reason'}
            or value['mechanism'] != 'supported' or value['alternative'] != 'ruled-out'
            or not _text(value['reason'], 2000)):
        raise CauseError('independent review did not establish the mechanism and rule out the alternative')
    return value


def require_baseline_review(baseline):
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
    if review.get('verdict') != 'PASS' or baseline_verdict(review.get('cause_verdict')) != baseline['verdict']:
        raise CauseError('the baseline review does not confirm this causal verdict')


def explanation(review):
    reason = review.get('causal_explanation')
    if review.get('cause_fixed') is not True or not _text(reason, 2000):
        raise CauseError('independent review did not confirm that the candidate addresses the proven mechanism')
    return reason


def _pair(data, commit, kind, refs, spec, proof, verifier, *, healthy):
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
    trigger = _pair(data, base, 'reproduction', evidence['trigger'], proof['trigger'], proof, verifier, healthy=False)
    control = _pair(data, base, 'runtime-log', evidence['control'], proof['control'], proof, verifier, healthy=True)
    if trigger != control:
        raise CauseError('baseline trigger and control used different images')
    return trigger


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
    require_baseline_review(baseline)
    fixed = _pair(data, head, 'resolution', record['trigger'], proof['fixed'], proof, verifier, healthy=True)
    control = _pair(data, head, 'runtime-log', record['control'], proof['control'], proof, verifier, healthy=True)
    if fixed != control or (proof['source_mode'] == 'workspace' and fixed != baseline_image):
        raise CauseError('causal comparison used an unexpected sandbox image')
    verify_driver(data, head, proof)
