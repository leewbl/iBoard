from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from workbench import recall_sessions


START = datetime(2026, 11, 4, tzinfo=timezone.utc)
END = datetime(2026, 11, 9, tzinfo=timezone.utc)
STAMP = '2026-11-05T04:05:06Z'
CURSOR = 'f6859b01-b3fe-4c69-8162-49d97dd22486'


def text(value, kind='text'):
    return {'type': kind, 'text': value}


class RecallSessionTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root/'scope'/'project'
        self.repo.mkdir(parents=True)

    def write(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in records), encoding='utf-8')
        return path

    def parse(self, path, host, *, repo=None, start=START, current=None):
        result = recall_sessions.transcript(path, self.repo if repo is None else repo, start, END, current, host=host)
        self.assertEqual(result[-1], hashlib.sha256(path.read_bytes()).hexdigest())
        return result[:3]

    def cursor_path(self, repo):
        scope = str(repo).lstrip('/').replace('/', '-')
        return self.root/'.cursor'/'projects'/scope/'agent-transcripts'/CURSOR/f'{CURSOR}.jsonl'

    def codex(self, *records, **metadata):
        meta = {'id': 'codex-chat', 'cwd': str(self.repo), 'source': 'vscode', **metadata}
        return self.write(self.root/'codex.jsonl', [{'type': 'session_meta', 'payload': meta}, *records])

    def codex_message(self, role, blocks, *, turn_id=None, provenance=None, phase=None, envelope=None, uid=None):
        meta = {'turn_id': turn_id}
        if provenance is not None:
            meta['content_item_metadata'] = [{'provenance': {'type': kind}} for kind in provenance]
        payload = {'type': 'message', 'role': role, 'content': blocks, 'phase': phase,
                   'internal_chat_message_metadata_passthrough': meta}
        if uid is not None:
            payload['id'] = uid
        return {'type': 'response_item', 'timestamp': STAMP, 'metadata': envelope or {}, 'payload': payload}

    def test_claude_keeps_original_line_order_scope_and_text_only(self):
        base = {'cwd': str(self.repo), 'sessionId': 'claude-chat', 'type': 'user', 'timestamp': STAMP}
        row = lambda content, **kwargs: {**base, 'message': {'role': 'user', 'content': content}, **kwargs}
        log = self.write(self.root/'claude.jsonl', [
            row('Other scope', cwd=str(self.root)),
            row([text('read tool'), text('result', 'tool_result')]),
            row('old', timestamp='2026-11-03T00:00:00Z'),
            row('old meta', isMeta=True),
            row('token=raw_value ' + ('x' * 1510), uuid='message-a'),
            row('duplicate', uuid='message-a'),
            {**base, 'type': 'assistant', 'message': {'role': 'assistant',
                 'content': [text('hidden', 'thinking'), text('Recorded user-facing outcome')]}}
        ])
        sid, turns, shortened = self.parse(log, 'claude')
        self.assertEqual((sid, shortened), ('claude-chat', 1))
        self.assertEqual([row['line'] for row in turns], [5, 7])
        self.assertTrue(turns[0]['text'].startswith('token=[omitted]'))
        self.assertEqual(turns[1]['text'], 'Recorded user-facing outcome')
        self.assertEqual(self.parse(log, 'claude', current=sid)[1], [])

    def test_native_cursor_infers_time_only_when_timestamp_absent(self):
        log = self.write(self.cursor_path(self.repo), [
            {'role': 'user', 'message': {'content': [text('Selected user request')]}},
            {'role': 'assistant', 'timestamp': STAMP, 'message': {
                'content': [text('tool body', 'tool_use'), text('Recorded result')]}},
            {'type': 'turn_ended', 'status': 'completed'},
            {'role': 'assistant', 'message': {'content': [text('hidden', 'tool_use')]}},
            {'role': 'user', 'message': {'content': [text('Accidental worktree')]}, 'cwd': str(self.root)},
        ])
        os.utime(log, (START.timestamp(), START.timestamp()))
        sid, turns, shortened = self.parse(log, 'cursor')
        self.assertEqual((sid, shortened), (CURSOR, 0))
        self.assertEqual([row['line'] for row in turns], [1, 2])
        self.assertEqual((turns[0]['at'], turns[0]['time_source']), (START.isoformat(), 'file-mtime'))
        self.assertNotIn('time_source', turns[1])
        self.assertEqual(self.parse(log, 'cursor', current=CURSOR)[1], [])
        self.assertEqual(self.parse(log, 'cursor', repo=self.root)[:2], (None, []))

    def test_cursor_does_not_accept_duplicate_workspace_slugs(self):
        same_slug = self.root/'scope-project'
        same_slug.mkdir()
        log = self.write(self.cursor_path(self.repo), [{'role': 'user', 'message': {'content': [text('Content')]}}])
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.parse(log, 'cursor')
        self.assertEqual(self.parse(log, 'cursor', repo=self.root)[:2], (None, []))

    def test_codex_filters_provenance_instead_of_dropping_mixed_user_blocks(self):
        get = lambda value: text(value, 'input_text')
        log = self.codex(
            {'type': 'event_msg', 'timestamp': STAMP, 'payload': {'type': 'user_message', 'text': 'event duplicate'}},
            self.codex_message('user', [get('Synthetic prompt'), get('User guidance ghp_fakeTestCredential123')],
                               provenance=['harness', 'user']),
            self.codex_message('user', [get('prefix <environment_context> inside a direct quote'),
                                        get('Authorization: Bearer fakeBearerCredential123')]),
            self.codex_message('user', [get(' <codex_internal_context source="goal">automation')]),
            self.codex_message('user', [get('Legacy inherited')], envelope={'inherited_user_message': True}),
            self.codex_message('assistant', [text('not a visible reply', 'output_text')], phase='analysis'),
            {'type': 'response_item', 'timestamp': STAMP, 'payload': {'type': 'function_call_output', 'output': 'tool'}},
            self.codex_message('assistant', [text('Thinking', 'summary_text'), text('Visible reply', 'output_text')],
                               phase='final_answer', uid='m1'),
            self.codex_message('assistant', [text('duplicate', 'output_text')], phase='final_answer', uid='m1'),
        )
        sid, turns, shortened = self.parse(log, 'codex')
        self.assertEqual((sid, shortened), ('codex-chat', 0))
        self.assertEqual([turn['line'] for turn in turns], [3, 4, 9])
        self.assertEqual(turns[0]['text'], 'User guidance [omitted]')
        self.assertIn('Authorization: Bearer [omitted]', turns[1]['text'])
        self.assertEqual(turns[-1]['text'], 'Visible reply')

    def test_codex_project_moves_are_checked_even_when_context_follows_request(self):
        context = lambda cwd, turn: {'type': 'turn_context', 'payload': {'cwd': str(cwd), 'turn_id': turn}}
        log = self.codex(
            self.codex_message('user', [text('selected', 'input_text')], turn_id='local'),
            context(self.repo, 'local'),
            self.codex_message('user', [text('private unrelated context', 'input_text')], turn_id='away'),
            context(self.root, 'away'),
            self.codex_message('assistant', [text('cannot establish scope', 'output_text')], phase='final_answer'),
        )
        self.assertEqual([turn['text'] for turn in self.parse(log, 'codex')[1]], ['selected'])
        self.assertEqual(self.parse(log, 'codex', repo=self.root)[:2], (None, []))

    def test_codex_voice_retains_input_and_excludes_transcript_tail(self):
        retained = '<input>Check this result.</input><transcript_delta>assistant: earlier</transcript_delta>'
        envelope = lambda body: f'<realtime_delegation>{body}</realtime_delegation>'
        log = self.codex(
            self.codex_message('user', [text(envelope(retained), 'input_text')]),
            self.codex_message('user', [text(envelope('<source>transcript_tail_flush</source>' + retained),
                                              'input_text')]),
        )
        self.assertEqual([turn['text'] for turn in self.parse(log, 'codex')[1]], ['Check this result.'])

    def test_codex_agent_child_and_mixed_identity_are_not_project_history(self):
        second_header = {'type': 'session_meta', 'payload': {'id': 'parent', 'cwd': str(self.repo)}}
        log = self.codex(second_header, parent_thread_id='parent', source={'subagent': 'spawn'})
        self.assertEqual(self.parse(log, 'codex')[:2], (None, []))
        log = self.codex(second_header)
        with self.assertRaisesRegex(ValueError, 'mixes session IDs'):
            self.parse(log, 'codex')
