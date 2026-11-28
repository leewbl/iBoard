"""Bind one Cursor Editor turn and its permitted hooks to blind-eval evidence."""

from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from workbench.recall_sessions import CURSOR_ID, cursor_identity


MAX_TRANSCRIPT = 5_000_000
MAX_HOOK_LOG = 256_000
MAX_CALLS = 40
SHA256 = re.compile(r'[0-9a-f]{64}')
IDENT = re.compile(r'[A-Za-z0-9_./:-]{1,160}')
MODELS = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._:/+\-]{0,127}')
TOOLS = {'Read': 'Read', 'Write': 'Write', 'StrReplace': 'Write', 'Edit': 'Write'}
COMMON = {'event', 'session', 'generation', 'model'}


class TraceError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise TraceError(reason)


def _absolute(value):
    path = Path(value).expanduser()
    _require(path.is_absolute() and '..' not in path.parts, 'an exact absolute Cursor evidence path is required')
    return path


@contextmanager
def _descriptor(path, *, directory=False):
    """Open only through real directories, even if a parent is replaced during the walk."""
    flags = os.O_RDONLY | os.O_NOFOLLOW
    with ExitStack() as cleanup:
        parent = os.open(path.anchor, flags | os.O_DIRECTORY)
        cleanup.callback(os.close, parent)
        for part in path.parts[1:-1]:
            parent = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            cleanup.callback(os.close, parent)
        handle = os.open(path.name, flags | (os.O_DIRECTORY if directory else os.O_NONBLOCK), dir_fd=parent)
        cleanup.callback(os.close, handle)
        yield handle


def _stamp(handle):
    observed = os.fstat(handle)
    _require(stat.S_ISREG(observed.st_mode), 'Cursor evidence must be a regular file')
    return (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns)


def _snapshot(path, limit):
    with _descriptor(path) as handle:
        before = _stamp(handle)
        _require(0 < before[2] <= limit, 'Cursor evidence is empty or exceeds its size limit')
        with os.fdopen(os.dup(handle), 'rb') as stream:
            raw = stream.read(limit + 1)
        _require(len(raw) == before[2] and _stamp(handle) == before, 'Cursor evidence changed during the read')
        return raw, before


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _object(pairs):
    result = dict(pairs)
    _require(len(result) == len(pairs), 'Cursor evidence contains conflicting JSON keys')
    return result


def _rows(raw, limit):
    lines = raw.splitlines()
    _require(0 < len(lines) <= limit, 'Cursor evidence has too many events')
    for line in lines:
        try:
            row = json.loads(line.decode('utf-8'), object_pairs_hook=_object)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise TraceError('Cursor evidence contains an invalid event') from error
        _require(isinstance(row, dict), 'Cursor event must be a JSON object')
        yield row


def _path(raw, workspace, declared):
    _require(isinstance(raw, str) and raw.isprintable() and len(raw) <= 4096,
             'Cursor tool path is missing or invalid')
    path = Path(raw)
    if path.is_absolute():
        _require(path.is_relative_to(workspace), 'Cursor tool path is outside the selected workspace')
        path = path.relative_to(workspace)
    name = path.as_posix()
    _require('..' not in path.parts and name in declared, 'Cursor tool path is outside the approved file scope')
    return name


def _hook_events(raw, workspace, model, request_sha256, files, editable):
    rows = list(_rows(raw, MAX_CALLS * 2 + 2))
    first, last = rows[0], rows[-1]
    session, generation = first.get('session'), first.get('generation')
    _require(set(first) == COMMON | {'request_sha256'} and first.get('event') == 'submit'
             and first.get('request_sha256') == request_sha256,
             'Cursor submit does not match the frozen request')
    _require(isinstance(session, str) and CURSOR_ID.fullmatch(session)
             and isinstance(generation, str) and IDENT.fullmatch(generation),
             'Cursor submit lacks its exact session or generation')
    _require(all((row.get('session'), row.get('generation'), row.get('model')) == (session, generation, model)
                 for row in rows), 'Cursor hook log mixes sessions, generations, or models')
    hashes = last.get('files')
    _require(set(last) == COMMON | {'status', 'files'} and last.get('event') == 'stop'
             and last.get('status') == 'completed' and isinstance(hashes, dict) and set(hashes) == files
             and all(isinstance(value, str) and SHA256.fullmatch(value) for value in hashes.values()),
             'Cursor hook log lacks a completed stop with all approved file hashes')

    calls, results, writes, trace = {}, {}, {}, []
    for row in rows[1:-1]:
        event, ident, tool = row.get('event'), row.get('id'), row.get('tool')
        _require(event in ('start', 'result') and isinstance(ident, str) and IDENT.fullmatch(ident)
                 and isinstance(tool, str) and tool in ('Read', 'Write'), 'Cursor hook event is unsupported')
        name = _path(row.get('path'), workspace, editable if tool == 'Write' else files)
        call = (tool, name)
        if event == 'start':
            _require(set(row) == COMMON | {'id', 'tool', 'path'} and ident not in calls,
                     'Cursor hook log repeats or corrupts a tool start')
            calls[ident] = call
            trace.append({'type': 'tool_use', 'id': ident, 'tool': tool, 'path': name})
        else:
            recorded_write = row.get('ok') is True and tool == 'Write'
            fields = COMMON | {'id', 'tool', 'path', 'ok'}
            if recorded_write:
                fields.add('sha256')
                value = row.get('sha256')
                _require(isinstance(value, str) and SHA256.fullmatch(value), 'Cursor write lacks its file digest')
                writes[name] = value
            _require(set(row) == fields and type(row.get('ok')) is bool
                     and calls.get(ident) == call and ident not in results,
                     'Cursor hook result does not match a permitted tool start')
            results[ident] = row['ok']
            trace.append({'type': 'tool_result', 'id': ident, 'error': not row['ok']})
    _require(set(calls) == set(results) and any(results.values()),
             'Cursor hook log has pending calls or lacks a successful permitted tool')
    _require(all(hashes[name] == value for name, value in writes.items()),
             'Cursor specimen changed after a successful write before turn completion')
    return session, generation, hashes, calls, results, trace


def _prompt_matches(text, request_sha256):
    wrapped = '<user_query>\n', '\n</user_query>'
    if _digest(text.encode()) == request_sha256:
        return True
    return (text.startswith(wrapped[0]) and text.endswith(wrapped[1])
            and _digest(text[len(wrapped[0]):-len(wrapped[1])].encode()) == request_sha256)


def _transcript(raw, workspace, session, request_sha256, files, calls, results):
    seen, returned = {}, set()
    submitted = False
    for row in _rows(raw, 2000):
        role, message = row.get('role'), row.get('message')
        _require(role in ('user', 'assistant', 'tool') and isinstance(message, dict)
                 and message.get('role', role) == role and not row.get('isMeta') and not row.get('isSidechain'),
                 'Cursor transcript includes an unsupported message')
        for envelope in (row, message):
            _require(envelope.get('sessionId', session) == session, 'Cursor transcript contains another session')
            _require(envelope.get('cwd', str(workspace)) == str(workspace),
                     'Cursor transcript contains another workspace')
        blocks = message.get('content')
        if isinstance(blocks, str):
            blocks = [{'type': 'text', 'text': blocks}]
        _require(isinstance(blocks, list) and all(isinstance(block, dict) for block in blocks),
                 'Cursor transcript message has invalid content')
        for block in blocks:
            kind = block.get('type')
            if kind == 'text' and role == 'user':
                text = block.get('text')
                _require(not submitted and isinstance(text, str) and _prompt_matches(text, request_sha256),
                         'Cursor transcript does not contain exactly one frozen user prompt')
                submitted = True
            elif kind == 'tool_use' and role == 'assistant':
                ident, tool, inputs = block.get('id'), block.get('name'), block.get('input')
                _require(submitted and isinstance(ident, str) and ident not in seen and ident in calls
                         and isinstance(tool, str) and tool in TOOLS and isinstance(inputs, dict),
                         'Cursor transcript contains an unsupported or unobserved tool call')
                paths = {_path(value, workspace, files) for key, value in inputs.items()
                         if key in ('path', 'file_path', 'filePath')}
                _require(len(paths) == 1 and calls[ident] == (TOOLS[tool], next(iter(paths))),
                         'Cursor transcript tool call disagrees with its permitted hook')
                seen[ident] = calls[ident]
            elif kind == 'tool_result' and role in ('user', 'tool'):
                ident = block.get('tool_use_id')
                error = block.get('is_error')
                _require(submitted and isinstance(ident, str) and ident in seen and ident not in returned
                         and (error is None or type(error) is bool and error is not results[ident]),
                         'Cursor transcript tool result disagrees with its permitted hook')
                returned.add(ident)
            else:
                _require(submitted and role == 'assistant' and kind in ('text', 'thinking', 'redacted_thinking'),
                         'Cursor transcript includes an unsupported content block')
    _require(submitted and seen == calls, 'Cursor transcript lacks the frozen prompt or a permitted tool call')


def capture(log, workspace, *, requested_model, request_sha256, files, editable, cursor_home=None):
    """Read an exact workspace transcript, retain digests, and return normalized tool receipts."""
    workspace, log = _absolute(workspace), _absolute(log)
    home = _absolute(Path.home() / '.cursor' if cursor_home is None else cursor_home)
    approved, writable = set(files), set(editable)
    _require(0 < len(approved) <= 24 and 0 < len(writable) <= len(approved) and writable <= approved
             and all(isinstance(name, str) and name.isprintable() and 0 < len(name) <= 300
                     and not Path(name).is_absolute() and Path(name).as_posix() == name
                     and name != '.' and '..' not in Path(name).parts for name in approved),
             'Cursor trace needs a bounded exact project file scope')
    _require(isinstance(request_sha256, str) and SHA256.fullmatch(request_sha256)
             and isinstance(requested_model, str) and MODELS.fullmatch(requested_model),
             'Cursor trace needs a frozen request and exact model')
    try:
        with _descriptor(workspace, directory=True):
            pass
        hook_raw, hook_stamp = _snapshot(log, MAX_HOOK_LOG)
        session, generation, hashes, calls, results, trace = _hook_events(
            hook_raw, workspace, requested_model, request_sha256, approved, writable)
        slug = workspace.as_posix().lstrip('/').replace('/', '-')
        transcript = home / 'projects' / slug / 'agent-transcripts' / session / f'{session}.jsonl'
        _require(cursor_identity(transcript, workspace) == session, 'Cursor transcript is outside the selected workspace')
        raw, transcript_stamp = _snapshot(transcript, MAX_TRANSCRIPT)
        _transcript(raw, workspace, session, request_sha256, approved, calls, results)
        for path, expected in ((log, hook_stamp), (transcript, transcript_stamp)):
            with _descriptor(path) as handle:
                _require(_stamp(handle) == expected, 'Cursor evidence changed before completion')
    except OSError as error:
        raise TraceError('Cursor evidence is missing or uses an indirect path') from error
    return {'session': session, 'generation': generation, 'model': requested_model, 'files': hashes,
            'hook_sha256': _digest(hook_raw), 'transcript_sha256': _digest(raw), 'trace': trace}
