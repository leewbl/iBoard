"""Freeze a live browser probe and evaluate its repeated intervention evidence."""

from hashlib import sha256
import json
import re
import subprocess

from workbench import cli
from workbench.runtime_forensics import ForensicsError, SOURCE


SOURCE_LIMIT = 32 * 1024
MANIFEST_LIMIT = 16 * 1024
MAX_OBSERVATION = 10**12
GIT_OBJECT = re.compile(r'[a-f0-9]{40}(?:[a-f0-9]{24})?')
METRIC_ID = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}')
PHASES = ('baseline', 'intervention-1', 'restored-1', 'intervention-2', 'restored-2')
MARK_PREFIX = 'iboard:probe:'


def _blob(repo, revision, path, suffix, limit):
    if (not isinstance(path, str) or len(path) > 400 or not path.endswith(suffix)
            or not SOURCE.fullmatch(path) or any(part in ('.', '..') for part in path.split('/'))):
        raise ForensicsError(f'browser probe requires a repository-relative {suffix} path')
    args = ('--no-replace-objects',)
    entry = cli.git(repo, *args, 'ls-tree', '--full-tree', revision, '--', path, timeout=15)
    header, _, found_path = entry.partition('\t')
    fields = header.split()
    if (found_path != path or len(fields) != 3 or fields[0] not in ('100644', '100755')
            or fields[1] != 'blob' or not GIT_OBJECT.fullmatch(fields[2])):
        raise ForensicsError(f'browser probe requires a tracked regular {suffix} file')
    object_id = fields[2]
    size = int(cli.git(repo, *args, 'cat-file', '-s', object_id, timeout=15))
    if not 0 < size <= limit:
        raise ForensicsError(f'browser probe {suffix} file exceeds its allowed size or is empty')
    raw = subprocess.check_output(
        ['git', '-C', str(repo), *args, 'cat-file', 'blob', object_id], timeout=15)
    if len(raw) != size:
        raise ForensicsError(f'browser probe {suffix} blob size changed during preparation')
    return raw


def _json_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ForensicsError('browser probe manifest contains a duplicate key')
    return obj


def _number(value, minimum, maximum):
    return type(value) in (int, float) and minimum <= value <= maximum


def prepare(repo, revision, manifest):
    """Read the approved manifest and script directly from the immutable commit."""
    if (not isinstance(revision, str) or not GIT_OBJECT.fullmatch(revision)
            or cli.git(repo, '--no-replace-objects', 'cat-file', '-t', revision, timeout=15) != 'commit'):
        raise ForensicsError('browser probe revision must identify an exact Git commit')
    raw = _blob(repo, revision, manifest, '.json', MANIFEST_LIMIT)
    try:
        config = json.loads(raw.decode('utf-8'), object_pairs_hook=_json_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ForensicsError('browser probe manifest must contain UTF-8 JSON') from error
    if (not isinstance(config, dict) or set(config) != {'schema', 'source', 'samples', 'settle_ms', 'metrics'}
            or type(config['schema']) is not int or config['schema'] != 1
            or type(config['samples']) is not int or not 2 <= config['samples'] <= 8
            or type(config['settle_ms']) is not int or not 0 <= config['settle_ms'] <= 2000
            or not isinstance(config['metrics'], dict) or not 1 <= len(config['metrics']) <= 16):
        raise ForensicsError('browser probe manifest does not match its schema')
    for metric, rule in config['metrics'].items():
        if (not METRIC_ID.fullmatch(metric) or not isinstance(rule, dict)
                or set(rule) != {'direction', 'min_change', 'restore_tolerance', 'max_spread'}
                or rule['direction'] not in ('increase', 'decrease')
                or not _number(rule['min_change'], 0, 2 * MAX_OBSERVATION) or rule['min_change'] <= 0
                or any(not _number(rule[key], 0, 2 * MAX_OBSERVATION) for key in ('restore_tolerance', 'max_spread'))):
            raise ForensicsError('browser probe metric rules do not match the schema')
    source_path = config['source']
    source = _blob(repo, revision, source_path, '.js', SOURCE_LIMIT)
    try:
        decoded = source.decode('utf-8')
    except UnicodeError as error:
        raise ForensicsError('browser probe source must be UTF-8 JavaScript') from error
    if not decoded.strip() or '\0' in decoded:
        raise ForensicsError('browser probe source cannot be blank or contain NUL')
    return {
        **config, 'revision': revision,
        'manifest': {'path': manifest, 'sha256': sha256(raw).hexdigest()},
        'source': {'path': source_path, 'sha256': sha256(source).hexdigest()},
    }, source


def request(config):
    """Pass the browser only the staged script identity and observation contract."""
    return {'sha256': config['source']['sha256'], 'samples': config['samples'],
            'settle_ms': config['settle_ms'], 'metrics': list(config['metrics'])}


def _trace_times(trace, window):
    setup, cleanup = MARK_PREFIX + 'setup-complete', MARK_PREFIX + 'cleanup-complete'
    pairs = {phase: (MARK_PREFIX + phase + ':start', MARK_PREFIX + phase + ':end') for phase in PHASES}
    ordered = (setup, *(mark for pair in pairs.values() for mark in pair), cleanup)
    expected = set(ordered)
    events = trace.get('traceEvents') if isinstance(trace, dict) else trace
    if (not isinstance(events, list) or not _number(window.get('start_us'), 0, 10**15)
            or not _number(window.get('end_us'), window['start_us'], 10**15)
            or any(type(window.get(key)) is not int or window[key] <= 0 for key in ('pid', 'tid'))):
        raise ForensicsError('browser probe is missing a valid renderer trace window')
    times = {}
    for item in events:
        if not isinstance(item, dict) or item.get('ph') != 'I' or item.get('cat') != 'blink.user_timing':
            continue
        name = item.get('name')
        if not isinstance(name, str) or name not in expected:
            continue
        timestamp = item.get('ts')
        if (name in times or not _number(timestamp, window['start_us'], window['end_us'])
                or any(type(item.get(key)) is not int or item[key] != window[key] for key in ('pid', 'tid'))):
            raise ForensicsError('browser probe trace markers are duplicated or cross the selected renderer window')
        times[name] = timestamp
    if times.keys() != expected or any(times[a] >= times[b] for a, b in zip(ordered, ordered[1:])):
        raise ForensicsError('browser probe trace markers are missing or out of order')
    return {
        'pid': window['pid'], 'tid': window['tid'], 'setup_complete_us': times[setup],
        'cleanup_complete_us': times[cleanup], 'phases': {
            phase: {'start_us': times[start], 'end_us': times[end],
                    'elapsed_ms': round((times[end] - times[start]) / 1000, 3)}
            for phase, (start, end) in pairs.items()
        },
    }


def _samples(config, receipt):
    if (not isinstance(receipt, dict) or set(receipt) != {'sha256', 'cleanup_confirmed', 'phases'}
            or receipt['sha256'] != config['source']['sha256'] or receipt['cleanup_confirmed'] is not True
            or not isinstance(receipt['phases'], list) or len(receipt['phases']) != len(PHASES)):
        raise ForensicsError('browser probe receipt does not match its source or cleanup')
    observations = {}
    for phase, item in zip(PHASES, receipt['phases']):
        if (not isinstance(item, dict) or set(item) != {'name', 'samples'} or item['name'] != phase
                or not isinstance(item['samples'], list) or len(item['samples']) != config['samples']):
            raise ForensicsError('browser probe receipt has invalid phase order or sample count')
        for sample in item['samples']:
            if (not isinstance(sample, dict) or sample.keys() != config['metrics'].keys()
                    or not all(_number(value, -MAX_OBSERVATION, MAX_OBSERVATION) for value in sample.values())):
                raise ForensicsError('browser probe receipt has invalid observations')
        observations[phase] = item['samples']
    return observations


def _range(values):
    low, high = min(values), max(values)
    return {'min': low, 'max': high, 'spread': high - low}


def _metric_result(rule, ranges):
    baseline = ranges['baseline']
    rounds = []
    for previous_name, treatment_name, restored_name in zip(PHASES[::2], PHASES[1::2], PHASES[2::2]):
        previous, treatment, restored = (ranges[name] for name in (previous_name, treatment_name, restored_name))
        if rule['direction'] == 'increase':
            effect = treatment['min'] - max(previous['max'], restored['max'])
        else:
            effect = min(previous['min'], restored['min']) - treatment['max']
        restore_delta = max(abs(restored[key] - baseline[key]) for key in ('min', 'max'))
        rounds.append({'phase': treatment_name, 'minimum_effect': effect, 'restore_delta': restore_delta,
                       'effect_met': effect >= rule['min_change'],
                       'restoration_met': restore_delta <= rule['restore_tolerance']})
    stable = all(bounds['spread'] <= rule['max_spread'] for bounds in ranges.values())
    supported = stable and all(item['effect_met'] and item['restoration_met'] for item in rounds)
    return {'supported': supported, 'stable': stable, 'rule': rule, 'ranges': ranges, 'rounds': rounds}


def evaluate(config, receipt_probe, trace, window):
    """Require two control-separated effects and restoration to the initial baseline."""
    observations = _samples(config, receipt_probe)
    timing = _trace_times(trace, window)
    metrics = {name: _metric_result(rule, {
        phase: _range([sample[name] for sample in observations[phase]]) for phase in PHASES
    }) for name, rule in config['metrics'].items()}
    supported = all(item['supported'] for item in metrics.values())
    return {'status': 'hypothesis-supported' if supported else 'inconclusive',
            'phases': [{'name': phase, 'samples': observations[phase], **timing['phases'][phase]} for phase in PHASES],
            'trace': {key: value for key, value in timing.items() if key != 'phases'}, 'metrics': metrics,
            'scope': 'This probe measures the specified intervention in the captured renderer and workload.'}
