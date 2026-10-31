import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from workbench import local_vitest, local_gate


class VitestGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(['git', '-C', str(self.root), 'init', '-q'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.name', 'T'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.email', 't@example.test'], check=True)
        self.name = 'src/unit.test.ts'
        for path, body in [(self.name, "test('x', () => {})"), (local_vitest.CONFIG, 'export default {}')]:
            item = self.root / path
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_text(body)
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'base'], check=True)
        self.base = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()
        self.report = {'success': True, 'numTotalTests': 1, 'numPassedTests': 1,
                       'numFailedTests': 0, 'numPendingTests': 0,
                       'testResults': [{'name': '/app/' + self.name, 'assertionResults': [{'status': 'passed'}]}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_tracked_files_pass(self):
        self.assertEqual(local_vitest.command(self.root, self.base, self.base, [self.name])[-1], '/app/' + self.name)
        self.assertEqual(local_vitest.validate_json(json.dumps(self.report) + 'JSON report written to /dev/stdout', [self.name]), 1)

    def test_rejects_empty_failed_and_skipped(self):
        for key, value in [('numTotalTests', 0), ('numPassedTests', 0), ('numFailedTests', 1),
                           ('numPendingTests', 1), ('success', False)]:
            with self.subTest(key=key), self.assertRaises(local_vitest.GuardError):
                local_vitest.validate_json(json.dumps({**self.report, key: value}), [self.name])

    def test_rejects_fabricated_marker_wrong_suite_and_filters(self):
        with self.assertRaises(local_vitest.GuardError): local_vitest.validate_json('PASS', [self.name])
        with self.assertRaises(local_vitest.GuardError): local_vitest.validate_json(json.dumps(self.report), ['src/other.test.ts'])
        with self.assertRaises(local_vitest.GuardError): local_vitest.command(self.root, self.base, self.base, ['-t'])

    def test_records_altered_test_and_rejects_skipped_source(self):
        for body in ("test('changed', () => {})", "test.skip('x', () => {})"):
            (self.root / self.name).write_text(body)
            subprocess.run(['git', '-C', str(self.root), 'add', self.name], check=True)
            subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'change'], check=True)
            head = subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()
            self.assertEqual(local_vitest.changed_test_sources(self.root, self.base, head)[0]['path'], self.name)
        with self.assertRaises(local_vitest.GuardError): local_vitest.command(self.root, head, head, [self.name])

    def test_changed_test_requires_independent_coverage_assessment(self):
        change = {'path': self.name, 'base_sha256': 'a', 'head_sha256': 'b'}
        reports = {'R7': {'path': '/proof/independent'}}
        assessment = {**change, 'verdict': 'PASS', 'preserves_coverage': True,
                      'reason': 'Original assertion and routing behavior remain covered.', 'evidence': ['DIFF', 'R7']}
        local_gate.guard_assessments({'guard_assessments': [assessment]}, [change], reports, '/proof/independent')
        for bad in ({}, {'guard_assessments': [{**assessment, 'preserves_coverage': False}]},
                    {'guard_assessments': [{**assessment, 'evidence': ['DIFF']}]},
                    {'guard_assessments': [{**assessment, 'head_sha256': 'wrong'}]}):
            with self.subTest(review=bad), self.assertRaises(local_gate.LocalGateError):
                local_gate.guard_assessments(bad, [change], reports, '/proof/independent')


if __name__ == '__main__': unittest.main()
