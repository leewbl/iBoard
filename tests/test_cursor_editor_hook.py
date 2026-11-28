import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'model/cursor_editor_hook.py'
SESSION = '9482f3b7-747b-47c6-b1f4-5fcd9c674bf8'


class CursorHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.work = self.root / 'project'
        self.work.mkdir()
        (self.work / 'read.txt').write_text('private fixture')
        (self.work / 'edit.txt').write_text('output')
        self.log = self.root / 'events.jsonl'
        self.config = self.root / 'hook.json'
        self.config.write_text(json.dumps({'workspace': str(self.work), 'model': 'model-1',
            'request_sha256': hashlib.sha256(b'fix it').hexdigest(), 'files': ['read.txt', 'edit.txt'],
            'editable': ['edit.txt'], 'log': str(self.log),
            'initial': {name: hashlib.sha256((self.work / name).read_bytes()).hexdigest()
                        for name in ('read.txt', 'edit.txt')}}))
        self.base = {'conversation_id': SESSION, 'generation_id': 'generation-1', 'model_id': 'model-1',
                     'workspace_roots': [str(self.work)], 'cwd': str(self.work), 'user_email': 'private@example.com'}

    def call(self, name, **data):
        process = subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), str(self.config)],
                                 input=json.dumps({**self.base, 'hook_event_name': name, **data}),
                                 text=True, capture_output=True, timeout=5)
        return process.returncode, json.loads(process.stdout)

    def tool(self, name, ident, path, event='preToolUse'):
        return self.call(event, tool_name=name, tool_use_id=ident, tool_input={'file_path': path})

    def test_approved_sequence_and_private_digests(self):
        self.assertEqual(self.call('beforeSubmitPrompt', prompt='fix it', attachments=[]), (0, {'continue': True}))
        self.assertEqual(self.tool('Read', 'one', 'read.txt'), (0, {'permission': 'allow'}))
        self.assertEqual(self.tool('Read', 'one', 'read.txt', 'postToolUse'), (0, {}))
        self.assertEqual(self.tool('StrReplace', 'two', str(self.work / 'edit.txt')), (0, {'permission': 'allow'}))
        self.assertEqual(self.tool('StrReplace', 'two', 'edit.txt', 'postToolUseFailure'), (0, {}))
        self.assertEqual(self.call('stop', status='completed'), (0, {}))
        rows = [json.loads(row) for row in self.log.read_text().splitlines()]
        self.assertEqual([r['event'] for r in rows], ['submit', 'start', 'result', 'start', 'result', 'stop'])
        self.assertTrue(rows[2]['ok'])
        self.assertFalse(rows[4]['ok'])
        self.assertEqual(rows[-1]['files'], {name: hashlib.sha256((self.work / name).read_bytes()).hexdigest()
                                             for name in ('read.txt', 'edit.txt')})
        self.assertNotIn('private', self.log.read_text())
        self.assertNotEqual(self.tool('Read', 'three', 'read.txt')[0], 0)

    def test_denies_unapproved_inputs_and_paths(self):
        self.assertEqual(self.call('beforeSubmitPrompt', prompt='wrong', attachments=[]), (2, {'continue': False}))
        self.assertEqual(self.call('beforeSubmitPrompt', prompt='fix it', attachments=[{'type': 'file'}]), (2, {'continue': False}))
        self.assertEqual(self.call('beforeSubmitPrompt', prompt='fix it', generation_id=''), (2, {'continue': False}))
        self.assertEqual(self.call('beforeSubmitPrompt', prompt='fix it'), (0, {'continue': True}))
        self.assertEqual(self.call('beforeSubmitPrompt', prompt='fix it'), (2, {'continue': False}))
        (self.work / 'link.txt').symlink_to(self.work / 'read.txt')
        for name, path in [('Shell', 'edit.txt'), ('Write', 'read.txt'), ('Read', '../hook.json'), ('Read', 'link.txt')]:
            self.assertEqual(self.tool(name, 'x', path), (2, {'permission': 'deny'}))
        self.assertEqual(self.call('preToolUse', model_id='other', tool_name='Read', tool_use_id='x',
                                   tool_input={'path': 'read.txt'}), (2, {'permission': 'deny'}))
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_submission_refuses_a_changed_baseline(self):
        (self.work / 'edit.txt').write_text('prefilled')
        self.assertEqual(self.call('beforeSubmitPrompt', prompt='fix it'), (2, {'continue': False}))
        self.assertEqual(self.log.read_text(), '')


if __name__ == '__main__':
    unittest.main()
