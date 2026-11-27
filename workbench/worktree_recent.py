"""Find recent local chat references to registered worktrees without exporting transcripts."""

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from workbench import recall_discovery, recall_sessions, worktree_usage


RECENT_DAYS = 4
MAX_FILES = 2000
MAX_BYTES = 768_000_000
MAX_MATCHES = 500
CHUNK = 1024 * 1024
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.I)
AGENT = re.compile(r'agent-[A-Za-z0-9_-]{1,100}')


def _scopes(paths, home):
    claude_home = Path(os.getenv('CLAUDE_CONFIG_DIR') or home / '.claude').expanduser()
    codex_home = Path(os.getenv('CODEX_HOME') or home / '.codex').expanduser()
    result = {}
    for path in sorted(paths):
        project = Path(path)
        (claude, _), = recall_discovery._locations('claude', project, home)
        (cursor, pattern), = recall_discovery._locations('cursor', project, home)
        folder = claude_home / 'projects' / claude.name
        result[('claude', folder, '*.jsonl')] = None
        result[('claude', folder, '*/subagents/agent-*.jsonl')] = None
        result[('cursor', cursor, pattern)] = project
    result[('codex', codex_home / 'sessions', '*/*/*/rollout-*.jsonl')] = None
    result[('codex', codex_home / 'archived_sessions', 'rollout-*.jsonl')] = None
    return result


def _search_paths(paths):
    spellings = {}
    for path in paths:
        for ascii_only in (False, True):
            # Search both JSON paths and paths nested in a JSON-encoded tool call.
            once = json.dumps(path, ensure_ascii=ascii_only)[1:-1]
            twice = json.dumps(once, ensure_ascii=ascii_only)[1:-1]
            for spelling in (once, twice):
                spellings[spelling.encode()] = path
    if not spellings:
        return None, {}, 0
    ordered = sorted(spellings, key=len, reverse=True)
    pattern = re.compile(br'(' + b'|'.join(map(re.escape, ordered)) + br''')(?=/|\\{0,3}["'])''')
    return pattern, spellings, len(ordered[0]) + 6


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


def _codex_scope(stream, paths):
    raw = stream.readline(recall_discovery.HEADER_LIMIT + 1)
    if len(raw) > recall_discovery.HEADER_LIMIT:
        raise ValueError('Codex session header size limit')
    header = json.loads(raw)
    if not isinstance(header, dict) or header.get('type') != 'session_meta':
        raise ValueError('Codex session header is unavailable')
    row = header.get('payload')
    if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not UUID.fullmatch(row['id']):
        raise ValueError('invalid Codex session identity')
    return row['id'], worktree_usage._checkout(row.get('cwd'), paths)


def _references(stream, size, matcher, known):
    pattern, spellings, overlap = matcher
    if pattern is None:
        return set()
    stream.seek(0)
    last = b''
    consumed = 0
    found = set()
    while consumed < size and found != known:
        chunk = stream.read(min(CHUNK, size - consumed))
        if not chunk:
            raise ValueError('transcript shortened during the scan')
        base = consumed - len(last)
        consumed += len(chunk)
        data = last + chunk
        for match in pattern.finditer(data):
            start = match.start()
            if not start and base:
                continue  # The next overlap contains every match with a visible prefix boundary.
            if start and data[start - 1] in b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./-':
                continue
            found.add(spellings[match[1]])
            if found == known:
                break
        last = data[-overlap:]
    return found


def _session(file, host, project, paths, selected, matcher, remaining):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(file, flags), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('session record is not a regular file')
        direct = None
        if host == 'codex':
            ident, direct = _codex_scope(stream, paths)
            if direct is None:
                return None, {}, 0  # Leave other projects' transcript bodies unread.
        elif host == 'cursor':
            ident = recall_sessions.cursor_identity(file, project)
            if ident is None:
                raise ValueError('Cursor transcript has no unambiguous workspace')
            direct = str(project)
        else:
            ident = file.stem
            if not (UUID.fullmatch(ident) or AGENT.fullmatch(ident)):
                raise ValueError('invalid Claude session identity')
        if before.st_size > remaining:
            raise OverflowError('recent transcript byte limit')
        references = _references(stream, before.st_size, matcher, selected)
        matches = dict.fromkeys(references, 'path-reference')
        if direct in selected:
            matches[direct] = 'workspace'
        if _fingerprint(before) != _fingerprint(file.stat()):
            raise ValueError('transcript changed during the scan')
    record = {'host': host, 'session_id': ident, 'last_modified_ns': before.st_mtime_ns,
              'last_modified_at': datetime.fromtimestamp(before.st_mtime, timezone.utc).isoformat(),
              'source_key': hashlib.sha256(os.fsencode(file)).hexdigest(),
              'source_inode': before.st_ino, 'source_bytes': before.st_size}
    return record, matches, before.st_size


def scan(paths, *, home=None, now=None):
    """Return sightings for linked checkouts and a bounded coverage receipt."""
    primary, *linked = tuple(paths)
    paths = {primary, *linked}
    selected = set(linked)
    matcher = _search_paths(selected)
    cutoff = (time.time() if now is None else now) - RECENT_DAYS * 86400
    home = Path.home() if home is None else Path(home)
    seen, evidence = set(), defaultdict(list)
    scopes, bytes_read, hits, skipped = 0, 0, 0, 0
    limit = None
    locations = _scopes(paths, home) if selected else {}
    for (host, root, glob), project in locations.items():
        try:
            if not stat.S_ISDIR(root.stat().st_mode):
                skipped += 1
                continue
        except FileNotFoundError:
            continue
        except OSError:
            skipped += 1
            continue
        scopes += 1
        for file in sorted(root.glob(glob)):
            try:
                if file.stat().st_mtime < cutoff:
                    continue
                if file in seen:
                    continue
                seen.add(file)
                if len(seen) > MAX_FILES:
                    raise OverflowError('recent transcript file limit')
                if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
                    raise ValueError('indirect transcript path')
                record, references, used = _session(file, host, project, paths, selected, matcher, MAX_BYTES - bytes_read)
                bytes_read += used
                if hits + len(references) > MAX_MATCHES:
                    raise OverflowError('recent transcript match limit')
                for path, access in references.items():
                    evidence[path].append({**record, 'access': access})
                hits += len(references)
            except (OSError, ValueError):
                skipped += 1
            except OverflowError as error:
                limit = str(error)
                break
        if limit:
            break
    result = {path: sorted(items, key=lambda row: (-row['last_modified_ns'], row['host'], row['source_key']))
              for path, items in evidence.items()}
    return {'status': 'partial' if skipped or limit else 'available', 'days': RECENT_DAYS,
            'scopes': scopes, 'files_considered': len(seen), 'bytes_considered': bytes_read,
            'unreadable_files_or_roots': skipped, 'limit': limit}, result
