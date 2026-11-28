"""Freeze and authorize one reporter-selected correction to reproduction setup."""

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from workbench import (benny, benny_correction_language, benny_handoff, benny_notice, benny_reproduce, benny_triage,
                       cli, project_triage, project_verify)
from workbench.benny_slack import SlackError


KIND = 'benny-correction-options'
NAME = re.compile(r'[a-z][a-z0-9-]{0,39}')
SHA256 = re.compile(r'[0-9a-f]{64}')
CORRECTION = re.compile(r'(?:correction|更正)[：:][ \t]*(?:setup|设置)[：:][ \t]*([a-z][a-z0-9-]{0,39})', re.I)
PATH_FIELDS = {'manifest', 'helpers', 'oracle'}
OBSERVED_FIELDS = ('id', 'marker', 'failure_marker', 'observe', 'source', 'entrypoints', 'browser', 'screenshot',
                   'recording')


def _reference(path):
    return {'path': str(path), 'sha256': cli.digest(path)}


def _scope(runtime):
    return {key: str(runtime[key]) for key in ('repo', 'image', 'journey', 'oracle_marker', 'oracle_failure_marker')} | {
        'memory_mb': runtime.get('memory_mb', 3328),
    }


def _recipe(runtime):
    manifest = project_verify._load_contract(runtime['manifest'])
    project, doctor, primary, oracle, files, digests = project_triage._inputs(
        **{key: runtime[key] for key in PATH_FIELDS | {'journey', 'oracle_marker', 'oracle_failure_marker'}},
    )
    selected = next((check for check in manifest['features'] if check['id'] == runtime['journey']), None)
    if (manifest['project'], manifest['doctor'], selected) != (project, doctor, primary):
        raise ValueError('Benny correction manifest changed while freezing the setup')
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    recipe = {'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
              'surface': manifest['surface'], 'oracle': oracle['helper'],
              'contract': {'project': project, 'doctor': doctor, 'journey': primary, 'independent': oracle,
                           'helper_digests': digests}}
    return recipe, {'manifest.json': manifest_bytes, **{f'helpers/{name}': data for name, data in files.items()}}


def _observation(recipe):
    contract = recipe['contract']
    return (contract['project'], recipe['surface'],
            tuple(contract['doctor'].get(key) for key in OBSERVED_FIELDS),
            tuple(contract['journey'].get(key) for key in OBSERVED_FIELDS),
            tuple(contract['independent'][key] for key in ('marker', 'failure_marker')))


def _bundle(path):
    return path.with_name(f'{path.stem}.inputs')


def _receipt_path(reference):
    if (not isinstance(reference, dict) or reference.keys() != {'path', 'sha256'}
            or not isinstance(reference['path'], str) or not isinstance(reference['sha256'], str)
            or not SHA256.fullmatch(reference['sha256'])):
        raise ValueError('Benny correction needs exact artifact references')
    path = Path(reference['path'])
    if path.resolve() != path:
        raise ValueError('Benny correction artifact must use its canonical path')
    return path


def _archive(reference):
    path = _receipt_path(reference)
    if not path.is_file():
        raise ValueError('Benny correction setup archive must be a regular file')
    return path, benny_handoff._load(path, reference['sha256'], KIND)


def freeze(path, variants, runtime):
    """Copy the operator-approved alternatives before the original probes execute."""
    if not isinstance(variants, dict) or not 1 <= len(variants) <= 8:
        raise ValueError('configure one to eight approved Benny correction setups')
    path = Path(path).expanduser().absolute()
    if path.resolve() != path:
        raise ValueError('Benny correction archive must use its canonical event path')
    original, _ = _recipe(runtime)
    approved, files, guidance = {}, {}, {}
    for name, overrides in variants.items():
        if (not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(overrides, dict)
                or not PATH_FIELDS <= overrides.keys() or overrides.keys() - PATH_FIELDS - {'applies_when'}
                or not all(isinstance(overrides[key], (str, Path)) and Path(overrides[key]).is_absolute()
                           for key in PATH_FIELDS)):
            raise ValueError('each correction setup needs a named manifest, helpers directory and independent probe')
        if 'applies_when' in overrides:
            description = overrides['applies_when']
            if not benny_triage._text(description, 360) or not description.isprintable() or len(description.strip()) < 12:
                raise ValueError('Benny correction applicability needs a concise, concrete setup description')
            guidance[name] = description.strip()
        recipe, contents = _recipe({**runtime, **overrides})
        if _observation(recipe) != _observation(original):
            raise ValueError('a Benny correction setup cannot change the observable acceptance contract')
        approved[name] = recipe
        files.update({f'{name}/{target}': data for target, data in contents.items()})
    archive = {'schema': cli.SCHEMA, 'kind': KIND, 'scope': _scope(runtime), 'original': original, 'setups': approved}
    if guidance:
        archive['guidance'] = guidance
    if path.exists():
        reference = _reference(path)
        if _archive(reference)[1] != archive:
            raise ValueError('Benny correction setup changed after it was frozen')
        for name in approved:
            options(reference, name, runtime)
        return reference
    if _bundle(path).exists() or _bundle(path).is_symlink():
        raise ValueError('Benny correction has an incomplete setup archive')
    with tempfile.TemporaryDirectory(prefix='.correction-setup-', dir=path.parent) as scratch:
        source = Path(scratch)/'inputs'
        source.mkdir()
        for name, data in files.items():
            target = source/name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        os.rename(source, _bundle(path))
    cli.write(path, archive)
    return _reference(path)


def options(archive, name, runtime):
    """Return only the paths copied into the matching frozen setup bundle."""
    path, state = _archive(archive)
    if not isinstance(name, str) or not NAME.fullmatch(name) or name not in state['setups']:
        raise ValueError('Benny correction setup was not approved by the operator')
    if _scope(runtime) != state['scope']:
        raise ValueError('Benny correction changed the original runtime scope')
    approved = state['setups'][name]
    folder = _bundle(path)/name
    if folder.resolve() != folder:
        raise ValueError('Benny correction inputs must remain in the frozen setup bundle')
    updated = {**runtime, 'manifest': str(folder/'manifest.json'), 'helpers': str(folder/'helpers'),
               'oracle': str(folder/'helpers'/approved['oracle'])}
    manifest = Path(updated['manifest'])
    if manifest.is_symlink() or not manifest.is_file() or cli.digest(manifest) != approved['manifest_sha256']:
        raise ValueError('Benny correction manifest changed after setup approval')
    if _recipe(updated)[0] != approved:
        raise ValueError('Benny correction probes changed after setup approval')
    return updated


def _bound_inputs(event, initial, notice, archive):
    initial_path = event/'reproduction/result.json'
    if (_receipt_path(initial) != initial_path or _receipt_path(notice) != event/'source-notice.json'
            or _receipt_path(archive) != event/'correction-options.json'):
        raise ValueError('Benny correction needs its original event and setup archive')
    _, approved = _archive(archive)
    original = benny_handoff._load(initial_path, initial['sha256'], 'benny-reproduction')
    _, route = benny.load_route(original['route']['path'], original['route']['sha256'])
    # The original must be terminal before calling the native audit again.
    if 'correction' in route:
        raise ValueError('Benny already accepted one correction for this report')
    _, proof, contract, _ = benny_reproduce._approved(initial_path, initial['sha256'])
    _require_contract(contract, approved['original'], 'original')
    row = benny_notice.confirmed(notice, reproduction=initial, proof=proof)
    return proof, route, row, approved


def _require_contract(contract, recipe, side):
    expected = recipe['contract']
    if {key: contract.get(key) for key in expected} != expected:
        raise ValueError(f'{side} reproduction differs from the frozen correction plan')


def _approved_message(route, row, current, ready_at):
    frozen, rules = route['snapshot'], route['rules']
    prior = [*frozen['messages'], row]
    current_rows = current['messages']
    if (current['permalink'] != frozen['permalink'] or current['location'] != frozen['location']
            or current_rows[:len(prior)] != prior or len(current_rows) not in (len(prior), len(prior) + 1)):
        raise SlackError('Benny source changed beyond one allowed setup correction')
    if current_rows == prior:
        return None
    message = current_rows[-1]
    if (not isinstance(message, dict) or message.keys() != row.keys()
            or not isinstance(message['ts'], str) or not benny.STAMP.fullmatch(message['ts'])
            or not isinstance(message['text'], str) or benny._ticks(message['ts']) <= benny._ticks(row['ts'])):
        raise SlackError('Benny source changed without an approved in-window setup correction from the reporter')
    reporter = frozen['messages'][0]
    if (reporter['bot'] or message['bot'] or message['author'] != reporter['author'] or message['files']
            or not benny._ticks(row['ts']) < benny._ticks(message['ts']) < round(ready_at * 1_000_000)):
        raise SlackError('Benny source changed without an approved in-window setup correction from the reporter')
    if benny.route(current, rules, route['selected_artifact']) != route['decision']:
        raise SlackError('Benny correction no longer matches the approved source decision')
    return message


def _named_setup(message, approved):
    match = CORRECTION.fullmatch(message['text'].strip())
    if not match:
        return None
    if match[1] not in approved['setups']:
        raise SlackError('Benny source changed without an approved in-window setup correction from the reporter')
    return match[1]


def select(*, initial, notice, archive, slack, ready_at, out, model=None):
    """Confirm one in-window addition from the original reporter and freeze its source route."""
    out = Path(out).expanduser().absolute()
    event = out.parent.parent
    if out != event/'correction/source' or out.resolve() != out:
        raise ValueError('Benny correction needs its original event and setup archive')
    proof, route, row, approved = _bound_inputs(event, initial, notice, archive)
    if ready_at != benny_reproduce._claim_ready_at(proof, row):
        raise ValueError('Benny correction needs the original rejection window')
    deadline = time.monotonic() + 30
    slack.check_team(route['rules']['workspace'], deadline)
    current = benny._snapshot(slack, route['snapshot']['location'], deadline)
    message = _approved_message(route, row, current, ready_at)
    if message is None:
        return None
    binding = {'initial': initial, 'notice': notice, 'archive': archive}
    variant = _named_setup(message, approved)
    details = {}
    if variant is None:
        guidance = approved.get('guidance')
        if not guidance:
            raise SlackError('Benny source changed without an approved in-window setup correction from the reporter')
        path = out.parent/'interpretation.json'
        interpretation = benny_correction_language.choose(path, message=message, guidance=guidance, binding=binding,
                                                           **({'model': model} if model is not None else {}))
        variant = benny_correction_language.confirmed(interpretation, message=message, guidance=guidance, binding=binding)
        if variant is None:
            raise SlackError('Benny retained the correction but found no clear, approved setup')
        deadline = time.monotonic() + 30
        if benny._snapshot(slack, route['snapshot']['location'], deadline) != current:
            raise SlackError('Benny source changed during correction interpretation')
        details['interpretation'] = interpretation
    corrected = {**route, 'snapshot': current,
                 'correction': {**binding, 'variant': variant, 'message': message, **details}}
    path = out/'route.json'
    if path.is_symlink():
        raise ValueError('Benny correction route must be a regular file')
    if path.exists():
        if benny.load_route(path, cli.digest(path))[1] != corrected:
            raise ValueError('Benny correction differs from its retained route')
    else:
        cli.write(path, corrected)
    return _reference(path)


def audit(path, proof, contract, route):
    """Check the original and corrected evidence together before any delivery."""
    path = Path(path)
    event = path.parent.parent.parent
    route_path = event/'correction/source/route.json'
    if (path != event/'correction/reproduction/result.json' or path.resolve() != path
            or proof['route']['path'] != str(route_path) or route_path.resolve() != route_path):
        raise ValueError('Benny corrected reproduction must use its original event paths')
    correction = route.get('correction')
    fields = {'initial', 'notice', 'archive', 'variant', 'message'}
    if not isinstance(correction, dict) or not fields <= correction.keys() or correction.keys() - fields - {'interpretation'}:
        raise ValueError('Benny corrected route needs its exact admitted correction')
    initial, notice, archive = (correction[key] for key in ('initial', 'notice', 'archive'))
    original, original_route, row, approved = _bound_inputs(event, initial, notice, archive)
    snapshot = route['snapshot']
    admitted = _approved_message(original_route, row, snapshot, benny_reproduce._claim_ready_at(original, row))
    if (admitted is None or admitted != correction['message']
            or route != {**original_route, 'snapshot': snapshot, 'correction': correction}):
        raise ValueError('Benny corrected route no longer matches the admitted source change')
    variant = _named_setup(admitted, approved)
    if 'interpretation' in correction:
        reference = correction['interpretation']
        if variant or _receipt_path(reference) != event/'correction/interpretation.json':
            raise ValueError('Benny correction mixes named and interpreted setup decisions')
        binding = {'initial': initial, 'notice': notice, 'archive': archive}
        variant = benny_correction_language.confirmed(reference, message=admitted, guidance=approved.get('guidance', {}),
                                                      binding=binding)
    if variant is None or variant != correction['variant'] or variant not in approved['setups']:
        raise ValueError('Benny correction lacks a confirmed decision for its approved setup')
    if any(proof[field] != original[field] for field in ('base', 'image', 'checkout', 'repository')):
        raise ValueError('Benny corrected reproduction changed the original baseline or runtime')
    if contract['memory_mb'] != approved['scope']['memory_mb']:
        raise ValueError('Benny corrected reproduction changed its approved memory limit')
    recipe = approved['setups'][correction['variant']]
    if _observation(recipe) != _observation(approved['original']):
        raise ValueError('a Benny correction setup cannot change the observable acceptance contract')
    _require_contract(contract, recipe, 'corrected')
    options(archive, correction['variant'], approved['scope'])
