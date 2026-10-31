import json
import signal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from model import claude_glm


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'glm.ini').write_text('test-secret')
        (self.root / '.claude').mkdir()
        (self.root / '.claude/settings.json').write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': 'https://open.bigmodel.cn/api/anthropic'}}))
        self.workspace = self.root / 'work'
        self.workspace.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_secret_not_in_arguments_or_result_and_sandbox_required(self):
        seen = {}
        class Process:
            returncode = 0
            def __init__(self, args, **kwargs):
                seen['args'] = args
                seen['env'] = kwargs['env']
                seen['settings'] = json.loads(Path(args[args.index('--settings') + 1]).read_text())
            def communicate(self, **kwargs):
                seen['prompt'] = kwargs['input']
                return ('response accidentally contains test-secret', '')
        with patch.object(claude_glm, 'ROOT', self.root), patch.object(Path, 'home', return_value=self.root), patch.object(claude_glm.subprocess, 'Popen', Process):
            result = claude_glm.execute(self.workspace, 'a task', 30)
        self.assertNotIn('test-secret', ' '.join(seen['args']))
        self.assertEqual(seen['env']['ANTHROPIC_API_KEY'], 'test-secret')
        self.assertEqual(seen['prompt'], 'a task')
        self.assertNotIn('test-secret', result['stdout'])
        self.assertTrue(seen['settings']['sandbox']['failIfUnavailable'])
        self.assertFalse(seen['settings']['sandbox']['allowUnsandboxedCommands'])
        self.assertIn('--restricted', seen['args'])

    def test_rejects_untrusted_endpoint(self):
        (self.root / '.claude/settings.json').write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': 'https://example.com'}}))
        with patch.object(claude_glm, 'ROOT', self.root), patch.object(Path, 'home', return_value=self.root):
            with self.assertRaises(ValueError):
                claude_glm.execute(self.workspace, 'a task', 30)

    def test_interruption_stops_model_process_group_and_reraises(self):
        class Process:
            pid = 12345
            calls = 0
            def __init__(self, *args, **kwargs):
                self.new_session = kwargs['start_new_session']
            def communicate(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt()
                return ('', '')
        with patch.object(claude_glm, 'ROOT', self.root), patch.object(Path, 'home', return_value=self.root), \
             patch.object(claude_glm.subprocess, 'Popen', Process), patch.object(claude_glm.os, 'killpg') as kill:
            with self.assertRaises(KeyboardInterrupt):
                claude_glm.execute(self.workspace, 'a task', 30)
        kill.assert_called_once_with(12345, signal.SIGKILL)

    def test_explicit_evidence_directory_is_readable_but_cannot_be_written(self):
        evidence = self.root / 'tasks' / 'one'
        evidence.mkdir(parents=True)
        seen = {}
        class Process:
            returncode = 0
            def __init__(self, args, **kwargs):
                seen['args'] = args
                seen['settings'] = json.loads(Path(args[args.index('--settings') + 1]).read_text())
            def communicate(self, **kwargs):
                return ('review', '')
        with patch.object(claude_glm, 'ROOT', self.root), patch.object(Path, 'home', return_value=self.root), \
             patch.object(claude_glm.subprocess, 'Popen', Process):
            claude_glm.execute(self.workspace, 'review', 30, read_only=True, evidence_paths=[evidence])
        fs = seen['settings']['sandbox']['filesystem']
        self.assertIn(str(evidence.resolve()), fs['allowRead'])
        self.assertIn(str(evidence.resolve()), fs['denyWrite'])
        self.assertIn(str(self.workspace.resolve()), fs['denyWrite'])
        self.assertEqual(seen['args'][seen['args'].index('--tools') + 1], 'Bash,Read,Glob,Grep')

    def test_evidence_cannot_expose_credentials_or_home_or_follow_links(self):
        evidence = self.root / 'evidence'
        evidence.mkdir()
        link = self.root / 'evidence-link'
        link.symlink_to(evidence, target_is_directory=True)
        regular = self.root / 'report.json'
        regular.write_text('{}')
        with patch.object(claude_glm, 'ROOT', self.root), patch.object(Path, 'home', return_value=self.root), \
             patch.object(claude_glm.subprocess, 'Popen') as spawn:
            for directory in (self.root, self.root.parent, Path('/'), self.root / '.claude', link, regular):
                with self.subTest(directory=str(directory)), self.assertRaises(ValueError):
                    claude_glm.execute(self.workspace, 'review', 30, read_only=True, evidence_paths=[directory])
            with self.assertRaises(ValueError):
                claude_glm.execute(self.workspace, 'implementation', 30, evidence_paths=[evidence])
        spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
