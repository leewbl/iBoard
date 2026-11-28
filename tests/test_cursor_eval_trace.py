import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import cursor_eval_trace


SESSION = '0212db55-5ab2-405e-9b88-9f0b624da086'
OTHER = '0bc9e23e-6f0b-410f-aef2-e73c2e2379d7'
MODEL = 'gpt-6.1-sol'
REQUEST = 'Read the guide and complete the output.\nKeep its public function name.'
FILES = {'guide.md': 'b' * 64, 'src/app.mjs': 'c' * 64}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def jsonl(path, rows):
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')


class CursorEvalTraceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.work = self.root / 'workspace'
        self.work.mkdir()
        self.home = self.root / '.cursor'
        self.log = self.root / 'hooks.jsonl'
        slug = self.work.as_posix().lstrip('/').replace('/', '-')
        self.transcript = self.home / 'projects' / slug / 'agent-transcripts' / SESSION / f'{SESSION}.jsonl'
        self.transcript.parent.mkdir(parents=True)
        self.request_digest = digest(REQUEST.encode())
        self.rows = [
            {'role': 'user', 'message': {'content': [{'type': 'text', 'text': f'<user_query>\n{REQUEST}\n</user_query>'}]}},
            {'role': 'assistant', 'message': {'content': [
                {'type': 'thinking', 'thinking': 'private signal'},
                {'type': 'tool_use', 'id': 'read-1', 'name': 'Read', 'input': {'path': str(self.work / 'guide.md')}},
                {'type': 'tool_use', 'id': 'write-1', 'name': 'StrReplace', 'input': {
                    'filePath': str(self.work / 'src/app.mjs'), 'contents': 'private source bytes'}},
                {'type': 'text', 'text': 'The app is done, claimed without using an additional tool.'},
            ]}},
        ]
        base = {'session': SESSION, 'generation': 'generation-1', 'model': MODEL}
        read = {'id': 'read-1', 'tool': 'Read', 'path': 'guide.md'}
        write = {'id': 'write-1', 'tool': 'Write', 'path': 'src/app.mjs'}
        self.events = [{**base, 'event': 'submit', 'request_sha256': self.request_digest},
                       {**base, **read, 'event': 'start'},
                       {**base, **write, 'event': 'start'},
                       {**base, **read, 'event': 'result', 'ok': True},
                       {**base, **write, 'event': 'result', 'ok': True, 'sha256': FILES['src/app.mjs']},
                       {**base, 'event': 'stop', 'status': 'completed', 'files': dict(FILES)}]

    def capture(self):
        return cursor_eval_trace.capture(self.log, self.work, requested_model=MODEL, request_sha256=self.request_digest,
                                         files=list(FILES), editable=['src/app.mjs'], cursor_home=self.home)

    def write(self):
        jsonl(self.transcript, self.rows)
        jsonl(self.log, self.events)

    def test_binds_scoped_raw_calls_to_hooks_without_retaining_raw_content(self):
        self.write()
        receipt = self.capture()
        self.assertEqual(receipt, {
            'session': SESSION, 'generation': 'generation-1', 'model': MODEL, 'files': FILES,
            'hook_sha256': digest(self.log.read_bytes()), 'transcript_sha256': digest(self.transcript.read_bytes()),
            'trace': [
                {'type': 'tool_use', 'id': 'read-1', 'tool': 'Read', 'path': 'guide.md'},
                {'type': 'tool_use', 'id': 'write-1', 'tool': 'Write', 'path': 'src/app.mjs'},
                {'type': 'tool_result', 'id': 'read-1', 'error': False},
                {'type': 'tool_result', 'id': 'write-1', 'error': False},
            ],
        })
        self.assertNotIn('private', repr(receipt))

    def test_accepts_exact_unwrapped_prompt_and_keeps_failed_actual_read_out_of_successes(self):
        self.rows[0]['message']['content'] = REQUEST
        self.events[3]['ok'] = False
        self.rows.append({'role': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'read-1', 'is_error': True}]}})
        self.write()
        self.assertTrue(self.capture()['trace'][2]['error'])
        self.rows[-1]['message']['content'][0]['is_error'] = False
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'tool result disagrees'):
            self.capture()

    def test_same_prompt_in_assistant_prose_or_followup_user_turn_cannot_bind(self):
        self.rows.append(self.rows[0])
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'one frozen user prompt'):
            self.capture()
        self.rows.pop()
        self.rows[0]['role'] = 'assistant'
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'unsupported content block'):
            self.capture()

    def test_read_self_report_missing_raw_tool_or_failure_only_cannot_admit(self):
        self.rows[1]['message']['content'].pop(1)
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'lacks the frozen prompt or a permitted tool call'):
            self.capture()
        self.events[3]['ok'] = self.events[4]['ok'] = False
        self.events[4].pop('sha256')
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'lacks a successful'):
            self.capture()

    def test_terminal_files_must_match_the_last_observed_writes(self):
        self.events[-1]['files']['src/app.mjs'] = 'f' * 64
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'after a successful write'):
            self.capture()

    def test_unapproved_tool_and_raw_tool_hook_disagreement_are_rejected(self):
        call = self.rows[1]['message']['content'][1]
        call['name'] = 'Shell'
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'unsupported or unobserved tool'):
            self.capture()
        call['name'] = 'Read'
        call['input']['path'] = 'src/app.mjs'
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'agrees with its permitted hook'):
            self.capture()

    def test_editable_scope_mixed_generation_and_failed_or_missing_stop_are_rejected(self):
        self.events[2]['path'] = self.events[4]['path'] = 'guide.md'
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'approved file scope'):
            self.capture()
        self.events[2]['path'] = self.events[4]['path'] = 'src/app.mjs'
        self.events[4]['generation'] = 'changed'
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'mixes sessions'):
            self.capture()
        self.events[4]['generation'] = 'generation-1'
        self.events[-1]['status'] = 'error'
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'completed stop'):
            self.capture()
        self.events.pop()
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'completed stop'):
            self.capture()

    def test_changed_request_model_and_snapshot_scope_are_rejected(self):
        self.events[0]['request_sha256'] = 'a' * 64
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'frozen request'):
            self.capture()
        self.events[0]['request_sha256'] = self.request_digest
        self.events[0]['model'] = 'auto'
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'mixes sessions'):
            self.capture()
        self.events[0]['model'] = MODEL
        self.events[-1]['files'].pop('guide.md')
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'all approved file hashes'):
            self.capture()

    def test_another_session_or_workspace_is_not_read_or_substituted(self):
        self.rows[0]['cwd'] = str(self.root)
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'another workspace'):
            self.capture()
        self.rows[0].pop('cwd')
        self.rows[0]['sessionId'] = OTHER
        self.write()
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'another session'):
            self.capture()
        other = self.transcript.parent.parent / OTHER / f'{OTHER}.jsonl'
        other.parent.mkdir()
        self.transcript.rename(other)
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'missing or uses an indirect path'):
            self.capture()

    def test_symlink_parent_and_transcript_replaced_during_capture_are_rejected(self):
        self.write()
        parent = self.transcript.parent
        moved = parent.with_name('detached')
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'indirect path'):
            self.capture()
        parent.unlink()
        moved.rename(parent)
        original = cursor_eval_trace._transcript

        def replace(*args):
            original(*args)
            copy = self.transcript.with_suffix('.new')
            copy.write_bytes(self.transcript.read_bytes())
            copy.replace(self.transcript)

        with patch.object(cursor_eval_trace, '_transcript', replace):
            with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'changed before completion'):
                self.capture()

    def test_conflicting_json_keys_and_bounded_read_fail_closed(self):
        self.write()
        with patch.object(cursor_eval_trace, 'MAX_TRANSCRIPT', 1):
            with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'size limit'):
                self.capture()
        raw = self.log.read_bytes()
        self.log.write_bytes(raw.replace(b'"event": "submit"', b'"event": "submit", "event": "submit"', 1))
        with self.assertRaisesRegex(cursor_eval_trace.TraceError, 'conflicting JSON keys'):
            self.capture()
