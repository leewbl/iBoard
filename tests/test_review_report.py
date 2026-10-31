import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from workbench import autopilot


class ReviewReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'report.json'

    def report(self, output, **changes):
        d = {'command': ['node', '-e', 'x' * 60000], 'stdout': output, 'stderr': '',
             'exit_code': 0, 'timed_out': False, 'cleanup_confirmed': True,
             'expected_marker': 'ACTUAL_END_MARKER', **changes}
        self.path.write_text(json.dumps(d))
        return self.path.read_bytes()

    def test_long_command_does_not_displace_actual_tail_and_original_is_unchanged(self):
        raw = self.report('noise\n' * 20000 + 'IBOARD_LOCK_EVENT {"type":"scenario-pass"}\nACTUAL_END_MARKER\n')
        view = autopilot._review_report(self.path)
        self.assertIn('ACTUAL_END_MARKER', view)
        self.assertIn('scenario-pass', view)
        self.assertNotIn('x' * 2000, view)
        self.assertIn(hashlib.sha256(raw).hexdigest(), view)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertLess(len(view), 14000)

    def test_actual_failure_is_quoted_without_synthesizing_success(self):
        self.report('noise\n' * 20000 + 'IBOARD_LOCK_EVENT {"type":"scenario-fail","error":"actual failure"}\n', exit_code=1)
        view = autopilot._review_report(self.path)
        self.assertIn('actual failure', view)
        self.assertIn('"exit_code": 1', view)
        self.assertNotIn('scenario-pass', view)
        self.assertNotIn('ACTUAL_END_MARKER\n', view)

    def test_oversized_critical_evidence_stops_instead_of_silently_omitting(self):
        self.report('IBOARD_LOCK_EVENT {"type":"scenario-fail","detail":"' + 'z' * 7000 + '"}\n')
        with self.assertRaisesRegex(autopilot.AutopilotError, 'critical original'):
            autopilot._review_report(self.path)

    def test_full_review_budget_fails_closed(self):
        with self.assertRaisesRegex(autopilot.AutopilotError, '100000'):
            autopilot._bounded_review_prompt('a' * 100001)
        self.assertEqual(autopilot._bounded_review_prompt('source and actual output'), 'source and actual output')

    def test_oversized_result_omission_is_explicit_not_replaced_with_verdict(self):
        self.report('noise\n' * 1000 + 'IBOARD_LOCK_RESULT ' + json.dumps({'detail': 'r' * 2000}) + '\nACTUAL_END_MARKER\n')
        view = autopilot._review_report(self.path)
        self.assertIn('oversized original IBOARD_LOCK_RESULT', view)
        self.assertIn('return blocked if insufficient', view)
        self.assertNotIn('"verdict": "PASS"', view)

    def test_original_runner_truncation_stops_even_if_success_marker_is_visible(self):
        self.report('ACTUAL_END_MARKER', stdout_truncated=True)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'original execution output was truncated'):
            autopilot._review_report(self.path)
