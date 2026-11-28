"""Run phase benchmarks on the same pinned images used for acceptance."""

import hashlib
import re
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from workbench import benchmark, cli, multiphase, phase_audit, workflow


class PhaseRuntimeError(ValueError):
    pass


def _snapshot(reference, limit, label):
    if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
            or not isinstance(reference['sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', reference['sha256'])):
        raise PhaseRuntimeError(f'{label} needs an approved path and digest')
    path = Path(reference['path']).expanduser()
    if path.is_symlink() or not path.is_file():
        raise PhaseRuntimeError(f'{label} must be a regular file')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit or hashlib.sha256(data).hexdigest() != reference['sha256']:
        raise PhaseRuntimeError(f'{label} changed since approval or exceeds its size bound')
    return data


def _reference(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise PhaseRuntimeError(f'missing or symbolic benchmark evidence at {path}')
    path = path.resolve(strict=True)
    return {'path': str(path), 'sha256': cli.digest(path)}


def _benchmark_result(result_path, spec, step, memory_mb):
    reference = _reference(result_path)
    path, result = phase_audit._receipt(reference, result_path.parent)
    expected = {**spec, 'schema': cli.SCHEMA, 'commits': result['commits'],
                'helpers': {name: ref['sha256'] for name, ref in step['helpers'].items()},
                'memory_mb': memory_mb, 'cpu_limit': 2}
    contract_path = path.parent / 'frozen' / 'contract.json'
    if cli.digest(contract_path) != result.get('contract_sha256') or cli.read(contract_path) != expected:
        raise PhaseRuntimeError('frozen benchmark differs from the approved workload')
    for name, digest in expected['helpers'].items():
        helper = path.parent / 'frozen' / benchmark._helper_name(name)
        _snapshot({'path': helper, 'sha256': digest}, 128_000, 'frozen benchmark helper')
    return reference, result


def _capture_inputs(step, probe, scratch):
    manifest = scratch / 'benchmark.json'
    manifest.write_bytes(_snapshot(step['benchmark'], 64_000, 'benchmark contract'))
    spec = benchmark._load(manifest)
    if spec['claim'] != probe:
        raise PhaseRuntimeError('benchmark workload does not match the planned probe')
    required = {spec[lane]['helper'] for lane in ('workload', 'end_to_end') if lane in spec}
    if 'profile' in spec:
        required.add(spec['profile'])
    if set(step['helpers']) != required:
        raise PhaseRuntimeError('approved benchmark helpers do not match its contract')
    helpers = scratch / 'helpers'
    helpers.mkdir(mode=0o700)
    for name, reference in step['helpers'].items():
        (helpers / benchmark._helper_name(name)).write_bytes(_snapshot(reference, 128_000, 'benchmark helper'))
    return manifest, helpers, spec


def _timeout(timeout):
    benchmark._number(timeout, 1, 2_147_483_647, 'available phase time')
    deadline = time.monotonic() + timeout

    def remaining():
        left = deadline - time.monotonic()
        if left < 1:
            raise PhaseRuntimeError('phase benchmark time limit expired')
        return min(3600, left)

    return remaining


def _baseline_result(result_path, spec, step, memory, trunk, probe):
    ref, result = _benchmark_result(result_path, spec, step, memory)
    phase_audit._samples(result_path, result, trunk, trunk, probe)
    if result['outcome'] != 'no-measurable-difference':
        raise PhaseRuntimeError('trunk control reported a directional difference on the same commit')
    images = result['images']
    if images['baseline'] != images['candidate']:
        raise PhaseRuntimeError('trunk control used different images for the same commit')
    return ref, images['baseline']


def baseline(contract, phase_id, out, *, timeout, image=None, runner=benchmark.run):
    """Measure the plan's original trunk before the phase has an implementation."""
    output = Path(out).expanduser().resolve()
    path = output / 'result.json'
    try:
        if output.exists():
            raise PhaseRuntimeError('phase benchmark output already exists')
        remaining = _timeout(timeout)
        trunk = contract['plan']['baseline']
        phase = next(entry for entry in contract['plan']['phases'] if entry['id'] == phase_id)
        probe = phase['verify']['perf']['probe']
        step, memory = contract['steps'][phase_id], contract['memory_mb']
        image = image or step['image']
        with TemporaryDirectory(prefix='iboard-phase-benchmark-') as scratch:
            manifest, helpers, spec = _capture_inputs(step, probe, Path(scratch))
            runner(repo=contract['repo'], base=trunk, revision=trunk, image=image, manifest=manifest,
                   helpers=helpers, out=output, timeout=remaining(), memory_mb=memory)
        ref, pinned = _baseline_result(path, spec, step, memory, trunk, probe)
        if image.startswith('sha256:') and pinned != image:
            raise PhaseRuntimeError('trunk control did not use the approved immutable image')
        remaining()
        return ref
    except (*multiphase.ERRORS, StopIteration, AttributeError) as error:
        raise PhaseRuntimeError(f'trunk benchmark failed: {error}; evidence path {path}') from error


def _check_report(data, report, lane, candidate, image, entry, step, origin):
    supplied = report if isinstance(report, dict) else _reference(report)
    path, raw = phase_audit._receipt(supplied, origin)
    ref = {'path': str(path), 'sha256': supplied['sha256']}
    kind = 'test-result' if lane == 'unit' else 'resolution'
    marker = entry['verify'][lane]['marker']
    phase_audit._execution(raw, candidate, image, marker)
    if (raw.get('command') != step[lane] or raw.get('kind') != kind
            or raw.get('actor') != 'independent-verifier' or raw.get('source_mode') != 'image'
            or raw.get('verified_image_id') != image or raw.get('source_verified_commit') != candidate
            or raw.get('expected_marker') != marker or raw.get('expected_exit') != 0):
        raise PhaseRuntimeError(f'{lane} does not match the authorized independent execution')
    if not any(item.get('path') == ref['path'] and item.get('sha256') == ref['sha256']
               and item.get('kind') == kind and item.get('commit') == candidate
               and item.get('actor') == 'independent-verifier' and not item.get('superseded')
               for item in data['evidence']):
        raise PhaseRuntimeError(f'{lane} is missing from the candidate task evidence')
    return ref


def candidate(data, phase, candidate, image, unit_report, live_report, out, *, timeout,
              runner=benchmark.run, attest=workflow._verified_image):
    """Benchmark the tested candidate using its existing immutable image."""
    output = Path(out).expanduser().resolve()
    path = output / 'result.json'
    try:
        if output.exists():
            raise PhaseRuntimeError('phase benchmark output already exists')
        if not isinstance(image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
            raise PhaseRuntimeError('candidate benchmark needs the tested immutable image')
        remaining = _timeout(timeout)
        contract, entry, step = phase['contract'], phase['entry'], phase['step']
        trunk, memory = contract['plan']['baseline'], contract['memory_mb']
        probe = entry['verify']['perf']['probe']
        repo = Path(data['repo']).resolve(strict=True)
        prior = phase['baseline_perf']
        before_path, _ = phase_audit._receipt(prior, output.parent)
        unit = _check_report(data, unit_report, 'unit', candidate, image, entry, step, output.parent)
        live = _check_report(data, live_report, 'live', candidate, image, entry, step, output.parent)
        with TemporaryDirectory(prefix='iboard-phase-benchmark-') as scratch:
            manifest, helpers, spec = _capture_inputs(step, probe, Path(scratch))
            before, pinned = _baseline_result(before_path, spec, step, memory, trunk, probe)
            if before['sha256'] != prior['sha256']:
                raise PhaseRuntimeError('trunk measurement changed after the phase began')

            def use_candidate(repository, base, head, image_id, time_limit):
                if (Path(repository).resolve(), base, head, image_id) != (repo, trunk, candidate, pinned):
                    raise PhaseRuntimeError('benchmark requested a different trunk or candidate')
                if attest(repo, image, min(time_limit, remaining(), 300), commit=candidate) != image:
                    raise PhaseRuntimeError('tested candidate image no longer matches its immutable identity')
                return image

            runner(repo=repo, base=trunk, revision=candidate, image=pinned, manifest=manifest, helpers=helpers,
                   out=output, timeout=remaining(), memory_mb=memory, derive=use_candidate)
        perf, result = _benchmark_result(path, spec, step, memory)
        if result.get('issues'):
            raise PhaseRuntimeError(f"inconclusive benchmark: {str(result['issues'][0])[:500]}")
        if result['images'] != {'baseline': pinned, 'candidate': image}:
            raise PhaseRuntimeError('benchmark did not measure the trunk and candidate images used for verification')
        record = {'commit': candidate, 'unit': unit, 'live': live, 'baseline_perf': before, 'perf': perf}
        phase_audit._phase(repo, entry, record, trunk, phase['parent'], output.parent)
        remaining()
        return record
    except (*multiphase.ERRORS, AttributeError) as error:
        raise PhaseRuntimeError(f'candidate benchmark failed: {error}; evidence path {path}') from error
