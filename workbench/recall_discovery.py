"""Discover bounded native transcript sources for one local workspace."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from workbench import recall_sessions


MAX_FILES = 1200
MAX_TRANSCRIPT_BYTES = recall_sessions.MAX_BYTES
MAX_PROJECT_BYTES = 45_000_000
HEADER_LIMIT = 65536


def _locations(host, repo, home):
    slug = repo.as_posix().lstrip('/').replace('/', '-')
    if host == 'claude':
        path = home/'.claude/projects'/re.sub(r'[^A-Za-z0-9-]', '-', repo.as_posix())
        return [(path, '*.jsonl')]
    if host == 'cursor':
        return [(home/'.cursor/projects'/slug/'agent-transcripts', '*/*.jsonl')]
    if host == 'codex':
        return [(home/'.codex/sessions', '*/*/*/rollout-*.jsonl'),
                (home/'.codex/archived_sessions', 'rollout-*.jsonl')]
    raise ValueError('unknown recall transcript host')


def _codex_project(file, repo, current):
    # Read only the first metadata row before looking at any conversation.
    try:
        with file.open('rb') as stream:
            line = stream.readline(HEADER_LIMIT + 1)
        if len(line) > HEADER_LIMIT:
            return False
        header = json.loads(line)
        if not isinstance(header, dict) or header.get('type') != 'session_meta':
            return False
        return recall_sessions.codex_scope(header.get('payload'), repo, current)
    except (OSError, ValueError):
        return False


def find_session(host, repo, ident):
    """Locate only the named session. The caller verifies contents and workspace."""
    if host not in recall_sessions.HOSTS or not recall_sessions.UUIDISH.fullmatch(ident):
        raise ValueError('invalid host or session ID')
    home = Path.home()
    if host == 'claude':
        project, _ = _locations(host, repo, home)[0]
        root = Path(os.getenv('CLAUDE_CONFIG_DIR') or home/'.claude').expanduser()/'projects'/project.name
        paths = root.glob(f'{ident}.jsonl')
    elif host == 'cursor':
        root, _ = _locations(host, repo, home)[0]
        paths = root.glob(f'{ident}/{ident}.jsonl')
    else:
        root = Path(os.getenv('CODEX_HOME') or home/'.codex').expanduser()/'sessions'
        paths = root.glob(f'*/*/*/rollout-*-{ident}.jsonl')
    return [path for path in paths if path.is_file() and not path.is_symlink()
            and path.resolve().is_relative_to(root.resolve())]


def discover(host, repo, start, current, *, home=None):
    """Return candidate paths and audit counts, keeping other project content unread."""
    home = Path.home() if home is None else Path(home)
    found, skipped = [], []
    scanned, selected_bytes, roots = 0, 0, 0
    for location, pattern in _locations(host, repo, home):
        if not location.is_dir() or location.is_symlink():
            continue
        roots += 1
        scope = location.resolve()
        for file in scope.glob(pattern):
            scanned += 1
            if scanned > MAX_FILES:
                raise ValueError(f'{host} discovery exceeded {MAX_FILES} paths; select transcript files explicitly')
            if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(scope):
                continue
            info = file.stat()
            if current and file.stem == current:
                continue
            if start and datetime.fromtimestamp(info.st_mtime, timezone.utc) < start:
                continue
            if host == 'codex' and not _codex_project(file, repo, current):
                continue
            if host == 'cursor':
                try:
                    if recall_sessions.cursor_identity(file, repo) is None:
                        continue
                except ValueError:
                    skipped.append({'host': host, 'file': str(file), 'reason': 'Cursor project or session path is ambiguous'})
                    continue
            if info.st_size > MAX_TRANSCRIPT_BYTES:
                skipped.append({'host': host, 'file': str(file), 'reason': 'indexed transcript exceeds the byte limit'})
                continue
            selected_bytes += info.st_size
            if selected_bytes > MAX_PROJECT_BYTES:
                raise ValueError(f'{host} discovery exceeded the project search size; narrow the time window')
            found.append(file)
    return found, skipped, {'host': host, 'roots_found': roots, 'paths_scanned': scanned, 'candidate_files': len(found)}
