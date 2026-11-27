"""Locate an exact local session and bound the catch-up by message order."""

import os
from pathlib import Path

from workbench import recall_discovery, recall_sessions
from workbench.automate_me import UUIDISH, _date, _file, _require


SESSION_ENV = {'claude': 'CLAUDE_CODE_SESSION_ID', 'codex': 'CODEX_THREAD_ID'}


def add_arguments(parser):
    parser.add_argument('--host', choices=recall_sessions.HOSTS)
    parser.add_argument('--transcript')
    parser.add_argument('--session-id')
    parser.add_argument('--invocation-line', type=int)


def _select(repo, host, transcript, session_id):
    if transcript is not None:
        _require(session_id is None, 'select a transcript or a local session ID')
        return host or 'claude', _file(transcript), None

    if host is None:
        candidates = [name for name, env in SESSION_ENV.items() if os.getenv(env)]
        _require(session_id is None and len(candidates) == 1, 'specify the transcript host or use one active host session')
        host = candidates[0]
    ident = session_id or (os.getenv(SESSION_ENV[host]) if host in SESSION_ENV else None)
    _require(ident and UUIDISH.fullmatch(ident), 'provide the exact local session ID')
    matches = recall_discovery.find_session(host, repo, ident)
    _require(len(matches) == 1, 'the selected session has no unique local transcript for this workspace')
    return host, matches[0], ident


def _command(turn):
    text = turn['text'].lstrip()
    match = recall_sessions.SLASH_COMMAND.match(text) if turn['role'] == 'user' else None
    return match[1] if match else None


def anchor(repo, *, transcript=None, host=None, session_id=None, invocation_line=None,
           command='ketchup', context_limit=None):
    repo = Path(repo).resolve(strict=True)
    source_host, path, expected = _select(repo, host, transcript, session_id)
    session, turns, _, digest = recall_sessions.transcript(path, repo, None, None, None, host=source_host,
                                                          include_commands=True)
    _require(session is not None and (expected is None or session == expected), 'session or workspace identity differs')
    names = {f'/{command}', f'/agent-workbench:{command}', f'/pstack:{command}'}
    if invocation_line is None:
        current = next((turn for turn in reversed(turns) if turn['role'] == 'user'), None)
    else:
        current = next((turn for turn in turns if turn['line'] == invocation_line), None)
    _require(current is not None and _command(current) in names,
             f'select the actual {command} invocation in the same workspace')
    preceding = [turn for turn in turns if turn['line'] < current['line']]
    last = next((turn for turn in reversed(preceding) if turn['role'] == 'user' and _command(turn) is None), None)
    _require(last is not None, 'the selected command has no preceding human message')
    inferred = any(turn.get('time_source') == 'file-mtime' for turn in (last, current))
    _require(inferred or _date(last['at']) <= _date(current['at']), 'session command predates the preceding human message')
    later = [turn for turn in preceding if turn['line'] > last['line']
             and (inferred or turn.get('time_source') == 'file-mtime' or _date(turn['at']) <= _date(current['at']))]
    return {'transcript': {'path': str(path), 'sha256': digest}, 'session': session, 'host': source_host,
            'last_user_line': last['line'], 'invocation_line': current['line'],
            'time_basis': 'message-order' if inferred else 'recorded-timestamps',
            'since': None if inferred else last['at'], 'until': None if inferred else current['at'],
            'request_context': last['text'], 'later_turn_context': later[-context_limit:] if context_limit else later}
