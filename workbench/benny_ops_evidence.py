"""Extract shareable operations facts from a settled Benny event."""

import hashlib
import json
from pathlib import Path
import re

from workbench import benny, benny_repair, benny_reproduce, cli, pr_watch, verify_existing_fix
from workbench.benny_slack import STAMP


_DIGEST = re.compile(r'[0-9a-f]{64}')
_TERMINAL = {
    'reproduced': 'The reported fault was reproduced.',
    'not-reproduced': 'The reported fault was not reproduced in the completed checks.',
    'environment-blocked': 'The verification environment blocked a conclusive result.',
    'inconclusive': 'The checks produced insufficient evidence for a conclusive result.',
    'source-changed': 'The source report changed during verification.',
    'source-unconfirmed': 'The original report could not be confirmed after verification.',
    'artifact-unconfirmed': 'The selected GitHub change could not be reconfirmed after verification.',
    'stale': 'The selected GitHub change moved during verification.',
    'insufficient-fix': 'The existing change did not resolve the reported fault.',
    'confirmed': 'The retained comparison confirmed the existing fix at the end of the run.',
    'verified': 'The candidate repair passed its local acceptance checks.',
    'draft': 'GitHub confirmed the verified repair as a draft pull request.',
}
_OPS = {
    'reproduced': 'Reproduced', 'not-reproduced': 'Could not reproduce', 'inconclusive': 'Could not reproduce',
    'confirmed': 'Existing fix verified', 'verified': 'Fix verified', 'draft': 'Draft pull request opened',
}


def _path(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('operations evidence needs a canonical file path')
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or not path.is_file():
        raise ValueError('operations evidence must be a regular canonical file')
    return path


def _read(path, *, digest=None, kind=None, schema=True):
    with path.open('rb') as stream:
        data = stream.read(1_500_001)
    if len(data) > 1_500_000 or digest is not None and hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('operations evidence changed or exceeds its read limit')
    record = json.loads(data)
    if (not isinstance(record, dict) or schema and record.get('schema') != cli.SCHEMA
            or kind is not None and record.get('kind') != kind):
        raise ValueError('unexpected operations evidence receipt')
    return record


def _receipt(reference, *, expected=None, kind=None, schema=True):
    if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
        raise ValueError('operations evidence needs an exact file reference')
    path, digest = _path(reference['path']), reference['sha256']
    if (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
            or expected is not None and path != expected):
        raise ValueError('operations evidence reference does not match this event')
    return path, _read(path, digest=digest, kind=kind, schema=schema)


def _scoped_route(reference, expected, scope):
    path, _ = _receipt(reference, expected=expected, kind='benny-source-route')
    _, route = benny.load_route(path, reference['sha256'])
    location = route['snapshot']['location']
    if ((route['rules']['workspace'], location['channel'], location['root'], route['rules']['repository'])
            != (scope['workspace'], scope['source_channel'], scope['source_root'], scope['repository'])):
        raise ValueError('operations evidence belongs to a different source or repository')
    return route


def _revision(value):
    if not isinstance(value, str) or not pr_watch.SHA.fullmatch(value):
        raise ValueError('operations evidence needs an exact revision')
    return value


def _reproduction(outcome, scope, directory, *, initial=False):
    event = directory/'correction' if 'corrected_route' in outcome and not initial else directory
    key = 'original_reproduction' if initial else 'reproduction'
    reference = outcome[key]
    path, report = _receipt(reference, expected=event/'reproduction/result.json', kind='benny-reproduction')
    route_ref = report['route']
    route = _scoped_route(route_ref, event/'source/route.json', scope)
    if (report.get('repository') != scope['repository'] or route['decision']['status'] != 'eligible-for-reproduction'
            or ('correction' in route) != (event != directory)
            or event != directory and (route_ref != outcome['corrected_route']
                or route['correction']['initial'] != outcome['original_reproduction'])):
        raise ValueError('operations reproduction receipt does not match this event')
    base, status = _revision(report['base']), report['status']
    if initial and status != 'reproduced':
        raise ValueError('the original reproduction was not confirmed')
    if status == 'reproduced':
        benny_reproduce._approved(path, reference['sha256'])
        text = f'Both the primary path and the independent probe observed the fault twice on baseline {base} in separate offline containers.'
    elif status == 'not-reproduced':
        baseline = path.parent/'baseline/result.json'
        contract = path.parent/'contract.json'
        _, run = _receipt({'path': str(baseline), 'sha256': report['baseline_sha256']})
        _, plan = _receipt({'path': str(contract), 'sha256': report['contract_sha256']})
        if (report.get('runtime_status') != status or plan.get('base') != base
                or not benny_reproduce.IMAGE.fullmatch(report['image'])):
            raise ValueError('the healthy baseline differs from the approved verification')
        # A healthy baseline has no independent review of the fault's expected visible state.
        probe_plan = {key: value for key, value in plan.items() if key != 'visual'}
        benny_reproduce._observations(baseline.parent, run, probe_plan, base, report['image'], 'pass')
        text = f'Both the primary path and the independent probe passed twice on baseline {base} in separate offline containers.'
    else:
        if status not in ('environment-blocked', 'inconclusive', 'source-unconfirmed'):
            raise ValueError('unexpected native reproduction outcome')
        return report, None
    if 'original_reproduction' in outcome:
        label = 'Original setup' if initial else 'Corrected setup'
        text = f'{label}. {text}'
    return report, {'kind': 'reproduction', 'text': text}


def _existing(outcome, scope, directory):
    folder = directory/'existing'
    _, receipt = _receipt(outcome['existing'], expected=folder/'result.json', kind='benny-reproduction-result')
    route_ref = receipt['source_route']
    route = _scoped_route({'path': route_ref['receipt'], 'sha256': route_ref['sha256']},
                          directory/'source/route.json', scope)
    artifact = receipt['artifact']
    match = verify_existing_fix.ARTIFACT.fullmatch(artifact)
    expected_outcome = {'changed': 'source-changed', 'unconfirmed': 'source-unconfirmed'}.get(receipt['source_recheck'])
    if (route['decision']['status'] != 'verify-existing' or route['decision']['artifact'] != artifact
            or receipt['outcome'] != outcome['status'] or receipt['verification'] != 'verification/result.json'
            or receipt['source_recheck'] not in ('changed', 'unconfirmed', 'unchanged')
            or expected_outcome is not None and receipt['outcome'] != expected_outcome
            or expected_outcome is None and receipt['outcome'] not in (
                'environment-blocked', 'artifact-unconfirmed', 'stale', 'insufficient-fix', 'inconclusive', 'confirmed')
            or match is None or match[1].casefold() != scope['repository'].casefold()
            or match[2] == 'pull' and not re.fullmatch(r'[1-9][0-9]{0,9}', match[3])
            or match[2] == 'commit' and not pr_watch.SHA.fullmatch(match[3])):
        raise ValueError('existing fix evidence does not match this event')
    if ('existing_verification' not in outcome or receipt['source_recheck'] != 'unchanged'
            or receipt['outcome'] not in ('confirmed', 'insufficient-fix')):
        return None
    _, run = _receipt(outcome['existing_verification'], expected=folder/'verification/result.json')
    pair = run['artifact']
    base, head = (_revision(run[key]) for key in ('base_commit', 'candidate_commit'))
    if (base == head or pair['repository'].casefold() != scope['repository'].casefold()
            or (pair['url'], pair['base'], pair['head']) != (artifact, base, head) or pair != run['artifact_recheck']
            or run['outcome'] != run['runtime_outcome'] or run['outcome'] != receipt['outcome']
            or receipt['runtime_outcome'] != run['outcome']):
        raise ValueError('existing fix verification does not match its selected artifact')
    after = 'pass' if run['outcome'] == 'confirmed' else 'observed-failure'
    for key, expected, revision in (('baseline', 'observed-failure', base), ('candidate', after, head)):
        round_ = run['rounds'][key]
        if (round_['commit'] != revision or round_['observations']
                != {'doctor_healthy': True, 'primary': [expected] * 2, 'oracle': [expected] * 2}):
            raise ValueError('existing fix comparison does not show both reported probe outcomes')
    result = 'passed twice each' if after == 'pass' else 'each still observed the fault twice'
    return {'kind': 'existing-fix', 'text': (
        f'Both probes observed the fault twice on baseline {base} and {result} on candidate {head}.')}


def _task(value, directory):
    path = _path(value)
    if (path.name != 'task.json' or path.parent.parent != directory/'tasks'
            or not re.fullmatch(r'benny-[0-9a-f]{32}', path.parent.name)):
        raise ValueError('operations repair receipt belongs to a different task')
    return path


def _task_evidence(reference, task):
    path, record = _receipt(reference, schema=False)
    if not path.is_relative_to(task.parent):
        raise ValueError('operations repair evidence belongs to a different task')
    return record


def _candidate(outcome, directory, original, candidate, independent):
    repair = outcome['repair']
    task = _task(outcome['task'], directory)
    base, head = (_revision(repair[key]) for key in ('base', 'head'))
    if base == head or base != original['base'] or original['status'] != 'reproduced':
        raise ValueError('operations candidate does not match its confirmed baseline')
    _task_evidence(candidate, task)
    review = _task_evidence(independent, task)
    if (review.get('verdict') != 'PASS' or review.get('benny_candidate') != candidate
            or review.get('candidate_commit') != head or review.get('baseline_commit') != base):
        raise ValueError('operations candidate review does not match the independently verified changes')
    benny_repair.candidate_proof(outcome['reproduction'], candidate, head)
    return {'kind': 'candidate', 'text': (
        f'Both probes passed twice in separate offline containers on candidate {head}. '
        'The independent review recorded a pass for that same candidate.')}


def _ref(record):
    return {key: record[key] for key in ('path', 'sha256')}


def _draft(outcome, scope, journal, directory, original):
    repair = outcome['repair']
    repository, number = scope['repository'], repair['pr']
    task = _task(outcome['task'], directory)
    reference = outcome['draft_journal']
    path, draft = _receipt(reference, expected=task.with_name('benny-draft.json'))
    plan = draft['plan']
    if type(number) is not int or not 1 <= number < 10**10:
        raise ValueError('operations draft needs the verified pull request number')
    url = f'https://github.com/{repository}/pull/{number}'
    if ((repair['status'], repair['repo'], repair['receipt'], repair['url']) != ('draft', repository, str(path), url)
            or journal.get('draft', {}).get('url') != url or draft['phase'] != 'draft' or draft['pr'] != number
            or plan['route_sha256'] != original['route']['sha256']
            or (plan['repository'], plan['head'], plan['base'], plan['branch'], plan['before_sha256'])
            != (repository, repair['head'], repair['base'], repair['branch'], outcome['reproduction']['sha256'])):
        raise ValueError('operations draft is not confirmed by the native draft journal')
    data = _read(task)
    candidate = data['benny_candidate']
    reviews = {(item['path'], item['sha256']) for item in data['evidence'] if isinstance(item, dict)
               and item.get('kind') == 'independent-review' and not item.get('superseded')
               and item.get('sha256') == plan['review_sha256'] and item.get('commit') == repair['head']}
    if len(reviews) != 1 or candidate['sha256'] != plan['after_sha256']:
        raise ValueError('operations draft is missing matching candidate evidence')
    review_path, digest = next(iter(reviews))
    return (_candidate(outcome, directory, original, candidate, {'path': review_path, 'sha256': digest}),
            {'kind': 'draft', 'text': f'The verified draft pull request is {url}.'})


def packet(outcome, *, repository):
    """Keep all Slack-visible facts confined to the settled run’s evidence."""
    try:
        path, journal = _receipt(outcome['operations'], kind='benny-operations')
        directory, status, scope = path.parent, outcome['status'], journal['scope']
        if (path.name != 'operations.json' or scope['repository'] != repository
                or not pr_watch.REPO.fullmatch(repository) or not STAMP.fullmatch(journal['root'])
                or 'intent' in journal or status not in _TERMINAL or journal['status'] != _OPS.get(status, 'Blocked')):
            raise ValueError('operations follow-up needs the settled receipt for this repository')
        facts = {'S1': {'kind': 'status', 'text': _TERMINAL[status]}}
        if 'existing' in outcome:
            fact = _existing(outcome, scope, directory)
            if fact:
                facts['X1'] = fact
            return facts
        if 'original_reproduction' in outcome:
            _, facts['B1'] = _reproduction(outcome, scope, directory, initial=True)
        report, fact = _reproduction(outcome, scope, directory)
        if status not in ('verified', 'draft') and report['status'] != status:
            raise ValueError('operations outcome differs from the retained reproduction')
        if fact:
            facts['R1'] = fact
        if status == 'draft':
            if 'draft_journal' in outcome:
                facts['V1'], facts['D1'] = _draft(outcome, scope, journal, directory, report)
            else:
                del facts['S1']
        if status == 'verified':
            repair = outcome['repair']
            if repair['status'] != status or repair['task'] != outcome['task']:
                raise ValueError('operations outcome does not identify the verified repair task')
            facts['V1'] = _candidate(outcome, directory, report, repair['candidate'], _ref(repair['independent']))
        return facts
    except (AttributeError, KeyError, OSError, TypeError, cli.WorkbenchError) as error:
        raise ValueError('operations evidence is unavailable or malformed') from error
