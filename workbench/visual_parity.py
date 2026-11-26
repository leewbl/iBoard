"""Freeze real Chromium states and require pixel-exact candidate proof."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import time

from runner.sandbox import MAX_SCREENSHOT, SandboxError, run_sandbox
from workbench import cli, visual_pixels, workflow


DRIVER = Path(__file__).resolve().parents[1] / 'skills' / 'visual-parity' / 'capture.mjs'
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}')
SHA = re.compile(r'[0-9a-f]{64}')
IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
ROLES = ('implementer', 'independent-verifier')


class ParityError(cli.WorkbenchError):
    pass


def _relative(path):
    if (not isinstance(path, str) or len(path) > 220 or not re.fullmatch(r'[A-Za-z0-9_./-]+', path)
            or any(part in ('', '.', '..') for part in path.split('/'))):
        raise ParityError('visual inputs must use bounded project-relative paths')
    return path


def _text(value, limit, *, empty=False):
    return (isinstance(value, str) and (bool(value) or empty)
            and len(value) <= limit and all(c.isprintable() for c in value))


def manifest(value):
    if (not isinstance(value, dict) or value.get('schema') != 1
            or set(value) - {'schema', 'viewport', 'states', 'server'}):
        raise ParityError('invalid visual state manifest')
    viewport, states = value.get('viewport'), value.get('states')
    if (not isinstance(viewport, dict) or set(viewport) != {'width', 'height'} or
            any(type(size) is not int or not 64 <= size <= 1920 for size in viewport.values())
            or viewport['width'] * viewport['height'] > 2_100_000
            or not isinstance(states, list) or not 1 <= len(states) <= 12):
        raise ParityError('visual parity needs a bounded viewport and one to twelve states')
    server = value.get('server')
    if 'server' in value:
        if (not isinstance(server, dict) or set(server) != {'entry', 'args', 'port', 'ready'}
                or type(server['port']) is not int or not 1024 <= server['port'] <= 65535
                or not isinstance(server['args'], list) or len(server['args']) > 16
                or any(not _text(arg, 512) for arg in server['args'])):
            raise ParityError('invalid loopback test server')
        _relative(server['entry'])
        _route(server['ready'])
    ids = set()
    for state in states:
        if (not isinstance(state, dict) or set(state) != {'id', 'path', 'steps'}
                or not isinstance(state['id'], str) or not ID.fullmatch(state['id']) or state['id'].lower() in ids
                or not isinstance(state['steps'], list) or len(state['steps']) > 12):
            raise ParityError('visual parity needs unique states and bounded steps')
        ids.add(state['id'].lower())
        _route(state['path']) if server else _relative(state['path'])
        for step in state['steps']:
            if not isinstance(step, dict):
                raise ParityError('invalid visual interaction')
            action = step.get('action')
            fields = {'action', 'selector'}
            if action in ('fill', 'press', 'text'):
                fields.add('value')
            if (set(step) != fields or action not in ('click', 'hover', 'fill', 'press', 'visible', 'text')
                    or not _text(step['selector'], 240)
                    or ('value' in fields and not _text(step['value'], 1024, empty=action in ('fill', 'text')))):
                raise ParityError('invalid visual interaction')
    return value


def _route(value):
    if (not isinstance(value, str) or not value.startswith('/') or value.startswith('//')
            or not re.fullmatch(r'/[A-Za-z0-9_./-]{0,220}', value)
            or any(part in ('.', '..') for part in value.split('/'))):
        raise ParityError('test server routes must stay on the declared loopback origin')


def _read(path, limit):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ParityError(f'visual evidence is missing or exceeds its limit: {path.name}')
    with path.open('rb') as source:
        data = source.read(limit + 1)
    if not data or len(data) > limit:
        raise ParityError(f'invalid visual evidence size: {path.name}')
    return data


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _json(path, limit=1_000_000):
    result = json.loads(_read(path, limit))
    if not isinstance(result, dict):
        raise ParityError(f'invalid visual evidence object: {path.name}')
    return result


def _reference(path):
    return {'path': str(path), 'sha256': cli.digest(path)}


def _bounded_time(start, timeout):
    remaining = timeout - (time.monotonic() - start)
    if remaining < 1:
        raise ParityError('visual parity budget expired')
    return remaining


def _source_paths(repo, revision, plan):
    if cli.git(repo, 'ls-tree', revision, '--', 'node_modules', timeout=15):
        raise ParityError('visual source checkout must keep the frozen browser dependencies outside tracked files')
    server = plan.get('server')
    paths = [server['entry']] if server else list({state['path'] for state in plan['states']})
    for path in paths:
        entry = cli.git(repo, 'ls-tree', revision, '--', path, timeout=15)
        if not entry.startswith(('100644 blob ', '100755 blob ')) or entry.partition('\t')[2] != path:
            raise ParityError(f'visual entry must be a tracked regular file: {path}')


def _image_meta(image, timeout):
    try:
        result = subprocess.check_output(['docker', 'image', 'inspect', image], timeout=min(timeout, 15), text=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise ParityError('cannot inspect the frozen local browser image') from error
    meta = json.loads(result)
    if len(meta) != 1 or meta[0].get('Id') != image:
        raise ParityError('visual Docker image identity changed')
    return meta[0]


def _same_runtime(before, after, timeout):
    base, candidate = (_image_meta(image, timeout) for image in (before, after))
    layers = base['RootFS']['Layers']
    if (not layers or candidate['RootFS']['Layers'][:len(layers)] != layers
            or any(base.get(key) != candidate.get(key) for key in ('Architecture', 'Os'))
            or base['Config'].get('Env') != candidate['Config'].get('Env')):
        raise ParityError('candidate image must retain the frozen browser image and its runtime configuration')


def _raw_capture(path, values, state, role, revision, image):
    report_path, screenshot_path = path / (role + '.json'), path / (role + '.png')
    log = _read(report_path, 2_500_000)
    image_bytes = _read(screenshot_path, MAX_SCREENSHOT)
    if values != {'report_sha256': _digest(log), 'png_sha256': _digest(image_bytes)}:
        raise ParityError(f'visual capture hashes changed: {state["id"]}/{role}')
    report = json.loads(log)
    if not isinstance(report, dict) or not isinstance(report.get('stdout'), str):
        raise ParityError('visual execution report has no browser receipt')
    receipt = json.loads(report.get('stdout', ''))
    if not isinstance(receipt, dict):
        raise ParityError('visual execution report has an invalid browser receipt')
    container = str(report.get('create_stdout', '')).strip()
    reported_path = Path(str(report.get('screenshot_path', '')))
    if (report.get('exit_code') != 0 or report.get('error') or report.get('stdout_truncated')
            or report.get('timed_out') or report.get('cleanup_confirmed') is not True
            or report.get('container_created') is not True or not SHA.fullmatch(container)
            or report.get('source_root') != '/app' or report.get('workspace_read_only') is not True
            or report.get('image') != image or report.get('source_verified_commit') != revision
            or report.get('actor') != role or report.get('command') != ['node', '/workspace/capture.mjs', state['id']]
            or report.get('screenshot_sha256') != _digest(image_bytes)
            or not reported_path.is_absolute() or reported_path.parts[-2:] != (state['id'], role + '.png')
            or receipt.get('kind') != 'visual-parity-capture' or receipt.get('state') != state['id']
            or receipt.get('actions') != state['steps'] or not _text(receipt.get('browser'), 120)
            or not _text(receipt.get('playwright'), 120) or not SHA.fullmatch(str(receipt.get('chromium_sha256')))
            or receipt.get('screenshot_sha256') != _digest(image_bytes)):
        raise ParityError(f'visual capture identity is invalid: {state["id"]}/{role}')
    return image_bytes, receipt, container


def _run(root, plan, driver, revision, image, timeout, memory_mb):
    started = time.monotonic()
    records = {}
    with TemporaryDirectory(prefix='iboard-visual-driver-') as scratch:
        (Path(scratch) / 'capture.mjs').write_bytes(driver)
        (Path(scratch) / 'manifest.json').write_text(json.dumps(plan), encoding='utf-8')
        for state in plan['states']:
            folder = root / state['id']
            folder.mkdir()
            records[state['id']] = {}
            for role in ROLES:
                image_path = folder / (role + '.png')
                try:
                    report = run_sandbox(scratch, image, ['node', '/workspace/capture.mjs', state['id']],
                                         _bounded_time(started, timeout), memory_mb=memory_mb,
                                         workspace_read_only=True, source_root='/app', screenshot_output=image_path)
                except (OSError, SandboxError) as error:
                    report = {'error': str(error), 'container_created': False, 'cleanup_confirmed': False}
                report.update(source_verified_commit=revision, actor=role)
                path = folder / (role + '.json')
                cli.write(path, report)
                if not image_path.is_file():
                    raise ParityError(f'visual capture failed; report saved at {path}')
                records[state['id']][role] = {'report_sha256': cli.digest(path), 'png_sha256': cli.digest(image_path)}
    return records


def _inspect(root, plan, records, revision, image, *, check_repeat):
    states = {state['id']: state for state in plan['states']}
    if not isinstance(records, dict) or set(records) != states.keys():
        raise ParityError('visual evidence does not contain the frozen state set')
    fingerprint, containers, images = None, set(), {}
    for state_id, roles in records.items():
        if not isinstance(roles, dict) or set(roles) != set(ROLES):
            raise ParityError('visual evidence needs both independent capture roles')
        images[state_id] = {}
        for role in ROLES:
            image_bytes, receipt, container = _raw_capture(root / state_id, roles[role], states[state_id], role,
                                                            revision, image)
            current = {key: receipt[key] for key in ('browser', 'playwright', 'chromium_sha256', 'viewport')}
            if container in containers or receipt['viewport'] != plan['viewport'] or (fingerprint and current != fingerprint):
                raise ParityError('visual captures must use distinct containers and the same browser and viewport')
            fingerprint = current
            containers.add(container)
            images[state_id][role] = image_bytes
        if check_repeat:
            check = visual_pixels.compare(*images[state_id].values())
            target = [plan['viewport']['width'], plan['viewport']['height']]
            if not check['equal'] or check['baseline_dimensions'] != target:
                raise ParityError(f'visual baseline is unstable or has the wrong viewport for {state_id}')
    return fingerprint, images, containers


def capture_baseline(repo, revision, image, manifest_path, out, timeout, memory_mb):
    started = time.monotonic()
    raw = _read(Path(manifest_path), 80_000)
    plan = manifest(json.loads(raw))
    revision = cli.sha(repo, revision, timeout=15)
    _source_paths(repo, revision, plan)
    image = workflow._verified_image(repo, image, min(timeout, 300), commit=revision)
    out.mkdir(parents=True, mode=0o700)
    plan_path = out / 'manifest.json'
    plan_path.write_bytes(raw)
    driver = _read(DRIVER, 100_000)
    records = _run(out, plan, driver, revision, image, _bounded_time(started, timeout), memory_mb)
    _inspect(out, plan, records, revision, image, check_repeat=True)
    path = out / 'baseline.json'
    cli.write(path, {'schema': 1, 'revision': revision, 'image': image, 'driver_sha256': _digest(driver),
                     'manifest_sha256': _digest(raw), 'states': records})
    return _reference(path)


def read_baseline(ref, expected_base):
    if (not isinstance(ref, dict) or set(ref) != {'path', 'sha256'} or not isinstance(ref['path'], str)
            or not SHA.fullmatch(str(ref['sha256']))):
        raise ParityError('visual baseline needs an exact path and SHA-256')
    path = Path(ref['path'])
    raw = _read(path, 100_000)
    proof = json.loads(raw)
    if (not isinstance(proof, dict) or _digest(raw) != ref['sha256']
            or proof.get('schema') != 1 or proof.get('revision') != expected_base
            or set(proof) != {'schema', 'revision', 'image', 'driver_sha256', 'manifest_sha256', 'states'}
            or not IMAGE.fullmatch(str(proof.get('image'))) or proof.get('driver_sha256') != cli.digest(DRIVER)):
        raise ParityError('visual baseline digest, Git revision, or trusted driver changed')
    payload = _read(path.with_name('manifest.json'), 80_000)
    if _digest(payload) != proof.get('manifest_sha256'):
        raise ParityError('frozen visual state manifest changed')
    plan = manifest(json.loads(payload))
    return path, proof, plan


def bind(path, digest, base):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ParityError('visual baseline record must be a regular file')
    path = path.resolve(strict=True)
    ref = {'path': str(path), 'sha256': digest}
    path, proof, plan = read_baseline(ref, base)
    _inspect(path.parent, plan, proof['states'], base, proof['image'], check_repeat=True)
    return ref


def _measure(root, plan, old, live, *, masks=False):
    results = {}
    target = [plan['viewport']['width'], plan['viewport']['height']]
    for state_id, pair in live.items():
        checks = {}
        for role, payload in pair.items():
            diff = root / state_id / (role + '-diff.png') if masks else None
            observed = visual_pixels.compare(old[state_id][role], payload, diff_path=diff)
            if observed['baseline_dimensions'] != target:
                raise ParityError(f'visual baseline dimensions changed: {state_id}/{role}')
            checks[role] = observed
        results[state_id] = checks
    return results


def compare_candidate(repo, base, ref, revision, image, out, timeout, memory_mb):
    started = time.monotonic()
    path, proof, plan = read_baseline(ref, base)
    before, pixels, containers = _inspect(path.parent, plan, proof['states'], base, proof['image'], check_repeat=True)
    revision = cli.sha(repo, revision, timeout=15)
    if subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', base, revision], timeout=15).returncode:
        raise ParityError('visual candidate must descend from its frozen baseline')
    _source_paths(repo, revision, plan)
    image = workflow._verified_image(repo, image, min(_bounded_time(started, timeout), 300), commit=revision)
    _same_runtime(proof['image'], image, _bounded_time(started, timeout))
    driver = _read(DRIVER, 100_000)
    if _digest(driver) != proof['driver_sha256']:
        raise ParityError('trusted visual driver changed during preparation')
    out.mkdir(parents=True, mode=0o700)
    captures = _run(out, plan, driver, revision, image, _bounded_time(started, timeout), memory_mb)
    current, images, used = _inspect(out, plan, captures, revision, image, check_repeat=False)
    if before != current or containers & used:
        raise ParityError('visual candidate changed the browser or reused a baseline container')
    states = _measure(out, plan, pixels, images, masks=True)
    equal = all(check['equal'] for state in states.values() for check in state.values())
    result = out / 'result.json'
    cli.write(result, {'schema': 1, 'baseline': ref, 'candidate': revision, 'image': image, 'states': states,
                       'captures': captures, 'equal': equal})
    return _reference(result), equal


def require(data, head):
    binding = data.get('visual_baseline')
    if binding is None:
        return None
    ref = data.get('visual_parity_result')
    if (not isinstance(ref, dict) or set(ref) != {'path', 'sha256'} or not isinstance(ref['path'], str)
            or not SHA.fullmatch(str(ref['sha256']))):
        raise ParityError('visual parity evidence is required before delivery')
    path = Path(ref['path'])
    payload = _read(path, 90_000)
    result = json.loads(payload)
    if (not isinstance(result, dict) or data['surface'] != 'ui'
            or _digest(payload) != ref['sha256'] or result.get('schema') != 1
            or set(result) != {'schema', 'baseline', 'candidate', 'image', 'states', 'captures', 'equal'}
            or result.get('candidate') != head or result.get('baseline') != binding or result.get('equal') is not True
            or not IMAGE.fullmatch(str(result.get('image')))):
        raise ParityError('visual parity evidence differs from the required candidate')
    base_path, proof, plan = read_baseline(binding, data['base_commit'])
    before, old, containers = _inspect(base_path.parent, plan, proof['states'], data['base_commit'], proof['image'],
                                       check_repeat=True)
    current, new, used = _inspect(path.parent, plan, result['captures'], head, result['image'], check_repeat=False)
    observed = _measure(path.parent, plan, old, new)
    if (before != current or containers & used or result.get('states') != observed
            or any(not check['equal'] for role in observed.values() for check in role.values())):
        raise ParityError('candidate screenshots differ from the frozen visual baseline')
    return ref


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    baseline = commands.add_parser('baseline')
    baseline.add_argument('--manifest', type=Path, required=True)
    candidate = commands.add_parser('compare')
    candidate.add_argument('--baseline', type=Path, required=True)
    candidate.add_argument('--baseline-sha256', required=True)
    for command in (baseline, candidate):
        command.add_argument('--repo', type=Path, required=True)
        command.add_argument('--revision', default='HEAD')
        command.add_argument('--image', required=True)
        command.add_argument('--out', type=Path, required=True)
        command.add_argument('--timeout', type=int, default=180)
        command.add_argument('--memory-mb', type=int, default=2048)
    args = parser.parse_args(argv)
    try:
        repo, out = args.repo.resolve(strict=True), args.out.resolve()
        if out.is_relative_to(repo) or not 1 <= args.timeout <= 1800 or not 128 <= args.memory_mb <= 8192:
            raise ParityError('visual parity requires bounded settings and output outside the source repo')
        if args.action == 'baseline':
            ref = capture_baseline(repo, args.revision, args.image, args.manifest, out, args.timeout, args.memory_mb)
            equal = True
        else:
            path = args.baseline.resolve(strict=True)
            expected = {'path': str(path), 'sha256': args.baseline_sha256}
            base = _json(path)['revision']
            ref, equal = compare_candidate(repo, base, expected, args.revision, args.image, out,
                                           args.timeout, args.memory_mb)
        print(json.dumps(ref))
        return 0 if equal else 1
    except (cli.WorkbenchError, SandboxError, workflow.WorkflowError, OSError, ValueError, KeyError,
            TypeError, AttributeError, subprocess.SubprocessError) as error:
        parser.exit(2, f'visual parity: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
