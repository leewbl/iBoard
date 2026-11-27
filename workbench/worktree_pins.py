"""Protect worktrees referenced by a local Codex desktop host's pinned chats."""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat

from workbench import cli, worktree_recent, worktree_usage
from workbench.automate_me import _read


SOURCE = 'codex-app:list_threads'
HOST_ID = re.compile(r'[A-Za-z0-9_.:-]{1,160}')
MAX_PINS = 300
MAX_FILES = 8000


class PinError(ValueError):
    pass


def _host(value):
    if not isinstance(value, str) or not HOST_ID.fullmatch(value):
        raise PinError('use the exact local Codex host ID')
    return value


def _thread(row):
    ident, directory = row.get('id'), row.get('cwd')
    if not isinstance(ident, str) or not worktree_recent.UUID.fullmatch(ident):
        raise PinError('pinned Codex chat has no native session ID')
    if directory is not None and (not isinstance(directory, str) or len(directory) > 4000 or
                                  not directory.isprintable() or not Path(directory).is_absolute()):
        raise PinError('pinned Codex chat has an invalid workspace path')
    return {'id': ident, 'cwd': directory}


def capture(repo, snapshot, host_id, out):
    from workbench.worktree_cleanup import _absolute, _records, _summary_path

    root = _absolute(repo).resolve(strict=True)
    rows = _records(root)
    host_id = _host(host_id)
    raw = json.loads(_read(snapshot, 1_000_000))
    if (not isinstance(raw, dict) or type(raw.get('schemaVersion')) is not int or
            not all(isinstance(raw.get(key), list) for key in ('pinnedThreads', 'unavailableHosts', 'unavailableSources'))
            or len(raw['pinnedThreads']) > MAX_PINS):
        raise PinError('use a complete Codex desktop pinnedThreads result')
    observed_at = raw.get('observedAt')
    stamp(observed_at)
    paths = {record['path'] for record in rows}
    threads = []
    for row in raw['pinnedThreads']:
        if not isinstance(row, dict) or not isinstance(row.get('kind'), str):
            raise PinError('invalid pinned chat summary')
        if row['kind'] == 'codex' and _host(row.get('hostId')) == host_id:
            value = _thread(row)
            value['cwd'] = worktree_usage._checkout(value['cwd'], paths)
            threads.append(value)
    if len({row['id'] for row in threads}) != len(threads):
        raise PinError('duplicate pinned Codex chat')
    status = 'partial' if raw['unavailableHosts'] or raw['unavailableSources'] else 'available'
    artifact = {'schema': cli.SCHEMA, 'source': SOURCE, 'repo': rows[0]['path'], 'host_id': host_id,
                'captured_at': observed_at, 'status': status, 'threads': threads}
    path = _summary_path(out, rows)
    if path.exists() or path.is_symlink():
        raise PinError('choose a new path for the host observation')
    cli.write(path, artifact)
    return {'snapshot': str(path), 'sha256': cli.digest(path), 'status': status, 'pinned': len(threads)}


def stamp(value):
    if not isinstance(value, str):
        raise PinError('Codex pin observation has no capture time')
    try:
        result = datetime.fromisoformat(value.removesuffix('Z') + '+00:00' if value.endswith('Z') else value)
    except ValueError as error:
        raise PinError('Codex pin observation has an invalid capture time') from error
    if result.tzinfo is None or result > datetime.now(timezone.utc):
        raise PinError('Codex pin observation requires a recorded past capture time')
    return result


def load(path, primary):
    raw = _read(path, 256_000)
    data = json.loads(raw)
    fields = {'schema', 'source', 'repo', 'host_id', 'captured_at', 'status', 'threads'}
    if (not isinstance(data, dict) or set(data) != fields or data['schema'] != cli.SCHEMA or
            data['source'] != SOURCE or data['repo'] != primary or data['status'] not in ('available', 'partial') or
            not isinstance(data['threads'], list) or len(data['threads']) > MAX_PINS):
        raise PinError('Codex pin observation is invalid or selects a different repository')
    _host(data['host_id'])
    stamp(data['captured_at'])
    if any(not isinstance(row, dict) or set(row) != {'id', 'cwd'} for row in data['threads']):
        raise PinError('invalid pinned Codex chat identifiers')
    threads = [_thread(row) for row in data['threads']]
    if len({row['id'] for row in threads}) != len(threads):
        raise PinError('duplicate pinned Codex chat')
    return {**data, 'sha256': sha256(raw).hexdigest()}


def _open(path):
    return os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb')


def _parent(row):
    value = row.get('parent_thread_id')
    source = row.get('source')
    subagent = source.get('subagent') if isinstance(source, dict) else None
    spawn = subagent.get('thread_spawn') if isinstance(subagent, dict) else None
    nested = spawn.get('parent_thread_id') if isinstance(spawn, dict) else None
    if value is not None and nested is not None and value != nested:
        raise PinError('ambiguous Codex child session parent')
    parent = value if value is not None else nested
    if parent is not None and (not isinstance(parent, str) or not worktree_recent.UUID.fullmatch(parent)):
        raise PinError('invalid Codex child session parent')
    return parent


def _catalog(home):
    base = Path(os.getenv('CODEX_HOME') or home / '.codex').expanduser()
    scanned, unreadable, seen = 0, 0, {}
    locations = ((base / 'sessions', '*/*/*/rollout-*.jsonl'), (base / 'archived_sessions', 'rollout-*.jsonl'))
    for root, pattern in locations:
        for file in root.glob(pattern):
            scanned += 1
            if scanned > MAX_FILES:
                return seen, scanned, unreadable + 1
            try:
                if not file.resolve().is_relative_to(root.resolve()):
                    raise PinError('indirect Codex transcript path')
                with _open(file) as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise PinError('Codex transcript is not a regular file')
                    record = worktree_recent._codex_metadata(source)
                ident = record['id']
                if ident in seen:
                    raise PinError('duplicate Codex transcript identity')
                seen[ident] = (file, _parent(record), record.get('cwd'))
            except (OSError, ValueError):
                unreadable += 1
    return seen, scanned, unreadable


def scan(paths, snapshot, *, home=None):
    known = set(paths)
    linked = known - {paths[0]}
    pinned = {row['id']: row['cwd'] for row in snapshot['threads']}
    summary = {'status': snapshot['status'], 'host_id': snapshot['host_id'], 'captured_at': snapshot['captured_at'],
               'snapshot_sha256': snapshot['sha256'], 'pinned': len(pinned)}
    if not pinned or not linked:
        return summary, {}
    indexed, scanned, gaps = _catalog(Path.home() if home is None else Path(home))
    children = defaultdict(list)
    for ident, (_, parent, _) in indexed.items():
        if parent:
            children[parent].append(ident)
    evidence = defaultdict(set)
    queue = [(ident, ident) for ident in pinned]
    traversed, total, hits = set(), 0, 0
    matcher = worktree_recent._search_paths(linked)
    while queue:
        root, ident = queue.pop()
        if (root, ident) in traversed:
            continue
        if len(traversed) >= MAX_FILES or hits >= worktree_recent.MAX_MATCHES:
            gaps += 1
            break
        traversed.add((root, ident))
        queue.extend((root, child) for child in children.get(ident, ()))
        direct = worktree_usage._checkout(pinned.get(ident), linked)
        if ident not in indexed:
            gaps += 1
            if direct:
                evidence[direct].add((root, ident, 'workspace'))
                hits += 1
            continue
        file, parent, cwd = indexed[ident]
        direct = direct or worktree_usage._checkout(cwd, linked)
        if direct:
            evidence[direct].add((root, ident, 'workspace'))
            hits += 1
        try:
            with _open(file) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise PinError('pinned Codex transcript is not a regular file')
                fresh = worktree_recent._codex_metadata(stream)
                if (fresh['id'], _parent(fresh), fresh.get('cwd')) != (ident, parent, cwd):
                    raise PinError('pinned Codex transcript lineage changed during scan')
                total += info.st_size
                if total > worktree_recent.MAX_BYTES:
                    gaps += 1
                    break
                matching = worktree_recent._references(stream, info.st_size, matcher, linked)
                if worktree_recent._fingerprint(info) != worktree_recent._fingerprint(file.stat()):
                    raise PinError('pinned Codex transcript changed during scan')
            references = matching - {direct}
            if hits + len(references) > worktree_recent.MAX_MATCHES:
                gaps += 1
                break
            for tree in references:
                evidence[tree].add((root, ident, 'path-reference'))
            hits += len(references)
        except (OSError, ValueError):
            gaps += 1
    matches = {tree: [{'thread_id': root, 'session_id': ident, 'access': access}
                      for root, ident, access in sorted(references)] for tree, references in evidence.items()}
    return {**summary, 'status': 'partial' if gaps else summary['status'],
            'headers_read': scanned, 'family_sessions_checked': len(traversed), 'coverage_gaps': gaps}, matches


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('repo', 'snapshot', 'host-id', 'out'):
        parser.add_argument('--' + name, required=True)
    try:
        result = capture(**vars(parser.parse_args(argv)))
    except (OSError, ValueError, cli.WorkbenchError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result['status'] != 'available')


if __name__ == '__main__':
    raise SystemExit(main())
