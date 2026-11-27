"""Read selected host transcripts with exact workspace scope and line provenance."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from workbench.automate_me import UUIDISH, _date, _digest, _file, _read, _require
from workbench.reflect import _scrub


HOSTS = ('claude', 'cursor', 'codex')
ROLES = ('user', 'assistant')
MAX_BYTES = 5_000_000
MAX_EXCERPT = 1500
CURSOR_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.I)
GENERATED = (
    '<local-command-', '<command-', '<bash-', '<task-notification', '<environment_context>',
    '<codex_internal_context', '<user_shell_command>', '# AGENTS.md instructions',
)
CREDENTIAL = re.compile(r'(?i)\b(?:gh[pousr]_|github_pat_|xox[baprs]-|sk-)[A-Za-z0-9._-]{8,}\b')
BEARER = re.compile(r'(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]{8,}={0,2}')
VOICE_INPUT = re.compile(r'<realtime_delegation>\s*<input>(.*?)</input>\s*'
                         r'<transcript_delta>.*?</transcript_delta>\s*</realtime_delegation>', re.S)


def _workspace(value, repo):
    return isinstance(value, str) and Path(value).is_absolute() and Path(value).resolve() == repo


def _rows(raw):
    for line, data in enumerate(raw.splitlines(), 1):
        if data.strip():
            row = json.loads(data)
            _require(isinstance(row, dict), 'transcript rows must be JSON objects')
            yield line, row


def _blocks(content, text_type):
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [block['text'] for block in content if isinstance(block, dict) and block.get('type') == text_type
            and isinstance(block.get('text'), str)]


def _conversation_text(message, role):
    if (not isinstance(message, dict) or message.get('role', role) != role):
        return ''
    content = message.get('content')
    if (role == 'user' and isinstance(content, list)
            and any(isinstance(block, dict) and block.get('type') == 'tool_result' for block in content)):
        return ''
    text = '\n'.join(_blocks(content, 'text'))
    return '' if text.lstrip().startswith(GENERATED) else text


def _turn(line, role, stamp, uid, text, *, inferred=False):
    # The timestamp flag is recorded only when the host does not supply one.
    fields = {'line': line, 'role': role, 'at': stamp.isoformat(), 'text': text}
    if inferred:
        fields['time_source'] = 'file-mtime'
    return uid, stamp, fields


def _claude(raw, repo):
    ident, turns = None, []
    for line, row in _rows(raw):
        role = row.get('type')
        if role not in ROLES or row.get('isMeta') or row.get('isSidechain') or not _workspace(row.get('cwd'), repo):
            continue
        session = row.get('sessionId')
        _require(isinstance(session, str) and UUIDISH.fullmatch(session) and ident in (None, session),
                 'transcript mixes session IDs or lacks its ID')
        ident = session
        message = row.get('message')
        if not isinstance(message, dict) or message.get('role') != role:
            continue
        text = _conversation_text(message, role)
        if text:
            turns.append(_turn(line, role, _date(row['timestamp']), row.get('uuid', line), text))
    return ident, turns


def _cursor_slug_is_unique(repo, slug):
    """Cursor encodes slashes and literal hyphens identically in its project directory."""
    parts = slug.split('-')
    paths = [(Path(repo.anchor), 0)]
    while paths:
        parent, first = paths.pop()
        for end in range(first + 1, len(parts) + 1):
            name = '-'.join(parts[first:end])
            if name in ('', '.', '..'):
                continue
            candidate = parent/name
            if not candidate.is_dir():
                continue
            if end < len(parts):
                paths.append((candidate, end))
            elif candidate.resolve() != repo:
                return False
    return True


def cursor_identity(path, repo):
    """Return the session UUID under the canonical Cursor workspace directory."""
    slug = repo.as_posix().lstrip('/').replace('/', '-')
    if (len(path.parents) < 5 or path.parents[1].name != 'agent-transcripts'
            or path.parents[2].name != slug or path.parents[3].name != 'projects'
            or path.parents[4].name != '.cursor'):
        return None
    _require(CURSOR_ID.fullmatch(path.stem) and path.parent.name == path.stem,
             'Cursor transcript must use its canonical session directory and filename')
    _require(_cursor_slug_is_unique(repo, slug), 'Cursor project directory is ambiguous for this workspace')
    return path.stem


def _cursor(raw, ident, repo, mtime):
    turns = []
    for line, row in _rows(raw):
        role = row.get('role')
        message = row.get('message')
        if (role not in ROLES or not isinstance(message, dict) or row.get('isMeta') or row.get('isSidechain')):
            continue
        if any(cwd is not None and not _workspace(cwd, repo) for cwd in (row.get('cwd'), message.get('cwd'))):
            continue
        session = row.get('sessionId')
        _require(session in (None, ident), 'Cursor transcript session does not match its filename')
        text = _conversation_text(message, role)
        if text:
            stamp = row.get('timestamp')
            turns.append(_turn(line, role, _date(stamp) if stamp is not None else mtime,
                               row.get('uuid', line), text, inferred=stamp is None))
    return turns


def _codex_text(message):
    role, content = message['role'], message.get('content', [])
    if role == 'assistant':
        return '\n'.join(_blocks(content, 'output_text'))
    if not isinstance(content, list):
        return ''
    item_metadata = message.get('internal_chat_message_metadata_passthrough')
    annotations = item_metadata.get('content_item_metadata') if isinstance(item_metadata, dict) else None
    if annotations is not None and (not isinstance(annotations, list) or len(annotations) != len(content)):
        return ''
    parts = []
    for i, block in enumerate(content):
        annotation = annotations[i] if annotations is not None else None
        provenance = annotation.get('provenance') if isinstance(annotation, dict) else None
        if provenance is not None and (not isinstance(provenance, dict) or provenance.get('type') != 'user'):
            continue
        for text in _blocks([block], 'input_text'):
            text = text.strip()
            if text.startswith('<realtime_delegation>'):
                voice = VOICE_INPUT.fullmatch(text)
                if voice is None:
                    continue
                text = voice[1].strip()
            if text and not text.startswith(GENERATED):
                parts.append(text)
    return '\n'.join(parts)


def codex_scope(payload, repo, current=None):
    """Check workspace and lineage from Codex's initial session metadata."""
    if not isinstance(payload, dict):
        return False
    ident, source = payload.get('id'), payload.get('source')
    return (isinstance(ident, str) and bool(UUIDISH.fullmatch(ident)) and ident != current
            and not payload.get('parent_thread_id') and payload.get('agent_path') in (None, '', '/root')
            and payload.get('thread_source') in (None, 'user', 'realtime_voice')
            and not (isinstance(source, dict) and 'subagent' in source)
            and _workspace(payload.get('cwd'), repo))


def _codex(raw, repo):
    rows = list(_rows(raw))
    metadata = next((row.get('payload') for _, row in rows if row.get('type') == 'session_meta'), None)
    _require(isinstance(metadata, dict), 'Codex transcript requires a session header')
    ident = metadata.get('id')
    _require(isinstance(ident, str) and UUIDISH.fullmatch(ident), 'Codex transcript lacks a stable session ID')
    if not codex_scope(metadata, repo):
        return None, []

    contexts = {}
    other_workspace = False
    has_header = False
    for _, row in rows:
        payload = row.get('payload')
        if row.get('type') == 'session_meta':
            _require(isinstance(payload, dict) and payload.get('id') == ident and _workspace(payload.get('cwd'), repo),
                     'Codex transcript mixes session IDs or workspaces')
            has_header = True
        if not has_header or row.get('type') != 'turn_context' or not isinstance(payload, dict):
            continue
        if 'cwd' in payload:
            matches = _workspace(payload['cwd'], repo)
            other_workspace |= not matches
            turn_id = payload.get('turn_id')
            if isinstance(turn_id, str):
                contexts[turn_id] = contexts.get(turn_id, True) and matches

    has_header, turns = False, []
    forked = bool(metadata.get('forked_from_id') or metadata.get('history_base'))
    for line, row in rows:
        if row.get('type') == 'session_meta':
            has_header = True
        if not has_header or row.get('type') != 'response_item':
            continue
        payload, envelope = row.get('payload'), row.get('metadata')
        if not isinstance(payload, dict) or payload.get('type') != 'message' or payload.get('role') not in ROLES:
            continue
        if (payload.get('phase') not in (None, 'commentary', 'final_answer')
                or payload.get('status') not in (None, 'completed')
                or (isinstance(envelope, dict) and envelope.get('inherited_user_message'))
                or (forked and not isinstance(envelope, dict))):
            continue
        detailed = payload.get('internal_chat_message_metadata_passthrough')
        turn_id = detailed.get('turn_id') if isinstance(detailed, dict) else None
        if other_workspace and (not isinstance(turn_id, str) or contexts.get(turn_id) is not True):
            continue
        text = _codex_text(payload)
        if text:
            turns.append(_turn(line, payload['role'], _date(row['timestamp']), payload.get('id') or line, text))
    return ident, turns


def transcript(path, repo, start, end, current, *, host='claude'):
    """Return the session ID, scoped visible turns, excerpt count trimmed, and raw digest."""
    _require(host in HOSTS, 'choose a supported transcript host')
    path, repo = _file(path), Path(repo).resolve(strict=True)
    _require(path.suffix == '.jsonl', 'select a JSONL transcript')
    ident = cursor_identity(path, repo) if host == 'cursor' else None
    before = path.stat()
    raw = _read(path, MAX_BYTES)
    if host == 'claude':
        ident, candidates = _claude(raw, repo)
    elif host == 'cursor':
        mtime = datetime.fromtimestamp(before.st_mtime, timezone.utc)
        candidates = _cursor(raw, ident, repo, mtime) if ident is not None else []
        after = path.stat()
        _require((before.st_size, before.st_mtime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino),
                 'Cursor transcript changed while reading its time evidence')
    else:
        ident, candidates = _codex(raw, repo)
    seen, turns, shortened = set(), [], 0
    if ident is not None and ident != current:
        for uid, stamp, row in candidates:
            if (start and stamp < start) or stamp > end or uid in seen:
                continue
            clean = BEARER.sub(r'\1[omitted]', CREDENTIAL.sub('[omitted]', _scrub(row['text'])))
            seen.add(uid)
            if clean:
                shortened += len(clean) > MAX_EXCERPT
                turns.append({**row, 'text': clean[:MAX_EXCERPT]})
    return ident, turns, shortened, _digest(raw)
