"""Freeze a measured performance issue and require a verified improvement for delivery."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from workbench import benchmark, cli, project_verify, refactor, run_control
from workbench.automate_me import _digest, _file, _keep, _read, _require
from workbench.task_lock import locked_task


SOURCE = 'skills/poteto-mode/playbooks/perf-issue.md'
ERRORS = (*project_verify.ERRORS, KeyError, TypeError, AttributeError, run_control.ControlError)


def _read_ref(ref, *, limit=2_600_000):
    _require(isinstance(ref, dict) and set(ref) == {'path', 'sha256'} and
             refactor.SHA.fullmatch(ref['sha256']), 'performance evidence needs a path and SHA-256')
    path = _file(ref['path'])
    _require(str(path) == ref['path'], 'performance evidence path changed')
    return path, json.loads(_read(path, limit, ref['sha256']))


def _ref(path):
    path = _file(path)
    return {'path': str(path), 'sha256': cli.digest(path)}


def _inputs(spec):
    lanes = [lane for lane in ('workload', 'end_to_end') if lane in spec]
    _require(spec.get('profile') and spec['production_tuning'] is not None and
             (spec['scope'] == 'end-to-end' or 'end_to_end' in lanes),
             'performance issues require real limiter measurements, matching configuration, and an end-to-end workload')
    return lanes, {spec['profile'], *(spec[lane]['helper'] for lane in lanes)}


def _raw(parent, entry, name, side, label, images, commits, memory, helpers, containers):
    file = f'{name}-{side}.json'
    _require(entry['side'] == side and entry['report'] == file, 'benchmark run order or identity changed')
    _, raw = _read_ref({'path': str(parent / file), 'sha256': entry['sha256']})
    engine = project_verify.ENGINES[Path(label).suffix]
    _require(raw.get('schema') == cli.SCHEMA and raw.get('commit') == commits[side]
             and raw.get('helper_sha256') == helpers[label], 'raw measurement is for another revision or helper')
    refactor._observe(raw, [engine, f'/workspace/{label}'], images[side], memory, containers)
    return raw


def _audit(repo, ref, base, head, spec, helpers, memory):
    path, score = _read_ref(ref)
    commits = {'baseline': base, 'candidate': head}
    lanes, _ = _inputs(spec)
    _require(score['schema'] == cli.SCHEMA and score['commits'] == commits and not score['issues']
             and score['order'] == 'baseline,candidate' and score['measurement_scope'] == spec['scope']
             and score['claim'] == spec['claim'], 'performance evidence is inconclusive or uses a different workload')
    images = score['images']
    _require(set(images) == set(commits) and all(refactor.IMAGE.fullmatch(value) for value in images.values()),
             'benchmark did not use immutable source images')
    directory = path.parent / 'frozen'
    _, contract = _read_ref({'path': str(directory / 'contract.json'), 'sha256': score['contract_sha256']})
    _require(contract == {**spec, 'schema': cli.SCHEMA, 'commits': commits, 'helpers': helpers,
                          'memory_mb': memory, 'cpu_limit': 2}, 'frozen measurement contract changed')
    for helper, digest in helpers.items():
        _read(directory / helper, 128_000, digest)

    expected = [(lane, n, side) for lane in lanes for n in range(1, spec['rounds'] + 1)
                for side in ('baseline', 'candidate')]
    _require([(row['lane'], row['round'], row['side']) for row in score['samples']] == expected,
             'measurement rounds are incomplete or unordered')
    values = {lane: {'baseline': [], 'candidate': []} for lane in lanes}
    times, containers, profiles = [], set(), {}
    for row in score['samples']:
        lane, n, side = row['lane'], row['round'], row['side']
        job = spec[lane]
        raw = _raw(path.parent, row, f'{lane}-{n}', side, job['helper'], images, commits, memory, helpers, containers)
        measured = benchmark._measurement(raw, job)
        _require(row['measurement'] == measured, 'summary differs from the raw timed workload')
        times.append(datetime.fromisoformat(raw['started_at']))
        values[lane][side].append(measured['elapsed_ms'])
    for side in ('baseline', 'candidate'):
        entry = score['profiles'][side]
        raw = _raw(path.parent, entry, 'profile', side, spec['profile'], images, commits, memory, helpers, containers)
        profile = benchmark._profile(raw, spec['workload'], repo, commits[side])
        _require(entry == {**profile, 'side': side, 'report': entry['report'], 'sha256': entry['sha256']},
                 'profile summary does not match the raw measured limiter')
        profiles[side] = profile
        times.append(datetime.fromisoformat(raw['started_at']))
    comparisons = {lane: benchmark._compare(values[lane], spec['minimum_delta_pct']) for lane in lanes}
    _require(times == sorted(times) and score['comparisons'] == comparisons
             and score['outcome'] == comparisons['workload']['observed'], 'performance result is inconsistent')
    return {'images': images, 'profiles': profiles, 'comparisons': comparisons,
            'first': times[0], 'last': times[-1]}


def _plan(data):
    path, plan = _read_ref(data['performance_contract'], limit=200_000)
    _require(plan['schema'] == cli.SCHEMA and plan['source'] == SOURCE and plan['task'] == data['id']
             and plan['base'] == data['base_commit'], 'performance plan does not match the task baseline')
    root = path.parent
    manifest = root / 'benchmark.json'
    _read(manifest, 64_000, plan['manifest_sha256'])
    spec = benchmark._load(manifest)
    _, names = _inputs(spec)
    _require(set(plan['helpers']) == names, 'the required frozen benchmark helpers changed')
    for name, value in plan['helpers'].items():
        _read(root / 'helpers' / name, 128_000, value)
    baseline = _audit(data['repo'], plan['baseline'], plan['base'], plan['base'],
                      spec, plan['helpers'], plan['memory_mb'])
    _require(baseline['images'] == {'baseline': plan['image'], 'candidate': plan['image']}
             and all(row['observed'] == 'no-measurable-difference' for row in baseline['comparisons'].values()),
             'baseline is not a quiet same-commit control')
    return root, plan, spec, baseline


def _clean_base(data):
    _require(data['status'] in {'running', 'paused'} and cli.sha(data['repo'], 'HEAD') == data['base_commit']
             and not cli.git(data['repo'], 'status', '--porcelain', '--untracked-files=all', timeout=20),
             'freeze performance on the clean task baseline before modifying it')


def _budget(data, timeout):
    if data['limits']['seconds'] is not None:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(data['started_at'])).total_seconds()
        timeout = min(timeout, data['limits']['seconds'] - elapsed)
    _require(timeout >= 1, 'task time budget exhausted before the performance run')
    return min(timeout, 3600)


@locked_task('task_path')
def freeze(task_path, manifest, helpers, image, out, *, timeout=600, memory_mb=1024,
           minimum_improvement_pct=None, runner=benchmark.run):
    task = Path(task_path).resolve(strict=True)
    run_control.admit(task, ValueError)
    data = cli.read(task)
    _require('performance_contract' not in data, 'this task already has a frozen performance contract')
    _clean_base(data)
    target = Path(out).resolve()
    _require(not target.is_relative_to(Path(data['repo']).resolve()), 'keep performance evidence outside the checkout')
    spec = benchmark._load(_file(manifest))
    _, names = _inputs(spec)
    minimum = spec['minimum_delta_pct'] if minimum_improvement_pct is None else minimum_improvement_pct
    benchmark._number(minimum, spec['minimum_delta_pct'], 100, 'minimum required improvement')
    target.mkdir(mode=0o700, parents=True)
    frozen = target / 'helpers'
    frozen.mkdir(mode=0o700)
    for name in sorted(names):
        (frozen / name).write_bytes(project_verify._helper(Path(helpers).resolve(strict=True), name))
    specfile = target / 'benchmark.json'
    cli.write(specfile, spec)
    base = data['base_commit']
    report = target / 'before'
    runner(repo=data['repo'], base=base, revision=base, image=image, manifest=specfile, helpers=frozen, out=report,
           memory_mb=memory_mb, timeout=_budget(data, timeout))
    _clean_base(data)
    _budget(data, timeout)
    baseline = _ref(report / 'result.json')
    plan = {'schema': cli.SCHEMA, 'source': SOURCE, 'task': data['id'], 'base': base,
            'manifest_sha256': cli.digest(specfile), 'baseline': baseline, 'memory_mb': memory_mb,
            'image': cli.read(baseline['path'])['images']['baseline'], 'minimum_improvement_pct': minimum,
            'helpers': {name: cli.digest(frozen / name) for name in sorted(names)}}
    data['performance_contract'] = _keep(target / 'plan.json', plan)
    _, _, _, before = _plan(data)
    cli.write(task, data)
    return {'plan': data['performance_contract'], 'base': base,
            'baseline_ms': before['comparisons']['workload']['baseline']['median_ms'],
            'hotspot': before['profiles']['baseline']['source']}


def _result(data, head, ref):
    _, plan, spec, before = _plan(data)
    _require(head != plan['base'], 'performance verification requires a distinct candidate')
    after = _audit(data['repo'], ref, plan['base'], head, spec, plan['helpers'], plan['memory_mb'])
    _require(before['last'] < after['first'] and after['images']['baseline'] == plan['image'],
             'candidate must follow the recorded baseline and reuse its image')
    comparison = after['comparisons']['workload']
    _require(comparison['observed'] == 'faster' and comparison['delta_pct'] >= plan['minimum_improvement_pct'],
             'workload failed to reproduce the required improvement')
    user_path = after['comparisons'].get('end_to_end')
    if user_path:
        _require(user_path['observed'] != 'slower' and user_path['delta_pct'] >= -spec['minimum_delta_pct'],
                 'end-to-end workload regressed')
    return {'baseline_ms': comparison['baseline']['median_ms'], 'candidate_ms': comparison['candidate']['median_ms'],
            'delta_pct': comparison['delta_pct'], 'scope': spec['scope'], 'claim': spec['claim'],
            'profile_before': after['profiles']['baseline'], 'profile_after': after['profiles']['candidate'],
            'artifact': ref, 'end_to_end': user_path}


def compare_candidate(data, head, out, *, timeout, runner=benchmark.run):
    root, plan, spec, _ = _plan(data)
    _require(cli.sha(data['repo'], 'HEAD') == head and head != plan['base']
             and not cli.git(data['repo'], 'status', '--porcelain', '--untracked-files=all', timeout=20),
             'measure the exact clean, distinct task candidate')
    output = Path(out).resolve()
    _require(not output.exists() and not output.is_relative_to(Path(data['repo']).resolve()),
             'choose a new performance evidence directory outside the checkout')
    runner(repo=data['repo'], base=plan['base'], revision=head, image=plan['image'], manifest=root / 'benchmark.json',
           helpers=root / 'helpers', out=output, memory_mb=plan['memory_mb'], timeout=_budget(data, timeout))
    ref = _ref(output / 'result.json')
    try:
        result = _result(data, head, ref)
        _budget(data, timeout)
    except ERRORS as error:
        raise ValueError(f'{error}. Retained measurement: {ref["path"]}') from error
    return ref, result


def require(data, head):
    try:
        return _result(data, head, data['performance_result'])
    except ERRORS as error:
        raise cli.WorkbenchError(f'performance evidence did not pass: {error}') from error


def planning_context(data):
    _, plan, _, before = _plan(data)
    return {'minimum_improvement_pct': plan['minimum_improvement_pct'],
            'baseline': before['comparisons']['workload']['baseline'],
            'profile': before['profiles']['baseline']}


@locked_task('task_path')
def verify(task_path, out, *, timeout=600):
    task = Path(task_path).resolve(strict=True)
    run_control.admit(task, ValueError)
    data = cli.read(task)
    head = cli.sha(data['repo'], 'HEAD')
    data['performance_result'], result = compare_candidate(data, head, out, timeout=timeout)
    cli.write(task, data)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    stages = parser.add_subparsers(dest='stage', required=True)
    for name in ('freeze', 'verify'):
        command = stages.add_parser(name)
        command.add_argument('--task', dest='task_path', required=True)
        command.add_argument('--out', required=True)
        command.add_argument('--timeout', type=int, default=600)
        if name == 'freeze':
            for option in ('manifest', 'helpers', 'image'):
                command.add_argument('--' + option, required=True)
            command.add_argument('--memory-mb', type=int, default=1024)
            command.add_argument('--minimum-improvement-pct', type=float)
    args = vars(parser.parse_args(argv))
    stage = args.pop('stage')
    try:
        result = {'freeze': freeze, 'verify': verify}[stage](**args)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
