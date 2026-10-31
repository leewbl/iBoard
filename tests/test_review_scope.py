import json
import subprocess
import unittest
from unittest.mock import patch
import test_autopilot as fixtures
from workbench import autopilot


class ReviewScopeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.AutopilotTests(methodName='test_direct_review_receives_both_versions_without_tools')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def commit(self, content):
        (self.f.workspace / 'code.txt').write_text(content)
        subprocess.run(['git', '-C', str(self.f.workspace), 'add', 'code.txt'], check=True)
        subprocess.run(['git', '-C', str(self.f.workspace), 'commit', '-qm', 'candidate'], check=True)

    def test_large_source_uses_real_read_only_adapter_and_records_accurate_scope(self):
        self.commit('SOURCE MUST BE READ VIA TOOLS\n' * 5000)
        result = {'exit_code': 0, 'timed_out': False,
                  'stdout': '{"status":"blocked","reason":"requires more inspection"}', 'stderr': ''}
        with patch.object(autopilot, 'execute_model', return_value=result) as cli, \
                patch.object(autopilot, 'direct_review') as direct:
            output = autopilot._model(self.f.task, 'final-review', 'ORIGINAL REPORT EVIDENCE',
                                      read_only=True, model=cli)
        direct.assert_not_called()
        self.assertTrue(cli.call_args.kwargs['read_only'])
        prompt = cli.call_args.args[1]
        self.assertIn('ORIGINAL REPORT EVIDENCE', prompt)
        self.assertIn('complete changed-file contents are NOT attached', prompt)
        self.assertIn('Inspect the exact git diff', prompt)
        self.assertIn('Return blocked', prompt)
        self.assertNotIn('SOURCE MUST BE READ VIA TOOLS', prompt)
        self.assertLessEqual(len(prompt), 100000)
        self.assertIn('blocked', output)
        saved = json.loads((self.f.task.parent / 'final-review-model.json').read_text())
        self.assertEqual(saved['review_scope'], 'read-only workspace inspection and supplied evidence')

    def test_small_source_keeps_tool_free_direct_review(self):
        self.commit('fixed small source')
        with patch.object(autopilot, 'execute_model') as cli, \
                patch.object(autopilot, 'direct_review', return_value='{"status":"blocked"}') as direct:
            autopilot._model(self.f.task, 'final-review', 'REPORT', read_only=True, model=cli)
        cli.assert_not_called()
        self.assertIn('fixed small source', direct.call_args.args[0])
        self.assertIn('no tools', direct.call_args.args[0])

    def test_oversized_original_evidence_never_falls_back_or_calls_any_model(self):
        with patch.object(autopilot, 'execute_model') as cli, patch.object(autopilot, 'direct_review') as direct:
            with self.assertRaisesRegex(autopilot.AutopilotError, '100000'):
                autopilot._model(self.f.task, 'final-review', 'x' * 100001, read_only=True, model=cli)
        cli.assert_not_called()
        direct.assert_not_called()

    def test_cli_failure_is_saved_and_cannot_masquerade_as_a_review(self):
        self.commit('large source\n' * 10000)
        with patch.object(autopilot, 'execute_model', return_value={
                'exit_code': 1, 'timed_out': False, 'stdout': '', 'stderr': 'adapter failed'}) as cli:
            with self.assertRaisesRegex(autopilot.AutopilotError, 'final-review model failed'):
                autopilot._model(self.f.task, 'final-review', 'REPORT', read_only=True, model=cli)
        saved = json.loads((self.f.task.parent / 'final-review-model.json').read_text())
        self.assertEqual(saved['exit_code'], 1)
        self.assertEqual(saved['review_scope'], 'read-only workspace inspection and supplied evidence')
