"""Capture and reduce Node runtime evidence in a source-verified offline Docker run."""

import argparse
from array import array
from collections import Counter, deque
from datetime import datetime, timezone
import hashlib
from itertools import accumulate
import json
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time
from urllib.parse import unquote, urlsplit

from runner.sandbox import SandboxError, run_sandbox
from workbench import cli, heap_allocations, workflow


SKILL = Path(__file__).resolve().parents[1] / 'skills' / 'runtime-forensics'
SOURCE = re.compile(r'(?:[a-zA-Z0-9_.-]+/)*[a-zA-Z0-9_.-]+')
FILES = {'cpu': ('cpu.cpuprofile', 16 << 20), 'heap': ('heap.heapsnapshot', 64 << 20)}


class ForensicsError(ValueError):
    pass


def _tracked(repo, revision, path):
    if not SOURCE.fullmatch(path) or any(part in ('.', '..') for part in path.split('/')):
        return False
    entry = cli.git(repo, 'ls-tree', revision, '--', path, timeout=15)
    return entry.startswith(('100644 blob ', '100755 blob ')) and entry.partition('\t')[2] == path


def _app_path(url):
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme == 'file' and not parsed.netloc:
        decoded = unquote(parsed.path)
        if decoded.startswith('/app/'):
            path = decoded.removeprefix('/app/')
            if SOURCE.fullmatch(path) and all(part not in ('.', '..') for part in path.split('/')):
                return path
    return None


def summarize_cpu(profile, source_lines):
    """Attribute the sampling time to leaf frames and cite tracked application code."""
    nodes, samples, deltas = (profile[key] for key in ('nodes', 'samples', 'timeDeltas'))
    if not isinstance(nodes, list) or not isinstance(samples, list) or not isinstance(deltas, list):
        raise ForensicsError('CPU profile arrays are missing')
    if not nodes or not samples or len(samples) != len(deltas):
        raise ForensicsError('CPU profile has no complete samples')
    frames = {node['id']: node['callFrame'] for node in nodes}
    if len(frames) != len(nodes) or not all(type(item) is int and item in frames for item in samples):
        raise ForensicsError('CPU samples reference unknown frame IDs')
    if not all(type(delta) is int for delta in deltas):
        raise ForensicsError('CPU profile has an invalid time axis')
    positions = sorted(zip(accumulate(deltas), samples), key=lambda sample: sample[0])
    if positions[0][0] < 0:
        raise ForensicsError('CPU sample timestamp precedes the profile start')
    durations = Counter()
    for (offset, frame_id), (next_offset, _) in zip(positions, positions[1:]):
        durations[frame_id] += next_offset - offset
    own = Counter()
    for frame_id, value in durations.items():
        frame = frames[frame_id]
        path = _app_path(frame.get('url'))
        line = frame.get('lineNumber')
        symbol = frame.get('functionName')
        if path and type(line) is int and isinstance(symbol, str) and 0 <= line < source_lines(path):
            own[(path, line + 1, symbol[:240])] += value
    total = sum(durations.values())
    return {'samples': len(samples), 'sampled_ms': round(total / 1000, 3),
            'application_sampled_ms': round(sum(own.values()) / 1000, 3),
            'hot_frames': [{'path': path, 'line': line, 'symbol': symbol or '(anonymous)',
                            'sampled_ms': round(value / 1000, 3), 'share_pct': round(100 * value / total, 2)}
                           for (path, line, symbol), value in own.most_common(15) if total and value]}


def summarize_heap(profile, class_name, *, allocations=None):
    """Describe shallow object size and an actual strong-reference path from a GC root."""
    snapshot, nodes, edges, strings = (profile[key] for key in ('snapshot', 'nodes', 'edges', 'strings'))
    meta = snapshot['meta']
    nf, ef = meta['node_fields'], meta['edge_fields']
    nn, ne = len(nf), len(ef)
    count, edge_count = snapshot['node_count'], snapshot['edge_count']
    if (type(count) is not int or not 0 < count <= 1_500_000 or type(edge_count) is not int
            or not 0 < edge_count <= 4_000_000 or nn * count != len(nodes) or ne * edge_count != len(edges)):
        raise ForensicsError('heap counts do not match the snapshot')
    ntype, name, size, degree = (nf.index(key) for key in ('type', 'name', 'self_size', 'edge_count'))
    etype, ename, target = (ef.index(key) for key in ('type', 'name_or_index', 'to_node'))
    node_types, edge_types = meta['node_types'][ntype], meta['edge_types'][etype]
    object_type = node_types.index('object')
    if node_types[nodes[ntype]] != 'synthetic':
        raise ForensicsError('heap snapshot is missing the synthetic root')
    sizes, counts = Counter(), Counter()
    offsets = array('I', [0])
    candidates = set()
    for i in range(count):
        at = i * nn
        children = nodes[at + degree]
        if type(children) is not int or not 0 <= children <= edge_count - offsets[-1]:
            raise ForensicsError('heap references have invalid ranges')
        offsets.append(offsets[-1] + children)
        if nodes[at + ntype] == object_type:
            label, shallow = strings[nodes[at + name]], nodes[at + size]
            sizes[label] += shallow
            counts[label] += 1
            if class_name is not None and label == class_name:
                candidates.add(i)
    if offsets[-1] != edge_count:
        raise ForensicsError('heap edge ranges do not cover the snapshot')
    result = {'nodes': count, 'edges': edge_count,
              'top_objects': [{'class': label[:240], 'count': counts[label], 'shallow_bytes': value}
                              for label, value in sizes.most_common(15)]}
    if class_name is None:
        if allocations is not None:
            result['allocation'] = heap_allocations.summarize(profile, allocations, None, None)
        return result

    parent = array('i', [-1]) * count
    via = array('i', [-1]) * count
    parent[0] = 0
    pending = deque([0] if candidates else [])
    found = None
    while pending:
        node = pending.popleft()
        if node in candidates:
            found = node
            break
        for edge in range(offsets[node], offsets[node + 1]):
            at = edge * ne
            if edge_types[edges[at + etype]] in ('weak', 'shortcut'):
                continue
            address = edges[at + target]
            if type(address) is not int or address < 0 or address % nn or address >= len(nodes):
                raise ForensicsError('heap edge has an invalid destination')
            child = address // nn
            if parent[child] == -1:
                parent[child], via[child] = node, edge
                pending.append(child)

    chain = []
    depth = 0
    cursor = found
    while cursor:
        depth += 1
        if depth <= 80:
            at = via[cursor] * ne
            key, kind = edges[at + ename], edge_types[edges[at + etype]]
            edge = str(key) if kind in ('element', 'hidden') else strings[key][:240]
            chain.append({'edge_kind': kind, 'edge': edge, 'node': strings[nodes[cursor * nn + name]][:240]})
        cursor = parent[cursor]
    result['selected_class'] = {'name': class_name, 'count': len(candidates), 'shallow_bytes': sizes[class_name],
                                'strong_root_path': list(reversed(chain)) if 0 < depth <= 80 else None,
                                'strong_root_path_length': depth or None}
    if allocations is not None:
        evidence = heap_allocations.summarize(profile, allocations, class_name, found)
        result['allocation'] = evidence
        evidence_status = 'allocation symbols not captured'
        if evidence['traced_objects']:
            evidence_status = ('allocation source frames recorded' if evidence['verified_source_objects']
                               else 'allocation stacks recorded; source mapping unavailable')
        result['selected_class']['allocation_source'] = evidence_status
    return result


def _trial(repo, revision, image, options, out):
    out.mkdir()
    started = time.monotonic()
    pinned = workflow._verified_image(repo, image, min(180, options['timeout']), commit=revision)
    remaining = options['timeout'] - (time.monotonic() - started)
    if remaining < 1:
        raise ForensicsError('source verification used the profile time budget')
    filename, limit = FILES[options['kind']]
    artifact = out / filename
    track = options.get('track_allocations', False)
    kind = 'heap-allocations' if track else options['kind']
    command = ['node', '/workspace/node-profile.mjs', kind, options['entry'],
               options['loader'] or '', str(options['window_ms']), *options['args']]
    with TemporaryDirectory(prefix='iboard-node-profile-') as scratch:
        for helper in ('node-profile.mjs', 'capture-node.cjs'):
            shutil.copyfile(SKILL / helper, Path(scratch) / helper)
        report = run_sandbox(scratch, pinned, command, remaining, memory_mb=options['memory_mb'],
                             source_root='/app', workspace_read_only=True, profile_output=artifact)
    report.update(schema=1, source_verified_commit=revision)
    cli.write(out / 'sandbox.json', report)
    if (report.get('exit_code') != 0 or not report.get('container_created')
            or not report.get('cleanup_confirmed') or report.get('error') or report.get('timed_out')
            or report.get('stdout_truncated') or report.get('image') != pinned
            or report.get('profile_path') != str(artifact) or not artifact.is_file()
            or artifact.stat().st_size > limit):
        raise ForensicsError(f'profile capture failed; evidence saved at {out / "sandbox.json"}')
    data = artifact.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != report.get('profile_sha256') or len(data) > limit:
        raise ForensicsError('profile changed after collection')
    receipt = json.loads(report['stdout'])
    if receipt.get('kind') != 'iboard-node-profile' or receipt.get('profile') != options['kind']:
        raise ForensicsError('profile receipt does not match the request')
    if receipt.get('allocation_tracking', False) is not track:
        raise ForensicsError('allocation tracking receipt does not match the request')
    target, capture = receipt['target'], receipt['capture']
    code, signal = target.get('code'), target.get('signal')
    if ((code is None and (not isinstance(signal, str) or not re.fullmatch(r'SIG[A-Z0-9]+', signal)))
            or (code is not None and (type(code) is not int or not 0 <= code <= 255 or signal is not None))
            or capture.get('at') not in ('exit', 'window') or type(capture.get('elapsed_ms')) is not int
            or not 0 <= capture['elapsed_ms'] <= 1000 * report['elapsed_seconds'] + 1):
        raise ForensicsError('profile receipt has an invalid target exit or capture time')
    profile = json.loads(data)
    lines = {}

    def source_lines(path):
        if path not in lines:
            ref = f'{revision}:{path}'
            lines[path] = 0
            if _tracked(repo, revision, path) and int(cli.git(repo, 'cat-file', '-s', ref, timeout=15)) <= 1_000_000:
                blob = subprocess.check_output(['git', '-C', str(repo), 'cat-file', 'blob', ref], timeout=15)
                lines[path] = len(blob.splitlines())
        return lines[path]

    def allocation_location(url, line):
        path = _app_path('file://' + url if url.startswith('/app/') else url)
        if path and line <= source_lines(path):
            return path, 'verified-git-source'
        return url, 'reported-location-unverified'

    if options['kind'] == 'cpu':
        reduced = summarize_cpu(profile, source_lines)
    else:
        graph = heap_allocations.parse(profile, allocation_location) if track else None
        if track and graph is None:
            raise ForensicsError('tracked heap snapshot has no V8 allocation trace')
        reduced = summarize_heap(profile, options['class_name'], allocations=graph)
    return {'commit': revision, 'image': pinned, 'artifact': str(artifact), 'sha256': digest,
            'report': str(out / 'sandbox.json'), 'target': target, 'capture': capture,
            'finding': reduced}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=FILES)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--image', required=True)
    parser.add_argument('--compare-revision')
    parser.add_argument('--compare-image')
    parser.add_argument('--entry', required=True)
    parser.add_argument('--loader')
    parser.add_argument('--arg', dest='args', action='append', default=[])
    parser.add_argument('--timeout', type=int, default=120, help='per-revision time budget')
    parser.add_argument('--memory-mb', type=int, default=2048)
    parser.add_argument('--window-ms', type=int, default=0)
    parser.add_argument('--class-name')
    parser.add_argument('--track-allocations', action='store_true', help='Record V8 allocation stacks with the heap snapshot')
    parser.add_argument('--out', type=Path, required=True)
    options = vars(parser.parse_args(argv))
    try:
        repo = options['repo'].resolve(strict=True)
        out = options['out'].resolve()
        kind, entry, loader = (options[key] for key in ('kind', 'entry', 'loader'))
        selected = [('baseline', options['revision'], options['image'])]
        if bool(options['compare_revision']) != bool(options['compare_image']):
            raise ForensicsError('a comparison needs both a revision and its prebuilt local image')
        if options['compare_revision']:
            selected.append(('comparison', options['compare_revision'], options['compare_image']))
        if (out.is_relative_to(repo) or not 1 <= options['timeout'] <= 900
                or not 128 <= options['memory_mb'] <= 8192 or not 0 <= options['window_ms'] <= 60_000):
            raise ForensicsError('provide an output directory outside the repo and bounded runtime settings')
        if len(options['args']) > 32 or any('\0' in arg or len(arg.encode()) > 2048 for arg in options['args']):
            raise ForensicsError('Node entry arguments exceed their size or count limits')
        name = options['class_name']
        if options['track_allocations'] and kind != 'heap':
            raise ForensicsError('only heap snapshots support allocation tracking')
        if name is not None and (kind != 'heap' or not name or len(name) > 160 or not name.isprintable()):
            raise ForensicsError('a class name selects an object class in a heap snapshot')
        revisions = []
        for role, requested, image in selected:
            if not requested or requested.startswith('-'):
                raise ForensicsError('select a Git commit or ref')
            revision = cli.sha(repo, requested, timeout=15)
            for path in (entry, loader) if loader else (entry,):
                if Path(path).suffix not in ('.js', '.cjs', '.mjs', '.ts', '.mts', '.cts') or not _tracked(repo, revision, path):
                    raise ForensicsError(f'Node entry and loader must be tracked source files at {revision}')
            revisions.append((role, revision, image))
        out.mkdir(parents=True)
        result = {'schema': 1, 'profile': kind, 'entry': entry, 'loader': loader, 'args': options['args'],
                  'window_ms': options['window_ms'], 'memory_mb': options['memory_mb'],
                  'started_at': datetime.now(timezone.utc).isoformat(), 'trials': {}}
        if options['track_allocations']:
            result['track_allocations'] = True
        for role, revision, image in revisions:
            result['trials'][role] = _trial(repo, revision, image, options, out / role)
            cli.write(out / 'result.json', result)
        print(out / 'result.json')
    except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError, AttributeError,
            SandboxError, subprocess.SubprocessError, workflow.WorkflowError, cli.WorkbenchError) as error:
        parser.exit(1, f'runtime forensics: {error}\n')


if __name__ == '__main__':
    main()
