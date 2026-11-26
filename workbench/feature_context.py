"""Bind an approved feature handoff to a task and its candidate review."""

import hashlib
import json
from pathlib import Path
import re
import subprocess

from workbench import cli
from workbench.prompt_profiles import PSTACK_COMMIT


PSTACK_SOURCE = 'skills/poteto-mode/playbooks/feature.md'


class FeatureContextError(ValueError):
    pass


CONTEXT_FILE = 'feature-context.json'
DESIGN_FILE = 'feature-design.json'
BINDING_FIELDS = {'path', 'sha256', 'design_path', 'design_sha256'}
CHECKPOINT_FIELDS = {'blocking', 'workstreams', 'shared_state', 'decomposition'}
JSON_LIMIT = 240_000
GIT_LIMIT = 512_000


def _json(path, expected, limit=JSON_LIMIT):
    """Use the bytes which were actually hashed, also for relative artifacts."""
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise FeatureContextError('feature artifact requires its SHA-256')
    if path.is_symlink() or not path.is_file():
        raise FeatureContextError('feature artifact must be a regular file')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit or hashlib.sha256(raw).hexdigest() != expected:
        raise FeatureContextError('feature artifact digest or size changed')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise FeatureContextError('feature artifact must be a JSON object')
    return value


def _reference(directory, value):
    if not isinstance(value, dict) or set(value) != {'path', 'sha256'} or not isinstance(value['path'], str):
        raise FeatureContextError('feature artifact reference needs path and SHA-256')
    path = directory / Path(value['path']).expanduser()
    return path, _json(path, value['sha256'])


def _git(repo, *args):
    result = subprocess.run(['git', '--no-replace-objects', '--literal-pathspecs', '-C', str(repo), *args],
                            capture_output=True, timeout=30)
    if result.returncode or len(result.stdout) > GIT_LIMIT:
        raise FeatureContextError('feature Git evidence is unavailable or exceeds its size limit')
    return result.stdout


def _checkpoint(value):
    if not isinstance(value, dict) or set(value) != CHECKPOINT_FIELDS:
        raise FeatureContextError('feature handoff needs all four throughput checkpoint items')
    for item in value.values():
        if (not isinstance(item, str) or not item.strip() or len(item.encode()) > 1200 or '\0' in item
                or (item.strip().lower().startswith('n/a') and not re.fullmatch(r'(?is)n/a:\s*\S.*', item.strip()))):
            raise FeatureContextError('each throughput item needs bounded text or n/a with a reason')


def _design(value, baseline, criteria):
    from workbench import architect, arena

    if (value.get('schema') != cli.SCHEMA or value.get('status') != 'selected'
            or value.get('pstack_commit') != PSTACK_COMMIT or value.get('commit') != baseline):
        raise FeatureContextError('feature design must be selected for the exact checkout baseline')
    shape = architect._shape(value['selected'], value['sources'])
    obligations = [{'id': f'C{index}', **item} for index, item in enumerate(shape['verification'], 1)]
    if value.get('obligations') != obligations:
        raise FeatureContextError('feature design obligations do not match the selected shape')
    acceptance = {item['text'] for item in criteria}
    if any(item['expectation'] not in acceptance for item in obligations if item['kind'] == 'runtime'):
        raise FeatureContextError('each architecture runtime expectation must be a frozen acceptance criterion')
    return arena._paths(value['implementation_scope'])


def _criteria(items, *, persisted=False):
    from workbench.verify_pr import _criteria as validate

    if persisted:
        items = [{'id': item['id'], 'text': item['text']} for item in items]
    return [{'id': item['id'], 'text': item['text'].strip()} for item in validate(items)]


def load_input(manifest, expected_sha256, repo):
    """Read the approved Feature manifest before creating a task or branch."""
    from workbench import architect, prototype

    try:
        manifest = Path(manifest).expanduser()
        value = _json(manifest, expected_sha256, limit=110_000)
        required = {'design', 'review_model', 'criteria', 'checkpoint'}
        if required - value.keys() or value.keys() - required - {'prototypes'}:
            raise FeatureContextError('feature handoff has missing or unknown fields')
        baseline = _git(repo, 'rev-parse', '--verify', 'HEAD^{commit}').decode('ascii').strip()
        _, design = _reference(manifest.parent, value['design'])
        criteria = _criteria(value['criteria'])
        scope = _design(design, baseline, criteria)
        architect._model(value['review_model'])
        _checkpoint(value['checkpoint'])
        references = value.get('prototypes', [])
        if not isinstance(references, list) or len(references) > 6:
            raise FeatureContextError('feature handoff accepts at most six native prototypes')
        prototypes = []
        for reference in references:
            path, receipt = _reference(manifest.parent, reference)
            recommendation = receipt['recommendation']
            prototype.validate_receipt(path, receipt, baseline=baseline, question=receipt['question'],
                                       conclusion=recommendation['conclusion'])
            prototypes.append({'question': receipt['question'], 'choice': recommendation['choice'],
                               'conclusion': recommendation['conclusion'], 'sha256': reference['sha256']})
        return {'schema': cli.SCHEMA, 'baseline': baseline, 'scope': scope, 'criteria': criteria,
                'checkpoint': value['checkpoint'], 'prototypes': prototypes, 'review_model': value['review_model'],
                'design': design, 'approved_design_sha256': value['design']['sha256'],
                'handoff_sha256': expected_sha256}
    except FeatureContextError:
        raise
    except architect.ERRORS as error:
        raise FeatureContextError(f'invalid feature handoff ({error})') from error


def freeze(task, data, context):
    """Write private snapshots and update the caller's task record in memory."""
    try:
        if (data.get('kind') != 'feature' or 'feature_context' in data
                or any(event.get('type') == 'feature-handoff' for event in data['events'])
                or data.get('base_commit') != context['baseline']
                or _criteria(data['criteria'], persisted=True) != context['criteria']):
            raise FeatureContextError('feature handoff does not match the task baseline or criteria')
        root = Path(task).expanduser().resolve().parent
        path, design = root / CONTEXT_FILE, root / DESIGN_FILE
        if any(item.exists() or item.is_symlink() for item in (path, design)):
            raise FeatureContextError('feature handoff snapshot already exists')
        cli.write(design, context['design'])
        cli.write(path, {key: value for key, value in context.items() if key != 'design'})
        metadata = {'path': str(path), 'sha256': cli.digest(path),
                    'design_path': str(design), 'design_sha256': cli.digest(design)}
        data['feature_context'] = metadata
        data['events'].append({'at': cli.now(), 'type': 'feature-handoff', **metadata})
        return metadata
    except FeatureContextError:
        raise
    except (OSError, ValueError, KeyError, TypeError, cli.WorkbenchError) as error:
        raise FeatureContextError(f'cannot freeze feature handoff ({error})') from error


def read(data):
    """Return frozen context only when both snapshots and task binding agree."""
    events = [event for event in data.get('events', [])
              if isinstance(event, dict) and event.get('type') == 'feature-handoff']
    if 'feature_context' not in data and not events:
        return None

    from workbench import architect

    try:
        meta = data.get('feature_context')
        if (data.get('kind') != 'feature' or not isinstance(meta, dict) or set(meta) != BINDING_FIELDS
                or len(events) != 1 or {key: events[0].get(key) for key in BINDING_FIELDS} != meta):
            raise FeatureContextError('feature handoff binding changed')
        path, design_path = Path(meta['path']), Path(meta['design_path'])
        if (not path.is_absolute() or path.parent != design_path.parent
                or path.name != CONTEXT_FILE or design_path.name != DESIGN_FILE):
            raise FeatureContextError('feature handoff paths are invalid')
        value = _json(path, meta['sha256'])
        design = _json(design_path, meta['design_sha256'])
        criteria = _criteria(data['criteria'], persisted=True)
        if (value.get('schema') != cli.SCHEMA or value.get('baseline') != data.get('base_commit')
                or value.get('criteria') != criteria or value.get('scope') != _design(design, data['base_commit'], criteria)):
            raise FeatureContextError('feature handoff baseline, scope or criteria changed')
        return {**value, 'design': design}
    except FeatureContextError:
        raise
    except architect.ERRORS as error:
        raise FeatureContextError(f'invalid retained feature handoff ({error})') from error


def prompt(context):
    """Expose the approved design and throughput checkpoint without the source packet."""
    selection = context['design']['selected']
    payload = {'baseline': context['baseline'], 'scope': context['scope'], 'criteria': context['criteria'],
               'design': {key: selection[key] for key in ('title', 'summary', 'modules', 'state', 'interfaces')},
               'obligations': context['design']['obligations'], 'checkpoint': context['checkpoint'],
               'prototypes': context['prototypes']}
    text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    if len(text.encode()) > 100_000:
        raise FeatureContextError('approved feature handoff is too large for the task context')
    return ('\nAPPROVED FEATURE HANDOFF (structured task data):\n' + text + '\n')


def runtime_criteria(context):
    """Identify the frozen criteria requiring runtime design evidence."""
    expectations = {item['expectation'] for item in context['design']['obligations'] if item['kind'] == 'runtime'}
    return {item['id'] for item in context['criteria'] if item['text'] in expectations}


def check_scope(data, head=None):
    context = read(data)
    if context is None:
        return []
    try:
        repo = data['repo']
        if head is None:
            commands = [
                ('diff', '--cached', '--name-only', '--no-renames', '-z', 'HEAD', '--'),
                ('diff', '--name-only', '--no-renames', '-z', '--'),
                ('ls-files', '--others', '-z', '--'),
            ]
        else:
            if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}', head):
                raise FeatureContextError('feature review requires an exact candidate commit')
            _git(repo, 'merge-base', '--is-ancestor', context['baseline'], head)
            commands = [('diff', '--name-only', '--no-renames', '-z', context['baseline'], head, '--')]
        changes = {name.decode('utf-8') for command in commands for name in _git(repo, *command).split(b'\0') if name}
        outside = changes - set(context['scope'])
        if outside:
            raise FeatureContextError('feature changes exceed the approved scope ' + ', '.join(sorted(outside)[:10]))
        return sorted(changes)
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        raise FeatureContextError('could not inspect the feature edit scope') from error


def verify_review(data, candidate, ref):
    """Check a static Architect receipt without treating runtime plans as proof."""
    context = read(data)
    if context is None:
        return None

    from workbench import architect

    try:
        changed = check_scope(data, candidate)
        _, review = _reference(Path(data['feature_context']['path']).parent, ref)
        if (review.get('schema') != cli.SCHEMA or review.get('status') != 'static-conforms'
                or review.get('baseline') != context['baseline'] or review.get('candidate') != candidate
                or review.get('design_sha256') != data['feature_context']['design_sha256']
                or review.get('changed') != changed or review.get('outside_scope') != [] or not changed):
            raise FeatureContextError('feature candidate lacks a matching static architecture review')
        patch = _git(data['repo'], 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--unified=2',
                     context['baseline'], candidate, '--')
        if review.get('patch_sha256') != hashlib.sha256(patch).hexdigest():
            raise FeatureContextError('feature architecture review diff changed')
        static = [item for item in context['design']['obligations'] if item['kind'] == 'static']
        runtime = [item['id'] for item in context['design']['obligations'] if item['kind'] == 'runtime']
        verification = review['verification']
        static_checks = [row for row in verification['checks'] if row['id'] not in runtime]
        pending = [row for row in verification['checks'] if row['id'] in runtime]
        if (review.get('runtime_pending') != runtime or [row['id'] for row in pending] != runtime
                or any(row.get('status') != 'pending' or row.get('diff_lines') != [] for row in pending)
                or any(row.get('status') != 'supported' for row in static_checks)):
            raise FeatureContextError('feature architecture review changed static or pending runtime obligations')
        architect._implementation({'summary': verification['summary'], 'checks': static_checks},
                                    static, patch.decode('utf-8').splitlines())
        return review
    except FeatureContextError:
        raise
    except architect.ERRORS as error:
        raise FeatureContextError(f'invalid feature architecture review ({error})') from error
