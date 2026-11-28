"""Resolve a Feature design through an approved Arena comparison."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time

from workbench import architect, arena, cli, feature_context, interrogate, investigate, model_profiles, prototype
from workbench.prompt_profiles import PSTACK_COMMIT


FORMAT = 'iboard-feature-arena-v1'
MANIFEST_LIMIT = 110_000
SANDBOX_LIMIT = 13_000_000
ARENA_FIELDS = {'image', 'source_mode', 'memory_mb', 'seconds', 'concurrency',
                'check', 'runners', 'rubric', 'judge_model', 'model_profile'}


def _require(condition, message):
    if not condition:
        raise feature_context.FeatureContextError(message)


def _bytes(path, limit, digest=None):
    _require(path.is_file() and not path.is_symlink(), 'Feature Arena evidence must be a regular file')
    with path.open('rb') as file:
        raw = file.read(limit + 1)
    _require(len(raw) <= limit, 'Feature Arena evidence exceeds its size bound')
    if digest is not None:
        _require(hashlib.sha256(raw).hexdigest() == digest, 'Feature Arena evidence changed after it was recorded')
    return raw


def _reference(path):
    return {'path': str(path), 'sha256': cli.digest(path)}


def _local_evidence(path, claimed_path, digest, limit=feature_context.JSON_LIMIT):
    _require(claimed_path == str(path) and path.resolve() == path,
             'Feature Arena proof does not identify the expected artifact')
    return feature_context._json(path, digest, limit=limit)


def _validate_source(repo, baseline, source):
    _require(source.get('schema') == cli.SCHEMA and source.get('commit') == baseline
             and source.get('pstack_commit') == PSTACK_COMMIT,
             'Architect comparison must match the exact Feature baseline')
    arena._bounded(source['question'], 6000, 'design question')
    decision = architect._blind_review(source['comparison'])['decision']
    if source['status'] == 'contested':
        _require(decision == 'contested' and 'selected' not in source and 'obligations' not in source,
                 'contested Architect comparison already claims a selection')
    else:
        _require(source['status'] == 'selected' and decision in ('A', 'B')
                 and source.get('selected') == source['alternatives'][decision]
                 and source.get('obligations') == _obligations(source['selected']),
                 'Feature Arena needs a valid selected or contested Architect comparison')
    sources = source['sources']
    _require(isinstance(sources, dict) and 1 <= len(sources) <= 12
             and set(sources) == {f'S{i}' for i in range(1, len(sources) + 1)},
             'Architect comparison needs pinned source ranges')
    ranges = [sources[f'S{i}'] for i in range(1, len(sources) + 1)]
    _require(all(isinstance(row, dict) and type(row.get('start')) is int
                 and type(row.get('end')) is int for row in ranges),
             'Architect comparison has an invalid source range')
    _, observed = investigate._collect(
        repo, baseline, [f"{row['path']}:{row['start']}-{row['end']}" for row in ranges],
        history=False, remaining=lambda: 10)
    _require(observed == sources, 'Architect source evidence differs from the pinned repository')
    _require(isinstance(source['alternatives'], dict) and set(source['alternatives']) == {'A', 'B'},
             'Feature Arena needs exactly two Architect alternatives')
    for option in source['alternatives'].values():
        architect._shape(option, sources)
    arena._paths(source['implementation_scope'])


def _obligations(option):
    return [{'id': f'C{index}', **item} for index, item in enumerate(option['verification'], 1)]


def _approval(path, sha256, repo, baseline, *, profile_snapshot=None):
    approval = feature_context._json(path, sha256, limit=MANIFEST_LIMIT)
    fields = {'design', 'review_model', 'criteria', 'checkpoint', 'arena'}
    _require(fields <= approval.keys() <= fields | {'prototypes', 'interrogate'},
             'Feature Arena approval has missing or unknown fields')
    source_path, source = feature_context._reference(path.parent, approval['design'])
    source_ref = {'path': str(source_path.resolve(strict=True)), 'sha256': approval['design']['sha256']}
    _validate_source(repo, baseline, source)
    criteria = feature_context._criteria(approval['criteria'])
    acceptance = {item['text'] for item in criteria}
    _require(all(check['expectation'] in acceptance for option in source['alternatives'].values()
                 for check in option['verification'] if check['kind'] == 'runtime'),
             'freeze the runtime expectations of both proposed architectures before comparison')
    architect._model(approval['review_model'])
    feature_context._checkpoint(approval['checkpoint'])
    settings = approval['arena']
    _require(isinstance(settings, dict) and settings.keys() <= ARENA_FIELDS,
             'Feature Arena approval cannot replace its derived repository, request, scope or alternatives')
    if 'model_profile' in settings:
        ref = settings['model_profile']
        if profile_snapshot is not None:
            _require(ref['sha256'] == profile_snapshot['sha256'], 'Feature Arena model profile changed after approval')
            ref = profile_snapshot
        settings = {**settings, 'model_profile': model_profiles.binding(ref, path.parent)}
    challenge = approval.get('interrogate')
    if 'interrogate' in approval:
        approved_profile = settings.get('model_profile')
        if ('model_profile' in challenge and approved_profile is not None
                and challenge['model_profile']['sha256'] == approved_profile['sha256']):
            challenge = {**challenge, 'model_profile': approved_profile}
        challenge = interrogate.settings(challenge, directory=path.parent)
    references = approval.get('prototypes', [])
    _require(isinstance(references, list) and len(references) <= 6,
             'Feature Arena accepts at most six native prototypes')
    prototypes, summaries = [], []
    for reference in references:
        path_to_proof, proof = feature_context._reference(path.parent, reference)
        recommendation = proof['recommendation']
        prototype.validate_receipt(path_to_proof, proof, baseline=baseline, question=proof['question'],
                                   conclusion=recommendation['conclusion'])
        prototypes.append({'path': str(path_to_proof.resolve(strict=True)), 'sha256': reference['sha256']})
        summaries.append({'question': proof['question'], 'choice': recommendation['choice'],
                          'conclusion': recommendation['conclusion'], 'sha256': reference['sha256']})
    handoff = {'review_model': approval['review_model'], 'criteria': criteria,
               'checkpoint': approval['checkpoint'], 'prototypes': prototypes}
    return handoff, summaries, source_ref, source, settings, challenge


def _arena_input(repo, baseline, source, handoff, settings, request, seconds=None):
    approved_seconds = arena._limit(settings.get('seconds', 600), 1, 3600, 'seconds')
    effective = approved_seconds if seconds is None else arena._limit(seconds, 1, approved_seconds, 'remaining seconds')
    request = (request + '\n\nFROZEN FEATURE ACCEPTANCE:\n'
               + json.dumps(handoff['criteria'], ensure_ascii=False) + '\nTHROUGHPUT CHECKPOINT:\n'
               + json.dumps(handoff['checkpoint'], ensure_ascii=False))
    return {**settings, 'seconds': effective, 'repo': str(repo), 'revision': baseline, 'request': request,
            'files': source['implementation_scope'], 'design_alternatives': source['alternatives']}


def _selected(source, source_sha256, choice):
    selected = source['alternatives'][choice]
    return {key: source[key] for key in ('schema', 'commit', 'pstack_commit', 'question',
                                         'sources', 'implementation_scope')} | {
        'status': 'selected', 'selected': selected, 'obligations': _obligations(selected),
        'arena_resolution': {'selected': choice, 'source_sha256': source_sha256},
    }


def _sandbox(directory, label, proof, config, head, *, must_pass=False):
    evidence = _local_evidence(directory / label / 'sandbox.json', proof['report'], proof['sha256'], SANDBOX_LIMIT)
    check = config['check']
    mode = config['source_mode']
    image = evidence.get('image', '')
    image_bound = (image == config['image'] if mode == 'workspace'
                   else isinstance(image, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', image))
    passed = evidence.get('exit_code') == check['exit_code'] and check['marker'] in evidence.get('stdout', '')
    _require(proof.get('executed') is True and proof.get('passed') is passed
             and evidence.get('schema') == cli.SCHEMA and evidence.get('commit') == head
             and evidence.get('workspace') == str(directory / label / 'source')
             and evidence.get('source_root') == ('/workspace' if mode == 'workspace' else '/app')
             and evidence.get('command') == check['command'] and image_bound
             and evidence.get('expected_exit') == check['exit_code']
             and evidence.get('expected_marker') == check['marker']
             and evidence.get('passed') is passed and evidence.get('container_created') is True
             and evidence.get('cleanup_confirmed') is True and evidence.get('workspace_read_only') is True
             and not evidence.get('error') and not evidence.get('timed_out') and (passed or not must_pass),
             'Feature Arena needs matching clean offline execution evidence')


def _git_patch(repo, baseline, head, scope):
    _require(isinstance(head, str) and re.fullmatch(r'[0-9a-f]{40}', head),
             'Feature Arena candidate must use an exact commit ID')
    lineage = feature_context._git(repo, 'rev-list', '--parents', '-n', '1', head).decode('ascii').split()
    _require(lineage == [head, baseline], 'Feature Arena candidate must have the pinned baseline as its only parent')
    filenames = feature_context._git(repo, 'diff', '--name-only', '-z', '--no-renames', baseline, head, '--')
    changes = set(filenames.decode('utf-8').split('\0')) - {''}
    _require(changes and changes <= set(scope), 'Feature Arena candidate Git diff is outside the approved scope')
    return feature_context._git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--no-color',
                                baseline, head, '--')


def _selection(directory, repo, config, result):
    baseline = config['revision']
    label, choice = result['base_candidate'], result['selected_design']
    candidates = result['candidates']
    _require(result['schema'] == cli.SCHEMA and result['status'] == 'verified'
             and result['baseline'] == baseline and label in ('A1', 'A2')
             and choice in ('A', 'B') and not result['dropouts']
             and len(candidates) == 2 and {item['design'] for item in candidates} == {'A', 'B'}
             and {item['id'] for item in candidates} == {'A1', 'A2'},
             'Feature Arena did not successfully compare both approved designs')
    if 'model_routes' in config:
        _require(all(item['provider'] == config['runners']['AB'.index(item['design'])] for item in candidates),
                 'Feature Arena candidate changed its approved model seat')
    reviews = arena.verify_model_calls(directory, config, result)
    winner = next(item for item in candidates if item['id'] == label)
    _require(winner['design'] == choice and winner['head'] == result['head']
             and winner['verification'] == result['verification'],
             'Feature Arena selection does not identify its exact winning candidate')
    patches = {}
    for item in candidates:
        candidate_label, head = item['id'], item['head']
        path = directory / candidate_label / 'candidate.patch'
        _require(item['path'] == str(path) and path.resolve() == path,
                 'Feature Arena candidate proof changed location')
        patch = _bytes(path, 15_000, item['patch_sha256'])
        _require(patch == _git_patch(repo, baseline, head, config['files']),
                 'Feature Arena candidate patch differs from Git')
        patches[candidate_label] = patch
        _sandbox(directory, candidate_label, item['verification'], config, head, must_pass=item is winner)
    path = directory / 'result.patch'
    _require(result['patch'] == str(path) and path.resolve() == path
             and patches['A1'] != patches['A2'] and _bytes(path, 15_000, result['patch_sha256']) == patches[label],
             'Feature Arena only exports the exact winner of distinct implementations')
    recommendations = set()
    for provider in ('glm', 'openai'):
        scorecard = result['scorecards'][provider]
        recommendation, totals = arena._scores(scorecard['answer'], candidates, config['rubric'])
        _require(scorecard['totals'] == totals, 'Feature Arena scorecard totals changed')
        recommendations.add(recommendation)
    legacy = {'provider': 'openai', 'model': config['judge_model']}
    score_route = reviews['cross-scorecard'] if reviews else legacy
    review_route = reviews['independent-review'] if reviews else legacy
    _require(label in recommendations and result['scorecards'][score_route['provider']]['model'] == score_route['model'],
             'Feature Arena selection disagrees with both independent scorecards')
    baseline_proof = result['baseline_verification']
    _sandbox(directory, 'baseline', baseline_proof, config, baseline)
    semantic = result['independent_review']
    review = _local_evidence(directory / 'selection' / 'independent-review.json',
                             semantic['report'], semantic['sha256'])
    entry = feature_context._git(repo, 'cat-file', 'blob', f"{baseline}:{config['entry']}")
    _require(semantic.get('verdict') == 'PASS' and review.get('verdict') == 'PASS'
             and review.get('schema') == cli.SCHEMA and review.get('head') == winner['head']
             and review.get('model') == review_route['model'] and review.get('provider') == review_route['provider']
             and review.get('sandbox_sha256') == winner['verification']['sha256']
             and review.get('baseline_sandbox_sha256') == baseline_proof['sha256']
             and review.get('acceptance_sha256') == hashlib.sha256(entry).hexdigest(),
             'Feature Arena needs an independent semantic review tied to the winning evidence')
    _sandbox(directory, 'independent', review['independent_sandbox'], config, winner['head'], must_pass=True)
    return winner['head'], choice


def _validate(receipt_path, receipt, repo, baseline, design, design_sha256):
    path = Path(receipt_path).expanduser().absolute()
    directory = path.parent.resolve(strict=True)
    _require(path == directory / 'result.json', 'Feature Arena resolution must use its native receipt path')
    _require(receipt.get('schema') == cli.SCHEMA and receipt.get('format') == FORMAT
             and receipt.get('status') == 'resolved' and receipt.get('baseline') == baseline
             and receipt.get('design_sha256') == design_sha256,
             'Feature Arena receipt does not match the prepared baseline and design')
    approval_path, digest = Path(receipt['approval']['path']), receipt['approval']['sha256']
    input_path = directory / 'arena-input.json'
    arena_input = _local_evidence(input_path, receipt['input']['path'], receipt['input']['sha256'])
    handoff, prototypes, source_ref, source, settings, challenge = _approval(
        approval_path, digest, repo, baseline, profile_snapshot=arena_input.get('model_profile'))
    _require(receipt['source_design'] == source_ref, 'Feature Arena source approval changed')
    request = _bytes(directory / 'request.txt', 8_000, receipt['request_sha256']).decode('utf-8')
    origin = Path(arena_input['repo']).resolve(strict=True)
    _require(arena_input == _arena_input(origin, baseline, source, handoff, settings, request,
                                        arena_input['seconds']),
             'Feature Arena execution contract differs from the frozen approval')
    config = arena._settings(input_path)
    observed = _local_evidence(directory / 'arena' / 'contract.json',
                               receipt['contract']['path'], receipt['contract']['sha256'])
    if ('cross_fallbacks' in config.get('model_routes', {})
            and 'cross_fallbacks' not in observed.get('model_routes', {})):
        # Older Arena contracts froze just the first cross-judge model.
        config['model_routes'].pop('cross_fallbacks')
    _require(observed == {**config, 'baseline': baseline},
             'Feature Arena retained contract does not match the frozen input')
    result = _local_evidence(directory / 'arena' / 'result.json',
                             receipt['arena']['path'], receipt['arena']['sha256'])
    candidate, choice = _selection(directory / 'arena', repo, config, result)
    _require(candidate == receipt['candidate'] and choice == receipt['selected_design']
             and design == _selected(source, source_ref['sha256'], choice),
             'Feature Arena handoff differs from its exact verified winning design or candidate')
    interrogation = receipt.get('interrogation')
    if challenge is not None:
        _require(isinstance(interrogation, dict), 'Feature Arena needs its approved Interrogate evidence')
        challenge_path = directory / 'interrogate' / 'result.json'
        report = _local_evidence(challenge_path, interrogation['path'], interrogation['sha256'])
        interrogate.validate_receipt(challenge_path, report, repo=repo, baseline=baseline, candidate=candidate,
                                     request_sha256=receipt['request_sha256'], config=challenge)
    else:
        _require(interrogation is None, 'Feature Arena received unapproved Interrogate evidence')
    return {'candidate': candidate, 'baseline': baseline, 'selected_design': choice,
            'request_sha256': receipt['request_sha256'], 'source_status': source['status'],
            'criteria': handoff['criteria'], 'checkpoint': handoff['checkpoint'],
            'review_model': handoff['review_model'], 'prototypes': prototypes, 'image': config['image'],
            'source_mode': config['source_mode'], 'interrogation': interrogation}


def validate_receipt(receipt_path, receipt, *, repo, baseline, design, design_sha256):
    """Rebind the exported design and exact winning commit to both native proofs."""
    try:
        return _validate(receipt_path, receipt, repo, baseline, design, design_sha256)
    except feature_context.FeatureContextError:
        raise
    except architect.ERRORS as error:
        raise feature_context.FeatureContextError(f'invalid Feature Arena receipt ({error})') from error


def resolve(manifest, expected_sha256, repo, request, out, *, seconds_limit=None, expected_model_profile=None, run=arena.run,
            challenge_runner=interrogate.run):
    """Run the authorized comparison and emit a Feature handoff only on verification."""
    started = time.monotonic()
    try:
        manifest = Path(manifest).expanduser().absolute()
        repo = Path(repo).expanduser().resolve(strict=True)
        baseline = cli.sha(repo, 'HEAD', timeout=10)
        handoff, _, source_ref, source, settings, challenge = _approval(manifest, expected_sha256, repo, baseline)
        if expected_model_profile is not None:
            _require(settings.get('model_profile', {}).get('sha256') == expected_model_profile,
                     'Feature Arena must use the same approved profile as its task')
        raw_request = _bytes(Path(request).expanduser(), 8_000)
        request_text = arena._bounded(raw_request.decode('utf-8'), 8_000, 'Feature request')
        settings_limit = arena._limit(settings.get('seconds', 600), 1, 3600, 'seconds')
        if seconds_limit is not None:
            _require(type(seconds_limit) is int and seconds_limit > 0, 'Feature Arena has no remaining time')
            settings_limit = min(seconds_limit, settings_limit)
        arena_input = _arena_input(repo, baseline, source, handoff, settings, request_text, settings_limit)
        directory = architect._output(repo, out)
    except feature_context.FeatureContextError:
        raise
    except architect.ERRORS as error:
        raise feature_context.FeatureContextError(f'invalid Feature Arena approval ({error})') from error
    request_path = directory / 'request.txt'
    with open(request_path, 'xb', opener=_private_file) as output:
        output.write(raw_request)
    input_path = directory / 'arena-input.json'
    cli.write(input_path, arena_input)
    receipt_path = directory / 'result.json'
    arena_dir = directory / 'arena'
    receipt = {'schema': cli.SCHEMA, 'format': FORMAT, 'status': 'blocked', 'baseline': baseline,
               'approval': {'path': str(manifest), 'sha256': expected_sha256}, 'source_design': source_ref,
               'request_sha256': hashlib.sha256(raw_request).hexdigest(), 'input': _reference(input_path)}
    try:
        native = run(input_path, arena_dir)
        _require(native.get('status') == 'verified', native.get('reason', 'Feature Arena comparison did not verify'))
        receipt['arena'] = _reference(arena_dir / 'result.json')
        receipt['contract'] = _reference(arena_dir / 'contract.json')
        verified = feature_context._json(arena_dir / 'result.json', receipt['arena']['sha256'])
        choice = verified['selected_design']
        design = _selected(source, source_ref['sha256'], choice)
        design_path = directory / 'design.json'
        design_bytes = (json.dumps(design, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
        receipt.update(status='resolved', candidate=verified['head'], selected_design=choice,
                       design_sha256=hashlib.sha256(design_bytes).hexdigest())
        if challenge is not None:
            remaining = None if seconds_limit is None else int(seconds_limit - (time.monotonic() - started))
            _require(remaining is None or remaining > 0, 'Feature Arena has no time left for Interrogate')
            outcome = challenge_runner(repo=repo, baseline=baseline, candidate=receipt['candidate'],
                                       request_sha256=receipt['request_sha256'], intent=request_text, config=challenge,
                                       out=directory / 'interrogate', seconds_limit=remaining)
            _require(outcome['status'] == 'clear', 'Interrogate withheld the Feature Arena candidate')
            receipt['interrogation'] = {'path': outcome['report'], 'sha256': outcome['report_sha256']}
        validate_receipt(receipt_path, receipt, repo=repo, baseline=baseline,
                         design=design, design_sha256=receipt['design_sha256'])
        cli.write(design_path, design)
        cli.write(receipt_path, receipt)
        path = directory / 'handoff.json'
        cli.write(path, {**handoff, 'design': _reference(design_path), 'arena': _reference(receipt_path)})
    except architect.ERRORS as error:
        receipt.update(status='blocked', reason=str(error)[:1800])
        for key in ('candidate', 'selected_design', 'design_sha256'):
            receipt.pop(key, None)
        cli.write(receipt_path, receipt)
        return {**receipt, 'report': str(receipt_path)}
    return {**receipt, 'report': str(receipt_path), 'handoff': str(path), 'handoff_sha256': cli.digest(path)}


def _private_file(path, flags):
    return os.open(path, flags, 0o600)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--issue-file', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        result = resolve(args.manifest, args.sha256, args.repo, args.issue_file, args.out)
    except architect.ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'resolved' else 1


if __name__ == '__main__':
    raise SystemExit(main())
