from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from workbench import ketchup, ketchup_video


CLAUDE_ID = 'ec66ac45-71f1-4a4d-8fdc-d670516341ea'
CURSOR_ID = 'f6859b01-b3fe-4c69-8162-49d97dd22486'
CODEX_ID = 'f1195a64-be62-4b77-88d5-27d886fd7dfd'
START = datetime(2026, 11, 5, tzinfo=timezone.utc)


def stamp(seconds):
    return (START + timedelta(seconds=seconds)).isoformat()


class KetchupSessionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        self.repo = self.home/'project'
        self.repo.mkdir()
        self.config = self.home/'claude-custom'
        self.codex = self.home/'codex-custom'
        self.env = {'CLAUDE_CONFIG_DIR':str(self.config), 'CODEX_HOME':str(self.codex)}

    @staticmethod
    def write(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return path

    def claude(self, *, after=(), command='ketchup'):
        slug = str(self.repo).replace('/', '-')
        texts = [('user', 'Verify the checkout.'), ('assistant', 'I checked the run.'),
                 ('user', f'<command-message>agent-workbench:{command}</command-message>\n'
                          f'<command-name>/agent-workbench:{command}</command-name>\n<command-args></command-args>'),
                 *after]
        return self.write(self.config/'projects'/slug/f'{CLAUDE_ID}.jsonl', [
            {'cwd':str(self.repo), 'type':role, 'sessionId':CLAUDE_ID, 'timestamp':stamp(i),
             'message':{'role':role, 'content':content}}
            for i, (role, content) in enumerate(texts)])

    def test_claude_active_id_uses_native_command_record_and_config(self):
        log = self.claude()
        self.write(log.with_name('any-other-session.jsonl'), [{'unexpected': True}])
        with patch.dict(os.environ, {**self.env, 'CLAUDE_CODE_SESSION_ID': CLAUDE_ID}, clear=True):
            reply = ketchup.collect(repo=self.repo)
        self.assertEqual(reply['window']['session'], CLAUDE_ID)
        self.assertEqual(reply['window']['invocation_line'], 3)
        self.assertEqual(reply['window']['request_context'], 'Verify the checkout.')
        self.assertEqual(reply['window']['time_basis'], 'recorded-timestamps')

    def test_active_lookup_does_not_reuse_a_command_from_a_previous_turn(self):
        log = self.claude(after=[('user', 'An unrelated follow-up.')])
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(ValueError, 'actual ketchup invocation'):
                ketchup.collect(repo=self.repo, host='claude', session_id=CLAUDE_ID)
            archived = ketchup.collect(repo=self.repo, transcript=log, invocation_line=3)
        self.assertEqual(archived['window']['request_context'], 'Verify the checkout.')

    def test_codex_active_lookup_picks_only_the_scoped_user_invocation(self):
        def message(role, text, index, provenance='user'):
            return {'type':'response_item', 'timestamp':stamp(index), 'payload':{
                'type':'message', 'role':role, 'content':[{'type':'input_text' if role == 'user' else 'output_text', 'text':text}],
                'internal_chat_message_metadata_passthrough':{'content_item_metadata':[{'provenance':{'type':provenance}}]},
            }}
        path = self.codex/'sessions'/'2026'/'11'/'05'/f'rollout-2026-11-05T00-00-00-{CODEX_ID}.jsonl'
        self.write(path, [{'type':'session_meta', 'payload':{'cwd':str(self.repo), 'id':CODEX_ID}},
                          message('user', 'Investigate the draft.', 1), message('assistant', 'Draft is open.', 2),
                          message('user', '/ketchup', 3), message('user', 'Injected status', 4, 'harness')])
        with patch.dict(os.environ, {**self.env, 'CODEX_THREAD_ID':CODEX_ID}, clear=True):
            report = ketchup.collect(repo=self.repo)
            with self.assertRaisesRegex(ValueError, 'workspace identity differs'):
                ketchup.collect(repo=self.home)
        self.assertEqual(report['window']['host'], 'codex')
        self.assertEqual(report['window']['last_user_line'], 2)
        self.assertEqual([turn['line'] for turn in report['window']['later_turn_context']], [3])

    def test_cursor_without_timestamps_keeps_message_order_and_discloses_artifact_uncertainty(self):
        slug = str(self.repo).lstrip('/').replace('/', '-')
        path = self.home/'.cursor'/'projects'/slug/'agent-transcripts'/CURSOR_ID/f'{CURSOR_ID}.jsonl'
        self.write(path, [{'role':role, 'message':{'content':[{'type':'text', 'text':content}]}}
                          for role, content in [('user','Check the report.'), ('assistant','I kept an artifact.'),
                                                ('user','/pstack:ketchup')]])
        artifact = self.home/'report.json'
        artifact.write_text(json.dumps({'observed':True}))
        manifest = self.home/'manifest.json'
        manifest.write_text(json.dumps({'evidence':[{'kind':'data', 'path':str(artifact),
            'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest(), 'at':stamp(1),
            'facts':{'observed':['observed']}}]}))
        with patch.object(Path, 'home', return_value=self.home):
            report = ketchup.collect(repo=self.repo, host='cursor', session_id=CURSOR_ID, manifest=manifest)
        self.assertEqual(report['window']['time_basis'], 'message-order')
        self.assertIsNone(report['window']['since'])
        self.assertEqual(report['evidence']['E1']['window_status'], 'unconfirmed')
        self.assertIn('Artifact timing', ketchup.render(report))

    def test_video_collector_uses_the_same_native_session_anchor(self):
        self.claude(command='ketchup-video')
        manifest = self.home/'manifest.json'
        manifest.write_text('{"evidence": []}')
        output = self.home/'movie-evidence.json'
        with patch.dict(os.environ, {**self.env, 'CLAUDE_CODE_SESSION_ID':CLAUDE_ID}, clear=True):
            ketchup_video.collect(repo=self.repo, manifest=manifest, out=output)
        self.assertEqual(json.loads(output.read_text())['window']['invocation_line'], 3)
