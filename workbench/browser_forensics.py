"""Capture and compare a live browser trace of a source-verified offline workload."""

import argparse
from collections import Counter, defaultdict
import hashlib
from itertools import chain
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import time
from urllib.parse import unquote, urlsplit

from runner.sandbox import BROWSER_MAPS_ARTIFACT, PROFILE_ARTIFACTS, SandboxError, run_sandbox
from workbench import browser_source_maps, cli, trace_forensics, workflow
from workbench.runtime_forensics import ForensicsError, SKILL, _tracked


DRIVER = SKILL / 'capture-browser.mjs'
ARTIFACT, TRACE_LIMIT = PROFILE_ARTIFACTS['.cdptrace']
SCRIPT_LIMIT = 2 << 20


def _script_observations(receipt):
    scripts_by_url = defaultdict(set)
    if receipt.get('scripts_complete') is not True:
        return {}
    scripts = receipt.get('scripts')
    if not isinstance(scripts, list) or len(scripts) > 100:
        raise ForensicsError('browser script evidence exceeds its scope')
    for script in scripts:
        url, url_sha, digest = (script[key] for key in ('url', 'url_sha256', 'sha256'))
        if (not isinstance(url, str) or not 0 < len(url) <= 4096 or not url.isprintable()
                or any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value)
                       for value in (url_sha, digest))):
            raise ForensicsError('invalid script URL or digest in the capture receipt')
        scripts_by_url[url_sha].add((url, digest))
    return scripts_by_url


def _bindings(repo, revision, port, receipt):
    """Bind only same-origin scripts whose captured bytes match a Git blob."""
    result = {}
    for url_sha, observed in _script_observations(receipt).items():
        if len(observed) != 1:
            continue
        url, expected = next(iter(observed))
        try:
            address = urlsplit(url)
        except ValueError:
            continue
        path = unquote(address.path).removeprefix('/')
        if (address.scheme != 'http' or address.netloc != f'127.0.0.1:{port}'
                or address.fragment or address.query or not _tracked(repo, revision, path)):
            continue
        ref = f'{revision}:{path}'
        if int(cli.git(repo, 'cat-file', '-s', ref, timeout=15)) > SCRIPT_LIMIT:
            continue
        raw = subprocess.check_output(['git', '-C', str(repo), 'cat-file', 'blob', ref], timeout=15)
        digest = hashlib.sha256(raw).hexdigest()
        if digest == expected:
            result[url_sha] = {'path':path, 'sha256':digest, 'lines':len(raw.splitlines())}
    return result


def _animation_times(sample):
    animations = sample.get('animations')
    if not isinstance(animations, list) or len(animations) > 80:
        raise ForensicsError('invalid animation observation')
    counts, times = Counter(), {}
    for animation in animations:
        ident, kind, stamp = (animation[key] for key in ('id', 'type', 'time'))
        if not isinstance(ident, str) or not isinstance(kind, str):
            raise ForensicsError('animation observations need string identifiers and types')
        key = ident, kind
        counts[key] += 1
        if ident and kind and type(stamp) in (int, float) and abs(stamp) <= 10**12 and math.isfinite(stamp):
            times[key] = stamp
    return {key:stamp for key, stamp in times.items() if counts[key] == 1}


def _measurement(trial, paused, expected_rate):
    receipt = trial.get('receipt', {})
    if (receipt.get('paused') is not paused or type(receipt.get('playback_rate')) not in (int, float)
            or receipt['playback_rate'] != expected_rate or not receipt.get('observed')):
        return {}
    before, after = (_animation_times(receipt['observed'][name]) for name in ('before', 'after'))
    return {key:after[key] - start for key, start in before.items() if key in after}


def _mechanism(trials):
    if 'paused' not in trials:
        return {'status':'not-run'}
    baseline, paused = (_measurement(trials[name], stopped, rate)
                        for name, stopped, rate in (('baseline', False, 1), ('paused', True, 0)))
    changed = [{'id':ident, 'type':kind, 'baseline_delta_ms':round(ms, 3), 'paused_delta_ms':round(paused[(ident, kind)], 3)}
               for (ident, kind), ms in baseline.items()
               if (ident, kind) in paused and ms >= 10 and abs(paused[(ident, kind)]) <= 1]
    return {'status':'reduced-animation-advance' if changed else 'inconclusive', 'animations':changed}


def _receipt(report, request):
    receipt = json.loads(report['stdout'])
    if (receipt.get('kind') != 'iboard-browser-trace'
            or any(receipt.get(key) != request[key] for key in ('port', 'route', 'window_ms'))
            or receipt.get('paused') is not request['paused'] or receipt.get('clicked') is not bool(request['click'])
            or receipt.get('browser_closed') is not True or receipt.get('server_stopped') is not True
            or type(receipt.get('scripts_complete')) is not bool
            or type(receipt.get('playback_rate')) not in (int, float)
            or receipt['playback_rate'] != (0 if request['paused'] else 1)):
        raise ForensicsError('browser receipt does not match the request and cleanup')
    observed = receipt.get('observed')
    if request['observe']:
        if not isinstance(observed, dict) or set(observed) != {'before', 'after'}:
            raise ForensicsError('browser receipt is missing its selected observations')
        for sample in observed.values():
            _animation_times(sample)
    elif observed is not None:
        raise ForensicsError('browser receipt contains an unrequested observation')
    return receipt


def _trace_window(trace):
    names = ('iboard:before-action', 'iboard:window-start', 'iboard:window-end')
    events = trace['traceEvents'] if isinstance(trace, dict) else trace
    marks = {name:[] for name in names}
    for event in events:
        if (isinstance(event, dict) and event.get('name') in names and event.get('ph') == 'I'
                and event.get('cat') == 'blink.user_timing'):
            marks[event['name']].append(event)
    if any(len(marks[name]) != 1 for name in names):
        raise ForensicsError('CDP trace does not identify a unique action and observation window')
    action, start, end = (marks[name][0] for name in names)
    times = [mark.get('ts') for mark in (action, start, end)]
    if (any(type(stamp) not in (int, float) or not 0 <= stamp <= 10**15 for stamp in times)
            or not times[0] <= times[1] < times[2]
            or any(type(start.get(key)) is not int or start[key] <= 0 or start[key] != end.get(key)
                   for key in ('pid', 'tid'))):
        raise ForensicsError('CDP trace window is unordered or crosses renderer threads')
    return {'action_us':times[0], 'start_us':times[1], 'end_us':times[2],
            'elapsed_ms':round((times[2] - times[1]) / 1000, 3), 'pid':start['pid'], 'tid':start['tid']}


def _label_sources(imported, bindings, window, mappings=None):
    # The page's Debugger session cannot attest scripts running on another
    # renderer thread. The importer groups by URL, so withhold the binding for
    # any URL that also occurs in other threads.
    database = Path(imported['database']['path'])
    db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    try:
        thread = (window['pid'], window['tid'])
        outside = {row[0] for row in db.execute("""
            SELECT path FROM trace_events
            WHERE path IS NOT NULL AND (pid IS NOT ? OR tid IS NOT ?)
            UNION
            SELECT frames.path
            FROM trace_profile_frames AS frames JOIN trace_profiles AS profiles USING (profile_key)
            WHERE frames.path IS NOT NULL AND (profiles.pid IS NOT ? OR profiles.tid IS NOT ?)
        """, thread + thread)}
    finally:
        db.close()
    fallback = trace_forensics._source_locator(None, None, Path('/app'))
    finding = imported['finding']
    frames = chain.from_iterable(path['frames'] for path in finding['hot_cpu_paths'])
    resolved = {}
    for item in chain(finding['duration_groups'], finding['cpu_frames'], frames):
        url, line = item['path'], item['line']
        url_sha = hashlib.sha256(url.encode()).hexdigest() if isinstance(url, str) and url not in outside else None
        bound = bindings.get(url_sha)
        generated_column = item.get('column')
        mapped = mappings.get(url_sha) if mappings is not None else None
        original = mapped[0].lookup(line, generated_column) if mapped is not None else None
        if bound is not None and type(line) is int and 0 < line <= bound['lines']:
            item.update(source_file=bound['path'], source_status='verified-git-source')
        elif original is not None:
            receipt = mapped[1]
            item.update(original, source_status='verified-git-source-map', source_map_sha256=receipt['map_sha256'])
            resolved[(url_sha, line, generated_column)] = (
                receipt['script_sha256'], receipt['map_sha256'], original['source_file'],
                original['source_line'], original['source_column'], original['source_symbol'], original['source_sha256'])
        else:
            location, status = fallback(url, line)
            item.update(source_file=location, source_status=status)
        display, status = fallback(url, 1)
        if status == 'reported-browser-location':
            item['path'] = display
        elif isinstance(url, str) and url.startswith(('data:', 'blob:', 'javascript:')):
            item['path'] = None
    if mappings is not None:
        db = sqlite3.connect(database)
        try:
            db.execute('''CREATE TABLE trace_source_locations(
                url_sha256 TEXT, generated_line INTEGER, generated_column INTEGER, script_sha256 TEXT, map_sha256 TEXT,
                source_file TEXT, source_line INTEGER, source_column INTEGER, source_symbol TEXT, source_sha256 TEXT,
                PRIMARY KEY(url_sha256, generated_line, generated_column))''')
            db.executemany('INSERT INTO trace_source_locations VALUES(?,?,?,?,?,?,?,?,?,?)',
                           ((*position, *row) for position, row in resolved.items()))
            db.commit()
        finally:
            db.close()
        imported['database']['sha256'] = cli.digest(database)
    finding['source_semantics'] = (
        'Generated locations are 1-based. Source positions are reported when Chromium script bytes match Git '
        'or a captured source map links the generated position to embedded Git-matching source. '
        'Missing columns and ambiguous thread attribution remain unresolved.')


def _trial(repo, revision, image, request, timeout, memory_mb, out):
    out.mkdir(mode=0o700)
    started = time.monotonic()
    pinned = workflow._verified_image(repo, image, min(180, timeout), commit=revision)
    remaining = timeout - (time.monotonic() - started)
    if remaining < 1:
        raise ForensicsError('source verification used the capture time budget')
    artifact = out / ARTIFACT
    maps_output = out / BROWSER_MAPS_ARTIFACT if request.get('source_maps') else None
    helper_sha = cli.digest(DRIVER)
    with TemporaryDirectory(prefix='iboard-browser-trace-') as scratch:
        stage = Path(scratch)
        shutil.copyfile(DRIVER, stage / DRIVER.name)
        if cli.digest(stage / DRIVER.name) != helper_sha:
            raise ForensicsError('browser capture helper changed while staging')
        cli.write(stage / 'browser-request.json', request)
        report = run_sandbox(stage, pinned, ['node', '/workspace/' + DRIVER.name], remaining,
                             memory_mb=memory_mb, source_root='/app', workspace_read_only=True,
                             profile_output=artifact, pids_limit=384,
                             browser_maps_output=maps_output)
    report.update(schema=cli.SCHEMA, source_verified_commit=revision, helper_sha256=helper_sha)
    report_path = out / 'sandbox.json'
    cli.write(report_path, report)
    if (report.get('exit_code') != 0 or not report.get('container_created') or not report.get('cleanup_confirmed')
            or report.get('error') or report.get('timed_out') or report.get('stdout_truncated')
            or report.get('image') != pinned or report.get('profile_path') != str(artifact)
            or not artifact.is_file() or not 0 < artifact.stat().st_size <= TRACE_LIMIT
            or not isinstance(report.get('profile_sha256'), str)):
        raise ForensicsError(f'browser capture failed; evidence saved at {report_path}')
    receipt = _receipt(report, request)
    loaded = trace_forensics._load(artifact, report['profile_sha256'])
    if loaded[0] != 'chrome':
        raise ForensicsError('the browser capture is not a Chrome trace')
    window = _trace_window(loaded[1])
    bindings = _bindings(repo, revision, request['port'], receipt)
    mappings = map_evidence = None
    if maps_output is not None:
        if (report.get('browser_maps_path') != str(maps_output)
                or receipt.get('source_maps_sha256') != report.get('browser_maps_sha256')):
            raise ForensicsError('browser capture did not collect the requested source map evidence')
        mappings, map_evidence = browser_source_maps.bind(
            repo, revision, request['port'], _script_observations(receipt), maps_output, report.get('browser_maps_sha256'))
    imported = trace_forensics._import(out / 'analysis', artifact, loaded, None, None, Path('/app'), None)
    imported.update(source_revision=revision, source_root='/app')
    _label_sources(imported, bindings, window, mappings)
    result = {'commit':revision, 'image':pinned, 'report':str(report_path), 'receipt':receipt,
              'script_bindings':bindings, 'window':window, 'trace':imported}
    if map_evidence is not None:
        result['source_maps'] = map_evidence
    return result


def capture(*, repo, revision, image, request, pause_animations, timeout, memory_mb, out):
    repo = Path(repo).expanduser().resolve(strict=True)
    out = Path(out).expanduser().resolve()
    if (out.is_relative_to(repo) or not 1 <= timeout <= 900 or not 128 <= memory_mb <= 8192
            or not 50 <= request['window_ms'] <= 30_000 or not 1024 <= request['port'] <= 65535
            or request['window_ms'] >= timeout * 1000):
        raise ForensicsError('provide an output directory outside the repo and bounded capture settings')
    route = request['route']
    if (not route.startswith('/') or route.startswith('//') or len(route) > 1024
            or '\\' in route or any(ch.isspace() or not ch.isprintable() for ch in route)):
        raise ForensicsError('browser route must be local to the selected loopback port')
    if len(request['args']) > 32 or any('\0' in arg or len(arg.encode()) > 2048 for arg in request['args']):
        raise ForensicsError('browser server arguments exceed their size or count limits')
    for selector in (request['click'], request['observe']):
        if selector is not None and (not 0 < len(selector) <= 400 or not selector.isprintable()):
            raise ForensicsError('browser selectors must be bounded single-line strings')
    if pause_animations and not request['observe']:
        raise ForensicsError('the animation probe requires an observed element')
    if type(request.get('source_maps', False)) is not bool:
        raise ForensicsError('source map collection requires a boolean request')
    if not revision or revision.startswith('-'):
        raise ForensicsError('select a Git commit or ref')
    revision = cli.sha(repo, revision, timeout=15)
    for path in (request['entry'], request['loader']) if request['loader'] else (request['entry'],):
        if Path(path).suffix not in ('.js', '.mjs', '.cjs', '.ts', '.mts', '.cts') or not _tracked(repo, revision, path):
            raise ForensicsError('browser server entry and loader must be tracked at the selected revision')

    out.mkdir(mode=0o700, parents=True)
    report_path = out / 'result.json'
    result = {'schema':cli.SCHEMA, 'commit':revision, 'request':request, 'memory_mb':memory_mb, 'trials':{}}
    cli.write(report_path, result)
    result['trials']['baseline'] = _trial(repo, revision, image, {**request, 'paused':False},
                                          timeout, memory_mb, out / 'baseline')
    cli.write(report_path, result)
    if pause_animations:
        pinned = result['trials']['baseline']['image']
        result['trials']['paused'] = _trial(repo, revision, pinned, {**request, 'paused':True},
                                            timeout, memory_mb, out / 'paused')
    result['mechanism'] = _mechanism(result['trials'])
    cli.write(report_path, result)
    return report_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--image', required=True)
    parser.add_argument('--entry', required=True)
    parser.add_argument('--loader')
    parser.add_argument('--arg', dest='args', action='append', default=[])
    parser.add_argument('--port', type=int, default=18789)
    parser.add_argument('--route', default='/')
    parser.add_argument('--click', help='Optional CSS selector of a control to click')
    parser.add_argument('--observe', help='Optional CSS selector of the element subtree to observe')
    parser.add_argument('--window-ms', type=int, default=1200)
    parser.add_argument('--pause-animations', action='store_true')
    parser.add_argument('--source-maps', action='store_true', help='Capture linked source maps from the selected loopback page')
    parser.add_argument('--timeout', type=int, default=120, help='Time limit per capture')
    parser.add_argument('--memory-mb', type=int, default=2048)
    parser.add_argument('--out', type=Path, required=True)
    options = vars(parser.parse_args(argv))
    request = {key:options.pop(key) for key in ('entry', 'loader', 'args', 'port', 'route', 'click', 'observe', 'window_ms')}
    if options.pop('source_maps'):
        request['source_maps'] = True
    try:
        print(capture(request=request, **options))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError,
            SandboxError, sqlite3.Error, subprocess.SubprocessError, workflow.WorkflowError, cli.WorkbenchError) as error:
        parser.exit(1, f'browser forensics: {error}\n')


if __name__ == '__main__':
    main()
