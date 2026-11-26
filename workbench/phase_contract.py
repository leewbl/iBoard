"""Bind a planned phase and its offline evidence to an ordinary iBoard task."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from workbench import arena, autopilot, benchmark, cli, investigate, multiphase, phase_audit, project_triage, project_verify, workflow
from workbench import visual_parity
from workbench.prompt_profiles import PSTACK_COMMIT
from workbench.task_lock import locked_task


class PhaseContractError(ValueError):
    pass


SHA = re.compile(r'[0-9a-f]{64}')
COMMIT = re.compile(r'[0-9a-f]{40}')
BINDING_FILE = 'phase-binding.json'
RESULT_FILE = 'phase-result.json'
APPROVAL_FILE = 'phase-approval.json'


def _require(condition, message):
    if not condition:
        raise PhaseContractError(message)


def _bytes(path, limit, expected):
    _require(isinstance(expected, str) and SHA.fullmatch(expected), 'phase artifact needs a full SHA-256')
    _require(path.is_file() and not path.is_symlink(), 'phase artifact must be a regular file')
    with path.open('rb') as file:
        contents = file.read(limit + 1)
    _require(len(contents) <= limit and hashlib.sha256(contents).hexdigest() == expected,
             'phase artifact digest or size did not match the approved bytes')
    return contents


def _ref(directory, value):
    _require(isinstance(value, dict) and set(value) == {'path', 'sha256'}
             and isinstance(value['path'], str) and value['path'], 'phase artifact requires a path and digest')
    path = directory / Path(value['path']).expanduser()
    _require(not path.is_symlink(), 'phase artifact cannot be a symbolic link')
    return {'path': str(path.resolve(strict=True)), 'sha256': value['sha256']}


def _json(path, expected, limit=180_000):
    data = json.loads(_bytes(path, limit, expected))
    _require(isinstance(data, dict), 'phase artifact must be a JSON object')
    return data


def _local_ref(path):
    return {'path': str(path), 'sha256': cli.digest(path)}


def _git(repo, *args):
    return investigate._git(repo, *args)


def _commit(repo, revision):
    _require(isinstance(revision, str) and COMMIT.fullmatch(revision), 'phase requires a full Git commit')
    actual = _git(repo, 'rev-parse', '--verify', '--end-of-options', revision + '^{commit}').decode().strip()
    _require(actual == revision, 'phase revision did not directly identify a commit')
    return actual


def _entrypoint(repo, revision, path):
    git_entry = _git(repo, 'ls-tree', revision, '--', path).split(b'\t', 1)[0].split()
    _require(len(git_entry) == 3 and git_entry[0] in (b'100644', b'100755') and git_entry[1] == b'blob',
             f'phase requires a tracked regular file: {path}')
    return git_entry[2].decode('ascii')


def _script(repo, revision, command, marker, exit_code, *, root, scope):
    autopilot._check_spec({'check': {'command': command, 'marker': marker, 'exit_code': exit_code}}, 'check')
    rewritten = [item if not item.startswith(root) else '/workspace/' + item[len(root):] for item in command]
    _require(workflow._workspace_command(rewritten), 'phase check requires a tracked script entrypoint')
    index = 0 if command[0].startswith(root) else 1
    _require(len(command) > index and command[index].startswith(root), f'phase check must use a {root} entrypoint')
    path = arena._paths([command[index][len(root):]])[0]
    _require(path not in scope, 'phase verification scripts must be outside the plan edit scopes')
    return {'path': path, 'blob': _entrypoint(repo, revision, path)}


def _validated_plan(repo, path, expected):
    plan = _json(path, expected)
    _require(plan.get('schema') == cli.SCHEMA and plan.get('status') == 'planned'
             and plan.get('pstack_commit') == PSTACK_COMMIT and plan.get('source') == multiphase.PSTACK_SOURCE
             and plan.get('authorization') == 'plan-only' and plan.get('open_questions') == [],
             'phase execution requires a native completed planning artifact')
    baseline = _commit(repo, plan['baseline'])
    scope = set(arena._paths(plan['scope']))
    multiphase._field(plan['request'], 'request')
    sources = plan['sources']
    keys = [f'S{index}' for index in range(1, len(sources) + 1)]
    _require(bool(keys) and len(keys) <= 12 and set(sources) == set(keys), 'phase plan needs pinned source ranges')
    selections = [f"{sources[key]['path']}:{sources[key]['start']}-{sources[key]['end']}" for key in keys]
    _, observed = investigate._collect(repo, baseline, selections, history=False, remaining=lambda: 12)
    _require(sources == observed, 'phase plan source evidence differs from the pinned repository')
    multiphase._validate({'summary': plan['summary'], 'phases': plan['phases']}, scope, sources)
    _require(plan.get('review_gated') == [entry['id'] for entry in plan['phases'] if entry['interaction']],
             'phase plan interaction review set changed')
    multiphase._prototypes(plan['prototypes'], path, baseline)
    if 'design' in plan:
        reference = _ref(path.parent, plan['design'])
        design = _json(Path(reference['path']), reference['sha256'], 240_000)
        _require(design.get('schema') == cli.SCHEMA and design.get('pstack_commit') == PSTACK_COMMIT
                 and design.get('status') == 'selected' and design.get('commit') == baseline
                 and scope <= set(design['implementation_scope']), 'phase plan design changed')
    return plan


def _benchmark_spec(reference):
    path = Path(reference['path'])
    _bytes(path, 64_000, reference['sha256'])
    spec = benchmark._load(path)
    _require(cli.digest(path) == reference['sha256'], 'phase benchmark changed while being read')
    return spec


def _step(repo, baseline, scope, directory, entry, step):
    fields = {'image', 'unit', 'live', 'feature_doc', 'benchmark', 'helpers'}
    _require(isinstance(step, dict) and fields <= step.keys() <= fields | {'screenshot', 'local_gate', 'visual_parity'},
             'each phase requires an image, unit and live commands, feature documentation and benchmark inputs')
    project_triage._image_reference(step['image'])
    scripts = {name: _script(repo, baseline, step[name], entry['verify'][name]['marker'], 0,
                             root='/app/', scope=scope) for name in ('unit', 'live')}
    _require(scripts['unit']['path'] != scripts['live']['path'], 'unit and live checks need distinct entrypoints')
    if 'screenshot' in step:
        screenshot = step['screenshot']
        _require(isinstance(screenshot, dict) and set(screenshot) == {'command', 'marker', 'exit_code'},
                 'phase screenshot requires command, marker and exit code')
        scripts['screenshot'] = _script(repo, baseline, screenshot['command'], screenshot['marker'],
                                         screenshot['exit_code'], root='/workspace/', scope=scope)
    arena._paths([step['feature_doc']])
    ref = _ref(directory, step['benchmark'])
    spec = _benchmark_spec(ref)
    _require(spec['claim'] == entry['verify']['perf']['probe'], 'phase benchmark claim differs from the planned probe')
    names = {spec[lane]['helper'] for lane in ('workload', 'end_to_end') if lane in spec}
    if 'profile' in spec:
        names.add(spec['profile'])
    _require(isinstance(step['helpers'], dict) and set(step['helpers']) == names,
             'phase needs the exact named helpers declared by its benchmark')
    helpers = {name: _ref(directory, value) for name, value in step['helpers'].items()}
    for reference in helpers.values():
        path = Path(reference['path'])
        frozen = project_verify._helper(path.parent, path.name)
        _require(hashlib.sha256(frozen).hexdigest() == reference['sha256'], 'phase benchmark helper digest changed')
    if 'local_gate' in step:
        from workbench import local_vitest

        policy = step['local_gate']
        _require(isinstance(policy, dict) and set(policy) == {'files', 'reason'},
                 'phase local gate requires files and reason')
        reason = multiphase._field(policy['reason'], 'phase local gate reason').strip()
        files = policy['files']
        try:
            local_vitest.command(repo, baseline, baseline, files)
        except local_vitest.GuardError as error:
            raise PhaseContractError(f'invalid planned local test files: {error}') from error
        step = {**step, 'local_gate': {'files': files, 'reason': reason}}
    if 'visual_parity' in step:
        visual = step['visual_parity']
        _require('screenshot' in step and isinstance(visual, dict) and set(visual) == {'path', 'sha256', 'driver_sha256'},
                 'phase visual parity requires the UI screenshot check and a frozen state manifest and driver')
        visual_ref = _ref(directory, {key: visual[key] for key in ('path', 'sha256')})
        visual_plan = visual_parity.manifest(_json(Path(visual_ref['path']), visual_ref['sha256'], 80_000))
        _require(visual['driver_sha256'] == cli.digest(visual_parity.DRIVER), 'approved visual capture driver changed')
        visual_parity._source_paths(repo, baseline, visual_plan)
        if server := visual_plan.get('server'):
            path = server['entry']
            _require(path not in scope, 'visual server entrypoint must stay outside the approved edit scope')
            scripts['visual_server'] = {'path': path, 'blob': _entrypoint(repo, baseline, path)}
        step = {**step, 'visual_parity': {**visual_ref, 'driver_sha256': visual['driver_sha256']}}
    return {**step, 'benchmark': ref, 'helpers': helpers, 'scripts': scripts,
            'surface': 'ui' if 'screenshot' in step else 'non-ui'}


def load(path, expected_sha256):
    """Load the explicit dispatch authorization and check all referenced source inputs."""
    try:
        path = Path(path).expanduser().absolute()
        manifest = _json(path, expected_sha256, 120_000)
        fields = {'repo', 'plan', 'steps', 'seconds', 'iterations'}
        _require(fields <= manifest.keys() <= fields | {'memory_mb', 'destination'}, 'invalid phase dispatch manifest')
        repo = (path.parent / Path(manifest['repo']).expanduser()).resolve(strict=True)
        limits = {name: manifest[name] for name in ('seconds', 'iterations')}
        _require(all(type(value) is int and 1 <= value <= 2_147_483_647 for value in limits.values()),
                 'phase dispatch needs positive time and iteration limits')
        memory = manifest.get('memory_mb', 1024)
        _require(type(memory) is int and 128 <= memory <= 8192, 'phase dispatch memory must be 128–8192 MiB')
        destination = manifest.get('destination')
        if destination is not None:
            _require(isinstance(destination, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', destination)
                     and all(part not in ('.', '..') for part in destination.split('/')),
                     'phase dispatch destination must name a personal GitHub fork')
        plan_ref = _ref(path.parent, manifest['plan'])
        plan = _validated_plan(repo, Path(plan_ref['path']), plan_ref['sha256'])
        entries = {entry['id']: entry for entry in plan['phases']}
        steps = manifest['steps']
        _require(isinstance(steps, dict) and set(steps) == set(entries), 'phase dispatch must configure every planned step')
        editable = {file for entry in entries.values() for file in entry['files']}
        steps = {key: _step(repo, plan['baseline'], editable, path.parent, entries[key], step)
                 for key, step in steps.items()}
        return {'repo': str(repo), 'plan': plan, 'plan_path': plan_ref['path'], 'plan_sha256': plan_ref['sha256'],
                'steps': steps, **limits, 'memory_mb': memory, 'destination': destination,
                'manifest_path': str(path.resolve(strict=True)), 'manifest_sha256': expected_sha256}
    except PhaseContractError:
        raise
    except multiphase.ERRORS as error:
        raise PhaseContractError(f'invalid phase dispatch authorization ({error})') from error


def criteria(entry):
    return [{'id': name, 'text': entry['verify'][name]['predicate'].strip()} for name in ('unit', 'live')]


def _entry(contract, phase_id):
    return next(entry for entry in contract['plan']['phases'] if entry['id'] == phase_id)


def request(contract, phase_id):
    entry, step = _entry(contract, phase_id), contract['steps'][phase_id]
    payload = {key: entry[key] for key in ('id', 'base', 'depends_on', 'files', 'change', 'observed_result', 'verify')}
    payload['functional_documentation'] = step['feature_doc']
    payload['checks'] = {name: step[name] for name in ('unit', 'live')}
    if 'local_gate' in step:
        payload['local_gate'] = step['local_gate']
    if 'screenshot' in step:
        payload['screenshot'] = step['screenshot']
    if 'visual_parity' in step:
        payload['visual_parity'] = {key: step['visual_parity'][key] for key in ('sha256', 'driver_sha256')}
    return ('Implement this authorized phase only. Other planned phases are context. '
            'Keep all frozen checks and their entrypoints unchanged. The workbench runs them in offline Docker.\n\n'
            f"Original request: {contract['plan']['request']}\n"
            f"Approved phase:\n{json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)}\n")


def task_plan(context):
    """Translate the frozen phase into the existing autopilot plan schema."""
    entry, step = context['entry'], context['step']
    plan = {'status': 'ready', 'criteria': criteria(entry), 'feature_doc': step['feature_doc']}
    for name, legacy_name in (('live', 'resolution'), ('unit', 'tests')):
        plan[legacy_name] = {'command': step[name], 'marker': entry['verify'][name]['marker'], 'exit_code': 0}
    if 'screenshot' in step:
        plan['screenshot'] = step['screenshot']
    return plan


def _image_matches(image, proof):
    return not image.startswith('sha256:') or proof == image


def _benchmark_receipt(contract, phase_id, reference, candidate, directory):
    entry, step = _entry(contract, phase_id), contract['steps'][phase_id]
    expected = _ref(directory, reference)
    path, score = phase_audit._receipt(expected, directory)
    baseline = contract['plan']['baseline']
    values, measured, started = phase_audit._samples(path, score, baseline, candidate, entry['verify']['perf']['probe'])
    spec = _benchmark_spec(step['benchmark'])
    approved_helpers = {name: ref['sha256'] for name, ref in step['helpers'].items()}
    _require(values == {**spec, 'schema': cli.SCHEMA, 'helpers': approved_helpers,
                        'memory_mb': contract['memory_mb'], 'cpu_limit': 2}
             and _image_matches(step['image'], score['images']['baseline']),
             'phase benchmark ran different immutable inputs or resource limits')
    for name, digest in approved_helpers.items():
        contents = project_verify._helper(path.parent / 'frozen', name)
        _require(hashlib.sha256(contents).hexdigest() == digest, 'phase benchmark helper snapshot changed')
    return expected, measured, started, score


def verify_baseline(contract, phase_id, baseline_perf, *, before=None):
    """Verify the original trunk proof before a phase's edit can start."""
    result = _benchmark_receipt(contract, phase_id, baseline_perf, contract['plan']['baseline'],
                                Path(contract['manifest_path']).parent)
    score = result[3]
    _require(score['outcome'] == 'no-measurable-difference' and score['images']['baseline'] == score['images']['candidate'],
             'phase trunk control was not stable on one immutable image')
    if before is not None:
        _require(max(result[2]) < datetime.fromisoformat(before), 'phase trunk performance was not measured before the task began')
    return result[0]


def verify_visual_baseline(contract, phase_id, ref, *, baseline_perf=None, before=None):
    step = contract['steps'][phase_id]
    spec = step.get('visual_parity')
    if spec is None:
        _require(ref is None, 'phase has an unapproved visual baseline')
        return None
    base = contract['plan']['baseline']
    _, proof, _ = visual_parity.read_baseline(ref, base)
    _require(proof['manifest_sha256'] == spec['sha256'] and proof['driver_sha256'] == spec['driver_sha256']
             and _image_matches(step['image'], proof['image']),
             'phase visual baseline does not match the approved states, driver or image')
    if baseline_perf:
        _, perf = phase_audit._receipt(baseline_perf, Path(contract['manifest_path']).parent)
        _require(perf['images']['baseline'] == proof['image'], 'visual baseline and trunk benchmark used different images')
    if before is not None:
        _require(visual_parity.bind(ref['path'], ref['sha256'], base, memory_mb=contract['memory_mb'], before=before) == ref,
                 'phase visual baseline path changed')
    return ref


def _event_ref(data, field, event_type, filename):
    events = [row for row in data.get('events', []) if isinstance(row, dict) and row.get('type') == event_type]
    if field not in data and not events:
        return None
    value = data.get(field)
    _require(isinstance(value, dict) and set(value) == {'path', 'sha256'} and len(events) == 1
             and {key: events[0].get(key) for key in value} == value, 'phase task evidence binding changed')
    path = Path(value['path'])
    _require(path.is_absolute() and path.name == filename and path.resolve(strict=True) == path,
             'phase task evidence path changed')
    return path, value['sha256']


def _workspace_settings(data, contract, entry, parent, *, replay=False):
    step = contract['steps'][entry['id']]
    repo = Path(data['repo'])
    _require(data.get('kind') == 'feature' and data.get('surface') == step['surface']
             and data.get('base_commit') == parent
             and [{'id': row['id'], 'text': row['text']} for row in data['criteria']] == criteria(entry),
             'phase task diverged from its approved acceptance, baseline or surface')
    limits = data['limits']
    _require(limits['iterations'] == contract['iterations'] and type(limits['seconds']) is int
             and 1 <= limits['seconds'] <= contract['seconds'], 'phase task diverged from the approved budgets')
    _require(_git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')
             == _git(contract['repo'], 'rev-parse', '--path-format=absolute', '--git-common-dir'),
             'phase task worktree is outside the approved repository')
    _commit(repo, parent)
    baseline = contract['plan']['baseline']
    if replay:
        _git(repo, 'merge-base', '--is-ancestor', baseline, parent)
    elif entry['base'] == 'trunk':
        _require(parent == baseline, 'phase must start at its declared trunk baseline')
    else:
        _require(parent != baseline, 'phase must start at the declared predecessor candidate')
        _git(repo, 'merge-base', '--is-ancestor', baseline, parent)
    for source in step['scripts'].values():
        _require(_entrypoint(repo, parent, source['path']) == source['blob'],
                 'phase verification entrypoint changed from trunk')
    if step['feature_doc'] not in entry['files']:
        _entrypoint(repo, parent, step['feature_doc'])
    if step.get('local_gate'):
        from workbench import local_gate

        try:
            policy = local_gate.validate_configuration(data, parent)
        except local_gate.LocalGateError as error:
            raise PhaseContractError('planned phase local test policy is missing or invalid') from error
        _require(all(policy[key] == value for key, value in step['local_gate'].items()),
                 'phase local test policy differs from the approved manifest')


def _save(path, data, value, field, event_type):
    _require(not path.exists() and not path.is_symlink() and field not in data
             and not any(event.get('type') == event_type for event in data['events']),
             'phase task artifact is already frozen')
    cli.write(path, value)
    reference = _local_ref(path)
    data[field] = reference
    data['events'].append({'type': event_type, 'at': cli.now(), **reference})
    return reference


def _replayed_candidate(data, contract, entry, parent, replay, baseline_perf):
    from workbench import delivery, phase_replay

    _require(isinstance(replay, dict) and set(replay) == {'source_task', 'source_result', 'source_review', 'proof'},
             'invalid phase replay binding')
    origin = Path(replay['source_task'])
    _require(origin.is_absolute() and origin.name == 'task.json' and origin.resolve(strict=True) == origin,
             'phase replay needs its exact original task path')
    original = cli.read(origin)
    # Reject replay-of-replay before following the source, including cycles.
    original_ref = _event_ref(original, 'phase_binding', 'phase-bound', BINDING_FILE)
    _require(original_ref is not None and 'replay' not in _json(*original_ref),
             'phase replay must use an original verified phase')
    context = read(original)
    _require(context['task_path'] == origin and context['phase'] == entry['id']
             and context['contract']['manifest_sha256'] == contract['manifest_sha256']
             and context['baseline_perf'] == baseline_perf
             and original.get('visual_baseline') == data.get('visual_baseline')
             and original.get('phase_result') == replay['source_result'],
             'original phase scope or evidence changed before replay')
    state = cli.read(origin.with_name('autopilot.json'))
    head = state['candidate_commit']
    _, issues = cli.evidence_gate(original, head)
    _require(state['phase'] in ('delivery', 'complete') and not issues,
             'phase replay requires the unchanged original verified candidate')
    try:
        proof = delivery._independent_verification(original, head)
    except delivery.DeliveryError as error:
        raise PhaseContractError('original independent phase review no longer passes') from error
    _require({'path': proof['path'], 'sha256': proof['sha256']} == replay['source_review'],
             'original independent phase review changed before replay')
    report = phase_replay.validate(replay['proof'], repo=data['repo'], trunk=contract['plan']['baseline'],
                                   old_base=context['parent'], old_head=head, new_base=parent, files=entry['files'])
    old_local, new_local = original.get('local_gate'), data.get('local_gate')
    if old_local is not None:
        from workbench import local_gate

        _require(isinstance(new_local, dict), 'phase replay must retain the original local test policy')
        try:
            local_gate.validate(original, head)
            local_gate.validate_configuration(data, report['candidate'])
        except local_gate.LocalGateError as error:
            raise PhaseContractError('phase replay must retain the original local test policy') from error
        _require(all(old_local[key] == new_local[key] for key in ('mode', 'files', 'reason')),
                 'phase replay changed the authorized local test policy')
    return report['candidate']


def bind(task_path, data, contract, phase_id, parent, request_path, baseline_perf, *, replay=None, visual_baseline=None):
    """Freeze phase identity, exact request, and pre-edit performance evidence."""
    task_path = Path(task_path).resolve(strict=True)
    parent_dir = task_path.parent
    entry = _entry(contract, phase_id)
    _workspace_settings(data, contract, entry, parent, replay=replay is not None)
    before = verify_baseline(contract, phase_id, baseline_perf, before=data['started_at'])
    source = Path(request_path)
    _require(source == parent_dir / 'issue.txt' and not source.is_symlink(), 'phase task request must be its retained issue.txt')
    expected = hashlib.sha256(request(contract, phase_id).encode('utf-8')).hexdigest()
    _bytes(source, 100_000, expected)
    value = {'schema': cli.SCHEMA, 'task': str(task_path), 'task_id': data['id'], 'repo': data['repo'],
             'phase': phase_id, 'parent': parent, 'request_sha256': expected, 'criteria_sha256': cli.criteria_digest(data),
             'limits': data['limits'], 'started_at': data['started_at'],
             'manifest': {'path': contract['manifest_path'], 'sha256': contract['manifest_sha256']},
             'baseline_perf': before}
    verify_visual_baseline(contract, phase_id, visual_baseline, baseline_perf=before, before=data['started_at'])
    if visual_baseline is not None:
        value['visual_baseline'] = data['visual_baseline'] = visual_baseline
    if replay is not None:
        value['replay'] = replay
        value['replayed_candidate'] = _replayed_candidate(data, contract, entry, parent, replay, before)
    return _save(parent_dir / BINDING_FILE, data, value, 'phase_binding', 'phase-bound')


def read(data):
    """Read and revalidate a phase binding, or return None for ordinary tasks."""
    try:
        ref = _event_ref(data, 'phase_binding', 'phase-bound', BINDING_FILE)
        if ref is None:
            return None
        path, digest = ref
        value = _json(path, digest)
        task_path = path.parent / 'task.json'
        _require(value.get('schema') == cli.SCHEMA and value.get('task') == str(task_path)
                 and value.get('task_id') == data['id'] and value.get('repo') == data['repo']
                 and value.get('criteria_sha256') == cli.criteria_digest(data)
                 and value.get('limits') == data['limits'] and value.get('started_at') == data['started_at'],
                 'phase task identity, budgets or criteria changed')
        contract = load(value['manifest']['path'], value['manifest']['sha256'])
        entry = _entry(contract, value['phase'])
        _workspace_settings(data, contract, entry, value['parent'], replay='replay' in value)
        request_path = task_path.parent / 'issue.txt'
        expected = hashlib.sha256(request(contract, value['phase']).encode('utf-8')).hexdigest()
        _require(value['request_sha256'] == expected, 'phase approval no longer matches its original request')
        _bytes(request_path, 100_000, expected)
        _require(verify_baseline(contract, entry['id'], value['baseline_perf'], before=data['started_at']) == value['baseline_perf'],
                 'phase trunk baseline evidence changed')
        _require(data.get('visual_baseline') == value.get('visual_baseline'), 'phase visual baseline binding changed')
        verify_visual_baseline(contract, entry['id'], value.get('visual_baseline'), baseline_perf=value['baseline_perf'])
        if 'replay' in value:
            _require(value.get('replayed_candidate') == _replayed_candidate(data, contract, entry, value['parent'],
                                                                            value['replay'], value['baseline_perf']),
                     'replayed phase candidate changed')
        else:
            _require('replayed_candidate' not in value, 'phase candidate has no verified replay')
        return {**value, 'contract': contract, 'entry': entry, 'step': contract['steps'][value['phase']],
                'task_path': task_path}
    except PhaseContractError:
        raise
    except (*multiphase.ERRORS, StopIteration) as error:
        raise PhaseContractError(f'invalid retained phase task ({error})') from error


def _changed(repo, context, candidate):
    commands = [('diff', '--cached', '--name-only', '--no-renames', '-z', 'HEAD', '--'),
                ('diff', '--name-only', '--no-renames', '-z', '--'), ('ls-files', '--others', '-z', '--')]
    if candidate is not None:
        if 'replayed_candidate' in context:
            _require(candidate == context['replayed_candidate'], 'candidate differs from the verified replay patch')
        _commit(repo, candidate)
        parent = context['parent']
        _require(candidate != parent, 'phase candidate must contain a change')
        _git(repo, 'merge-base', '--is-ancestor', parent, candidate)
        history = _git(repo, 'rev-list', '--first-parent', '--parents', parent + '..' + candidate).decode().splitlines()
        _require(bool(history) and all(len(line.split()) == 2 for line in history)
                 and history[-1].split()[1] == parent,
                 'phase candidate must follow a linear history rooted at its approved parent')
        commands = [('diff', '--name-only', '--no-renames', '-z', parent, candidate, '--'),
                    ('log', '--format=', '--name-only', '--no-renames', '-z', parent + '..' + candidate, '--')]
    paths = {name.decode('utf-8') for command in commands for name in _git(repo, *command).split(b'\0') if name}
    outside = paths - set(context['entry']['files'])
    _require(not outside, 'phase changes exceed the approved scope ' + ', '.join(sorted(outside)[:10]))
    if candidate is not None:
        for script in context['step']['scripts'].values():
            _require(_entrypoint(repo, candidate, script['path']) == script['blob'],
                     'phase verification entrypoint changed in the candidate')
    return sorted(paths)


def check_scope(data, candidate=None):
    context = read(data)
    return _changed(data['repo'], context, candidate) if context else []


def _verify_result(data, context, record, candidate):
    _require(record.get('commit') == candidate and record.get('baseline_perf') == context['baseline_perf'],
             'phase proof does not use the bound candidate and earlier trunk measurement')
    _changed(data['repo'], context, candidate)
    directory = context['task_path'].parent
    contract, entry, step = context['contract'], context['entry'], context['step']
    _, _, _, performance = _benchmark_receipt(contract, entry['id'], record['perf'], candidate, directory)
    parity = record.get('visual_parity')
    if step.get('visual_parity'):
        _require(parity is not None and parity == data.get('visual_parity_result'), 'phase visual result binding changed')
        visual_result = _json(Path(parity['path']), parity['sha256'], 90_000)
        _require(visual_result.get('candidate') == candidate and visual_result.get('equal') is True
                 and visual_result.get('image') == performance['images']['candidate'],
                 'phase performance and visual checks must use the same candidate image')
    else:
        _require(parity is None, 'phase proof has an unapproved visual result')
    for name, kind in (('unit', 'test-result'), ('live', 'resolution')):
        path, run = phase_audit._receipt(record[name], directory)
        _require(path.parent == directory and run.get('kind') == kind and run.get('command') == step[name]
                 and run.get('actor') == 'independent-verifier' and run.get('actor') != data['implementer']
                 and run.get('workspace') == data['repo'] and run.get('source_verified_commit') == candidate
                 and run.get('expected_marker') == entry['verify'][name]['marker'] and run.get('expected_exit') == 0
                 and run.get('source_root') == '/app' and run.get('memory_mb') == contract['memory_mb']
                 and run.get('source_mode') == 'image' and run.get('verified_image_id') == run.get('image')
                 and (parity is None or run.get('image') == visual_result['image'])
                 and any(item.get('path') == str(path) and item.get('sha256') == record[name]['sha256']
                         and item.get('kind') == kind and item.get('commit') == candidate
                         and item.get('actor') == run['actor'] and not item.get('superseded')
                         and item.get('criteria_digest') == cli.criteria_digest(data) for item in data['evidence']),
                 'phase unit and live evidence must come from exact, independent candidate checks')
    technical = {key: value for key, value in record.items() if key != 'visual_parity'}
    return phase_audit._phase(data['repo'], entry, technical, contract['plan']['baseline'], context['parent'], directory)


def record_completion(task_path, data, record):
    context = read(data)
    _require(context is not None and context['task_path'] == Path(task_path).resolve(strict=True),
             'cannot record completion for a different phase task')
    candidate = record['commit']
    _require(cli.sha(data['repo'], 'HEAD') == candidate, 'phase proof candidate is not checked out')
    if context['step'].get('visual_parity'):
        record = {**record, 'visual_parity': visual_parity.require(data, candidate, phase=context)}
    result = _verify_result(data, context, record, candidate)
    value = {'schema': cli.SCHEMA, 'phase': context['phase'], 'binding_sha256': data['phase_binding']['sha256'],
             'record': record, 'result': result}
    _save(context['task_path'].parent / RESULT_FILE, data, value, 'phase_result', 'phase-result')
    return result


def verify_completion(data, candidate, *, context=None):
    context = context or read(data)
    if not context:
        return None
    ref = _event_ref(data, 'phase_result', 'phase-result', RESULT_FILE)
    _require(ref is not None and ref[0].parent == context['task_path'].parent, 'phase lacks committed performance and check evidence')
    value = _json(*ref)
    _require(value.get('schema') == cli.SCHEMA and value.get('phase') == context['phase']
             and value.get('binding_sha256') == data['phase_binding']['sha256'], 'phase completion does not match the task')
    result = _verify_result(data, context, value['record'], candidate)
    _require(value.get('result') == result, 'phase completion summary changed from the actual evidence')
    return result


def _approval(data, candidate, context):
    ref = _event_ref(data, 'phase_approval', 'phase-approved', APPROVAL_FILE)
    if ref is None:
        return None
    _require(ref[0].parent == context['task_path'].parent, 'phase approval belongs to a different task')
    approval = _json(*ref)
    _require(approval.get('schema') == cli.SCHEMA and approval.get('phase') == context['phase']
             and approval.get('candidate') == candidate
             and approval.get('result_sha256') == data['phase_result']['sha256'],
             'operator approval did not review the exact candidate and evidence')
    multiphase._field(approval['approved_by'], 'approver')
    multiphase._field(approval['reason'], 'approval reason')
    return approval


def interaction_approved(data, candidate, *, context=None):
    context = context or read(data)
    return not context or not context['entry']['interaction'] or _approval(data, candidate, context) is not None


def require_delivery(data, candidate, *, destination=None):
    """Require planned proof at delivery and candidate-specific consent for interaction changes."""
    context = read(data)
    if context is None:
        return None
    result = verify_completion(data, candidate, context=context)
    if destination is not None:
        _require(context['contract']['destination'] == destination, 'phase was not authorized for this GitHub destination')
        _require(interaction_approved(data, candidate, context=context), 'phase interaction awaits operator review')
    if context['step'].get('local_gate'):
        from workbench import local_gate

        try:
            local_gate.validate(data, candidate)
        except local_gate.LocalGateError as error:
            raise PhaseContractError(f'approved local phase tests did not clear: {error}') from error
    return result


@locked_task('task_path')
def approve_interaction(task_path, *, expected_candidate, expected_result_sha256, approved_by, reason):
    """Record an explicit review of the exact interaction candidate and its evidence."""
    from workbench import delivery

    task = Path(task_path).resolve(strict=True)
    data = cli.read(task)
    context = read(data)
    _require(context is not None and context['entry']['interaction'] and context['task_path'] == task,
             'only a bound interaction phase requires operator approval')
    _require(cli.sha(data['repo'], 'HEAD') == expected_candidate
             and not _git(data['repo'], 'status', '--porcelain', '--untracked-files=all'),
             'operator must review the clean checked-out candidate')
    verify_completion(data, expected_candidate, context=context)
    delivery._independent_verification(data, expected_candidate)
    _require(data['phase_result']['sha256'] == expected_result_sha256, 'operator reviewed a different proof digest')
    value = {'schema': cli.SCHEMA, 'phase': context['phase'], 'candidate': expected_candidate,
             'result_sha256': expected_result_sha256, 'approved_by': multiphase._field(approved_by, 'approver'),
             'reason': multiphase._field(reason, 'approval reason')}
    ref = _save(task.parent / APPROVAL_FILE, data, value, 'phase_approval', 'phase-approved')
    cli.write(task, data)
    return ref
