"""Probe model availability, resolve role budgets, and freeze routes for iBoard tasks."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time

from model import claude_glm, codex_catalog, codex_review
from workbench import cli, pstack_model_rules
from workbench.prompt_profiles import PSTACK_COMMIT
from workbench.task_lock import task_lock


SOURCE = 'skills/setup-pstack/SKILL.md'
FILE = 'model-profile.json'
MAX_PROFILE_BYTES = 120_000
EFFORTS = ('low', 'medium', 'high', 'xhigh')
BUDGETS = {'large': 0, 'medium': 1, 'small': 2}
ROLES = {
    'planning': 'xhigh',
    'feature-refactoring': 'xhigh',
    'bug-fix': 'xhigh',
    'repair': 'high',
    'baseline-review': 'high',
    'final-review': 'xhigh',
    'perf-issue': 'xhigh',
    'hillclimb': 'xhigh',
    'judgment-prose': 'xhigh',
    'arena-runners': 'high',
    'arena-cross-judge-pool': 'high',
    'swarm-workers': 'high',
    'interrogate-reviewers': 'high',
}
PANELS = {'arena-runners', 'arena-cross-judge-pool', 'interrogate-reviewers'}
TASK_ROLES = {'planning', 'feature-refactoring', 'bug-fix', 'repair', 'baseline-review', 'final-review'}
ALIASES = ('inherit-parent', 'auto')
MODELS = {'glm': r'glm-[a-z0-9][a-z0-9.-]{0,64}', 'openai': r'gpt-[a-z0-9][a-z0-9.-]{0,64}'}
PSTACK_ROLES = {
    'feature, refactoring': 'feature-refactoring',
    'bug-fix': 'bug-fix',
    'perf-issue': 'perf-issue',
    'hillclimb': 'hillclimb',
    'judgment and prose': 'judgment-prose',
    'arena runners': 'arena-runners',
    'arena cross-judge pool': 'arena-cross-judge-pool',
    'swarm workers': 'swarm-workers',
    'interrogate reviewers': 'interrogate-reviewers',
}
PROBE = 'Return the JSON object {"available":true} and no other content. Do not use tools.'
ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, cli.WorkbenchError, codex_review.ReviewError)


class ProfileError(ValueError):
    pass


def _require(ok, message):
    if not ok:
        raise ProfileError(message)


def _model(value):
    _require(isinstance(value, dict) and set(value) == {'provider', 'model'}
             and value['provider'] in MODELS and re.fullmatch(MODELS[value['provider']], value['model']),
             'select an exact provider and model ID')
    return value


def _reading(path, sha256=None):
    path = Path(path).expanduser()
    _require(not path.is_symlink() and path.is_file(), 'use a regular profile input file')
    with path.open('rb') as source:
        raw = source.read(MAX_PROFILE_BYTES + 1)
    _require(len(raw) <= MAX_PROFILE_BYTES and (sha256 is None or hashlib.sha256(raw).hexdigest() == sha256),
             'profile input size or digest changed')
    value = json.loads(raw)
    _require(isinstance(value, dict) and value.get('schema') == cli.SCHEMA, 'invalid model profile schema')
    return raw, value


def _exact(path, sha256):
    _require(isinstance(sha256, str) and re.fullmatch(r'[0-9a-f]{64}', sha256), 'supply the approved SHA-256')
    return _reading(path, sha256)


def _save(path, value, *, reuse=False):
    target = Path(path).expanduser().absolute()
    _require(not target.is_symlink(), 'profile output cannot be a symbolic link')
    if target.exists():
        _require(reuse and _reading(target)[1] == value, 'use a new output or an identical installed profile')
    else:
        cli.write(target, value)
    return {'path': str(target.resolve()), 'sha256': cli.digest(target)}


def discover(out, *, seconds=20, list_models=codex_catalog.list_models):
    """List candidate routes advertised by the local Codex catalog."""
    target = Path(out).expanduser()
    _require(not target.exists() and not target.is_symlink(), 'use a fresh model catalog output')
    _require(type(seconds) is int and 1 <= seconds <= 60, 'model catalog timeout must be between 1 and 60 seconds')
    models = list_models(timeout=seconds)
    _require(models, 'Codex did not advertise a supported model; provide explicit probe routes')
    record = {'schema': cli.SCHEMA, 'source': SOURCE, 'kind': 'catalog', 'provider': 'openai',
              'method': 'codex-app-server:model/list', 'observed_at': cli.now(), 'models': models}
    return {**_save(target, record), 'models': models}


def _probe(choice, effort, seconds, editor, reviewer):
    model = choice['model']
    if choice['provider'] == 'glm':
        with TemporaryDirectory(prefix='iboard-model-probe-') as folder:
            work = Path(folder).resolve()
            raw = editor(work, PROBE, seconds, read_only=True, file_tools_only=True, capture_trace=True,
                         private_workspace=True, requested_model=model, requested_effort=effort)
        observed = raw.get('model')
        _require(raw.get('exit_code') == 0 and not raw.get('timed_out') and raw.get('trace_completed') is True
                 and isinstance(observed, str) and claude_glm.reported_model_id(observed) == model
                 and raw.get('requested_effort') == effort, 'GLM did not confirm the requested model and complete')
        answer = json.loads(raw['stdout'])
    else:
        answer = reviewer(PROBE, model, seconds, effort=effort)
        observed = None  # Codex exec does not report the serving model in its stdout.
    _require(answer == {'available': True}, 'model probe did not produce the required observation')
    return observed


def probe(manifest, out, *, editor=claude_glm.execute, reviewer=codex_review.review):
    _, spec = _reading(manifest)
    _require(set(spec) == {'schema', 'engines', 'seconds'} and type(spec['seconds']) is int
             and 10 <= spec['seconds'] <= 600 and isinstance(spec['engines'], list)
             and 1 <= len(spec['engines']) <= 12, 'declare models and one probe budget')
    choices = []
    for row in spec['engines']:
        _require(set(row) == {'provider', 'model', 'efforts'}, 'declare provider, model and allowed efforts')
        item = _model({name: row[name] for name in ('provider', 'model')})
        efforts = row['efforts']
        _require(isinstance(efforts, list) and efforts and len(efforts) == len(set(efforts))
                 and all(effort in EFFORTS for effort in efforts), 'invalid probe effort list')
        choices.extend((item, effort) for effort in efforts)
    _require(len(choices) <= 20 and len({(x['provider'], x['model'], e) for x, e in choices}) == len(choices),
             'use no more than twenty distinct probes')
    target = Path(out).expanduser()
    _require(not target.exists() and not target.is_symlink(), 'use a fresh model probe output')
    deadline = time.monotonic() + spec['seconds']
    closed, unavailable, results = set(), set(), []
    for item, effort in choices:
        row = {**item, 'effort': effort, 'status': 'unverified', 'observed_model': None}
        vendor, model = item['provider'], item['model']
        remaining = min(60, int(deadline - time.monotonic()))
        if vendor in closed or remaining <= 0:
            row['reason'] = 'provider stopped or probe budget exhausted'
        elif (vendor, model) in unavailable:
            row['reason'] = 'model unavailable in this probe'
        else:
            try:
                row.update(observed_model=_probe(item, effort, remaining, editor, reviewer),
                           status='confirmed', accepted_at=cli.now())
            except ERRORS as error:
                if vendor == 'openai' and isinstance(error, codex_review.ModelUnavailable):
                    unavailable.add((vendor, model))
                    row.update(status='unavailable', reason='model_not_found')
                else:
                    closed.add(vendor)
                    row['reason'] = type(error).__name__
        results.append(row)
    return {**_save(target, {'schema': cli.SCHEMA, 'source': SOURCE, 'kind': 'inventory',
                             'probes': results, 'observed_at': cli.now()}), 'probes': results}


def _available(record):
    _require(record.get('source') == SOURCE and record.get('kind') == 'inventory'
             and isinstance(record.get('probes'), list) and 1 <= len(record['probes']) <= 20,
             'use a complete role inventory')
    result = set()
    unavailable = set()
    for row in record['probes']:
        item = _model({name: row[name] for name in ('provider', 'model')})
        effort = row['effort']
        _require(effort in EFFORTS and row['status'] in ('confirmed', 'unverified', 'unavailable'), 'invalid probe receipt')
        if row['status'] == 'unavailable':
            _require(item['provider'] == 'openai' and row.get('reason') == 'model_not_found'
                     and row['observed_model'] is None, 'invalid model unavailability receipt')
            unavailable.add((item['provider'], item['model']))
        if row['status'] != 'confirmed':
            continue
        observed = row['observed_model']
        _require((isinstance(observed, str) and claude_glm.reported_model_id(observed) == item['model']
                  if item['provider'] == 'glm' else observed is None)
                 and datetime.fromisoformat(row['accepted_at']).tzinfo is not None, 'unconfirmed model identity')
        key = (item['provider'], item['model'], effort)
        _require(key not in result, 'duplicate model capability')
        result.add(key)
    return {route for route in result if route[:2] not in unavailable}


def _routes(spec, available):
    _require(set(spec) == {'schema', 'budget', 'parent', 'roles'} and spec['budget'] in BUDGETS
             and isinstance(spec['roles'], dict) and set(spec['roles']) <= set(ROLES),
             'name the budget, parent model, and supported model roles')
    shift = BUDGETS[spec['budget']]
    parent = _model(spec['parent'])
    result = {}
    for role, effort in ROLES.items():
        prefs = spec['roles'].get(role, ['inherit-parent'] if role in PANELS else 'inherit-parent')
        if role not in PANELS:
            prefs = [prefs]
        _require(isinstance(prefs, list) and 1 <= len(prefs) <= 5, 'a model panel needs one to five seats')
        if role == 'arena-runners' and role in spec['roles']:
            _require(len(prefs) >= 2, 'Arena configuration needs at least two seats')
        resolved = []
        cap = max(0, EFFORTS.index(effort) - shift)
        for pref in prefs:
            choice = parent if pref in ALIASES else _model(pref)
            vendor, slug = choice['provider'], choice['model']
            _require(role not in TASK_ROLES or vendor == 'glm', 'task file and evidence roles require Claude Code + GLM')
            if role == 'arena-cross-judge-pool' and pref in ALIASES:
                continue
            effective = next((step for step in reversed(EFFORTS[:cap + 1]) if (vendor, slug, step) in available), None)
            if effective is None:
                raise ProfileError(f'{role} needs a confirmed {vendor}/{slug} at or below {EFFORTS[cap]}')
            resolved.append({**choice, 'effort': effective, 'target': EFFORTS[cap],
                             'selection': pref if pref in ALIASES else 'explicit'})
        result[role] = resolved if role in PANELS else resolved[0]
    return result


def _profile(value):
    _require(set(value) - {'migration'} == {'schema', 'source', 'kind', 'inventory_sha256', 'inventory', 'choices', 'routes'}
             and value['source'] == SOURCE and value['kind'] == 'profile'
             and re.fullmatch(r'[0-9a-f]{64}', value['inventory_sha256'])
             and hashlib.sha256(_serialized(value['inventory'])).hexdigest() == value['inventory_sha256']
             and _routes(value['choices'], _available(value['inventory'])) == value['routes'],
             'saved model profile no longer matches the confirmed capabilities')
    if 'migration' in value:
        migration = value['migration']
        _require(isinstance(migration, dict) and set(migration) == {'plan', 'plan_sha256', 'mapping', 'mapping_sha256'}
                 and _canonical_digest(migration['plan']) == migration['plan_sha256']
                 and _canonical_digest(migration['mapping']) == migration['mapping_sha256']
                 and _migrated_choices(migration['plan'], migration['mapping']) == value['choices'],
                 'saved model profile no longer matches its approved rule migration')
    return value


def _serialized(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode()


def _canonical_digest(value):
    return hashlib.sha256(_serialized(value)).hexdigest()


def inspect_rule(rule, out):
    path = Path(rule).expanduser()
    _require(path.is_file() and not path.is_symlink(), 'use a regular pstack rule file')
    with path.open('rb') as stream:
        raw = stream.read(pstack_model_rules.MAX_BYTES + 1)
    plan = {'schema': cli.SCHEMA, 'source': SOURCE, 'kind': 'rule-migration', 'pstack_commit': PSTACK_COMMIT,
            'rule_sha256': hashlib.sha256(raw).hexdigest(), **pstack_model_rules.parse(raw)}
    return {**_save(out, plan), 'budget': plan['budget'], 'roles': plan['roles'], 'changes': plan['changes']}


def _migrated_choices(plan, mapping):
    _require(isinstance(plan, dict) and set(plan) == {
                 'schema', 'source', 'kind', 'pstack_commit', 'rule_sha256', 'defaults_version',
                 'budget', 'roles', 'changes'}
             and plan['schema'] == cli.SCHEMA and plan['source'] == SOURCE and plan['kind'] == 'rule-migration'
             and plan['pstack_commit'] == PSTACK_COMMIT and re.fullmatch(r'[0-9a-f]{64}', plan['rule_sha256'])
             and type(plan['defaults_version']) is int and plan['defaults_version'] in (1, 2, 3)
             and plan['budget'] in BUDGETS and isinstance(plan['roles'], dict) and set(plan['roles']) == set(PSTACK_ROLES)
             and isinstance(plan['changes'], list),
             'use a plan from the pinned pstack rule inspector')
    _require(isinstance(mapping, dict) and set(mapping) == {'schema', 'parent', 'models'}
             and mapping['schema'] == cli.SCHEMA and isinstance(mapping['models'], dict),
             'provide an exact parent and explicit model mappings')
    parent = _model(mapping['parent'])
    substitutions = {slug: _model(item) for slug, item in mapping['models'].items()}
    missing = set()

    def translate(slug):
        _require(isinstance(slug, str) and pstack_model_rules.SLUG.fullmatch(slug),
                 'rule migration contains an invalid model ID')
        if slug in ALIASES:
            return slug
        if slug in substitutions:
            return substitutions[slug]
        for vendor, pattern in MODELS.items():
            if re.fullmatch(pattern, slug):
                return {'provider': vendor, 'model': slug}
        missing.add(slug)
        return None

    roles = {}
    for original, role in PSTACK_ROLES.items():
        value = plan['roles'][original]
        if role in PANELS:
            _require(isinstance(value, list) and 1 <= len(value) <= 5, 'rule migration has an invalid panel')
            roles[role] = [translate(entry) for entry in value]
        else:
            roles[role] = translate(value)
    _require(not missing, 'provide explicit mappings for: ' + ', '.join(sorted(missing)))
    return {'schema': cli.SCHEMA, 'budget': plan['budget'], 'parent': parent, 'roles': roles}


def _draft_profile(current, spec, out, *, migration=None):
    selection = _routes(spec, _available(current))
    profile = {'schema': cli.SCHEMA, 'source': SOURCE, 'kind': 'profile', 'inventory_sha256': _canonical_digest(current),
               'inventory': current, 'choices': spec, 'routes': selection}
    if migration is not None:
        profile['migration'] = migration
        _require(len(_serialized(profile)) <= MAX_PROFILE_BYTES, 'migrated profile exceeds the profile size limit')
    return {**_save(out, _profile(profile)), 'routes': selection}


def draft(inventory, inventory_sha256, choices, out, *, previous=None, previous_sha256=None):
    _, current = _exact(inventory, inventory_sha256)
    _, spec = _reading(choices)
    if previous is not None or previous_sha256 is not None:
        before = load(previous, previous_sha256)['choices']
        spec = {**spec, 'roles': {**before['roles'], **spec['roles']}}
    return _draft_profile(current, spec, out)


def draft_rule(plan, plan_sha256, mapping, mapping_sha256, inventory, inventory_sha256, out):
    _, preview = _exact(plan, plan_sha256)
    _, routes = _exact(mapping, mapping_sha256)
    _, current = _exact(inventory, inventory_sha256)
    spec = _migrated_choices(preview, routes)
    migration = {'plan': preview, 'plan_sha256': _canonical_digest(preview),
                 'mapping': routes, 'mapping_sha256': _canonical_digest(routes)}
    return _draft_profile(current, spec, out, migration=migration)


def load(profile, sha256):
    _, data = _exact(profile, sha256)
    return _profile(data)


def install(profile, sha256, out, *, previous_sha256=None):
    value = load(profile, sha256)
    target = Path(out).expanduser().absolute()
    if previous_sha256 is None:
        return _save(target, value, reuse=True)
    _require(target.is_file() and not target.is_symlink(), 'update a previously installed model profile')
    with task_lock(target, lock_name='.iboard-model-profile.lock'):
        current = load(target, previous_sha256)
        if current != value:
            cli.write(target, value)
        return {'path': str(target.resolve()), 'sha256': cli.digest(target)}


def snapshot(task, data, profile):
    result = _save(Path(task).parent / FILE, profile)
    reference = {'file': FILE, 'sha256': result['sha256']}
    data['model_profile'] = reference
    return reference


def intake(task, data, stage):
    reference = data.get('model_profile')
    _require(reference == stage.get('model_profile'), 'model profile changed after task intake')
    if reference is None:
        return None
    _require(set(reference) == {'file', 'sha256'} and reference['file'] == FILE,
             'invalid task model profile reference')
    return load(Path(task).with_name(FILE), reference['sha256'])


def for_stage(profile, phase, kind):
    if phase == 'plan':
        role = 'planning'
    elif phase == 'implementation':
        role = 'bug-fix' if kind == 'bug' else 'feature-refactoring'
    elif phase.startswith('premise-'):
        role = 'baseline-review'
    elif phase in ('baseline-review', 'final-review'):
        role = phase
    elif phase.startswith(('repair-', 'ci-repair-')):
        role = 'repair'
    else:
        raise ProfileError(f'unknown model stage: {phase}')
    return {'role': role, **profile['routes'][role]}


def select(profile, role, *, other_than=None):
    routes = profile['routes'][role]
    if other_than is None:
        return routes
    _require(role == 'arena-cross-judge-pool' and other_than in MODELS, 'select a known cross-vendor judge')
    return next((item for item in routes if item['provider'] != other_than), None)


def binding(reference, directory):
    """Freeze a profile by value so an approved run need not reread an installed file."""
    if set(reference) == {'path', 'sha256'}:
        value = load(Path(directory) / Path(reference['path']).expanduser(), reference['sha256'])
        reference = {'profile': value, 'sha256': reference['sha256']}
    _require(set(reference) == {'profile', 'sha256'} and
             hashlib.sha256(_serialized(reference['profile'])).hexdigest() == reference['sha256'],
             'model profile snapshot does not match the approved bytes')
    _profile(reference['profile'])
    return reference


def invocation(route):
    return {name: route[name] for name in ('provider', 'model', 'effort')}


def resolve(profile, role, preference=None):
    if preference in (None, 'profile'):
        return invocation(profile['routes'][role])
    _require(isinstance(preference, dict) and set(preference) in (
        {'provider', 'model'}, {'provider', 'model', 'effort'}), 'invalid explicit role choice')
    engine = _model({key: preference[key] for key in ('provider', 'model')})
    cap = max(0, EFFORTS.index(ROLES[role]) - BUDGETS[profile['choices']['budget']])
    pool = _available(profile['inventory'])
    requested = preference.get('effort')
    for effort in reversed(EFFORTS[:cap + 1]):
        if (requested is None or requested == effort) and (engine['provider'], engine['model'], effort) in pool:
            return {**engine, 'effort': effort}
    raise ProfileError(f'{role} has no confirmed effort within the approved budget for {engine["model"]}')


def confirm_glm(route, raw):
    observed = raw.get('model')
    _require(raw.get('exit_code') == 0 and not raw.get('timed_out') and raw.get('trace_completed') is True
             and isinstance(observed, str) and claude_glm.reported_model_id(observed) == route['model']
             and raw.get('requested_effort') == route['effort'], 'Claude Code did not confirm the approved invocation')
    return observed


def read_only(route, prompt, seconds, *, editor=claude_glm.execute, reviewer=codex_review.review):
    if route['provider'] == 'glm':
        with TemporaryDirectory(prefix='iboard-role-review-') as folder:
            raw = editor(Path(folder).resolve(), prompt, seconds, read_only=True, file_tools_only=True,
                         capture_trace=True, private_workspace=True, requested_model=route['model'],
                         requested_effort=route['effort'])
        return {'answer': raw.get('stdout'), 'observed_model': confirm_glm(route, raw),
                'requested_effort': raw['requested_effort']}
    return {'answer': reviewer(prompt, route['model'], seconds, effort=route['effort']),
            'observed_model': None, 'requested_effort': route['effort']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action, fields in [('discover', ('out',)), ('probe', ('manifest', 'out')),
                           ('inspect-rule', ('rule', 'out')),
                           ('draft-rule', ('plan', 'plan-sha256', 'mapping', 'mapping-sha256',
                                           'inventory', 'inventory-sha256', 'out')),
                           ('draft', ('inventory', 'inventory-sha256', 'choices', 'out')),
                           ('install', ('profile', 'sha256', 'out')), ('show', ('profile', 'sha256'))]:
        child = sub.add_parser(action)
        for field in fields:
            child.add_argument('--' + field, required=True)
        if action == 'discover':
            child.add_argument('--seconds', type=int, default=20)
        elif action == 'show':
            child.add_argument('--role', choices=ROLES)
            child.add_argument('--other-than', choices=MODELS)
        elif action == 'draft':
            child.add_argument('--previous')
            child.add_argument('--previous-sha256')
        elif action == 'install':
            child.add_argument('--previous-sha256')
    args = vars(parser.parse_args(argv))
    action = args.pop('action')
    try:
        if action == 'show':
            role, other = args.pop('role'), args.pop('other_than')
            data = load(**args)
            _require(not other or role, 'select the judge role with --other-than')
            result = {'budget': data['choices']['budget'], 'routes': data['routes']} if role is None else {
                'role': role, 'route': select(data, role, other_than=other)}
        else:
            result = {'discover': discover, 'probe': probe, 'inspect-rule': inspect_rule,
                      'draft-rule': draft_rule, 'draft': draft, 'install': install}[action](**args)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
