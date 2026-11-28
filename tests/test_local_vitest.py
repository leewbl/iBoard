import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from workbench import local_vitest, local_gate


class VitestRegressionTests(unittest.TestCase):
    path = 'src/agents/prompt.regression.test.ts'
    name = 'keeps the chosen runtime identity'
    marker = 'RUNTIME_IDENTITY_CHANGED'

    def _report(self, *, red=False):
        status = 'failed' if red else 'passed'
        return {
            'success': not red,
            'numTotalTestSuites': 1, 'numPassedTestSuites': int(not red),
            'numFailedTestSuites': int(red), 'numPendingTestSuites': 0,
            'numTotalTests': 1, 'numPassedTests': int(not red), 'numFailedTests': int(red),
            'numPendingTests': 0, 'numTodoTests': 0,
            'testResults': [{
                'name': '/app/' + self.path, 'status': status, 'message': '',
                'assertionResults': [{
                    'ancestorTitles': [], 'fullName': self.name, 'status': status,
                    'failureMessages': [f'AssertionError: {self.marker}: expected true to be false\n'
                                        '    at src/agents/prompt.regression.test.ts:8:5'] if red else [],
                }],
            }],
        }

    def _validate(self, report, *, red=False, stderr=''):
        output = json.dumps(report) + 'JSON report written to /dev/stdout\n'
        local_vitest.validate_regression_json(
            output, self.path, self.name, red=self.marker if red else None, stderr=stderr)

    def test_pins_native_shard_to_a_single_safe_file(self):
        for project in ('unit', 'unit-fast', 'gateway', 'agents-core'):
            with self.subTest(project=project):
                self.assertEqual(local_vitest.regression_command(self.path, project), [
                    'node', '/app/node_modules/vitest/vitest.mjs', 'run', '--reporter=json',
                    '--outputFile=/dev/stdout', '--config', f'/app/test/vitest/vitest.{project}.config.ts',
                    '--maxWorkers=1', self.path])
        for path, project in (('src/../wrong.regression.test.ts', 'unit'), ('/tmp/x.regression.test.ts', 'unit'),
                              ('src/x.test.ts', 'unit'), (self.path, '--config'), (self.path, [])):
            with self.subTest(path=path, project=project), self.assertRaises(local_vitest.GuardError):
                local_vitest.regression_command(path, project)

    def test_requires_one_actual_pass_or_intended_assertion(self):
        self._validate(self._report())
        self._validate(self._report(red=True), red=True)
        report = self._report(red=True)
        report['testResults'][0]['assertionResults'][0]['failureMessages'] = [
            f'AssertionError [ERR_ASSERTION]: {self.marker}\n    at src/agents/prompt.regression.test.ts:8:5']
        self._validate(report, red=True)

    def test_rejects_wrong_suite_test_counts_skips_and_setup_errors(self):
        for update in (
            {'success': False}, {'numTotalTests': 0}, {'numTotalTests': 2}, {'numPassedTests': True},
            {'numTodoTests': 1}, {'numPendingTests': 1}, {'numFailedTestSuites': 1},
            {'testResults': []},
        ):
            with self.subTest(update=update), self.assertRaises(local_vitest.GuardError):
                self._validate({**self._report(), **update})
        for update in ({'name': '/app/src/other.regression.test.ts'}, {'message': 'missing import'},
                       {'assertionResults': []}, {'status': 'skipped'}):
            report = self._report(red=True)
            report['testResults'][0].update(update)
            with self.subTest(update=update), self.assertRaises(local_vitest.GuardError):
                self._validate(report, red=True)
        report = self._report()
        report['testResults'][0]['assertionResults'][0]['fullName'] = 'a nearby passing test'
        with self.assertRaises(local_vitest.GuardError):
            self._validate(report)

    def test_rejects_unrelated_failures_and_unhandled_errors(self):
        for errors in (
            [f'TypeError: {self.marker}'],
            [f'AssertionError: something else\n{self.marker}'],
            [f'AssertionError: {self.marker}_SIMILAR: expected true to be false'],
            [f'AssertionError: {self.marker}', 'TypeError: unrelated teardown'],
        ):
            report = self._report(red=True)
            report['testResults'][0]['assertionResults'][0]['failureMessages'] = errors
            with self.subTest(errors=errors), self.assertRaises(local_vitest.GuardError):
                self._validate(report, red=True)
        with self.assertRaisesRegex(local_vitest.GuardError, 'unhandled error'):
            self._validate(self._report(red=True), red=True, stderr='\x1b[31mUnhandled Errors\x1b[0m')


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

    def _commit_sources(self, sources, message):
        for name, body in sources.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', message], check=True)
        return subprocess.check_output(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], text=True).strip()

    def test_real_git_enumerates_openclaw_helpers_mocks_and_test_configuration(self):
        names = [
            'src/gateway/test-helpers.ts',
            'src/gateway/shared-auth.test-helpers.ts',
            'src/talk/provider-discovery.test-fixtures.ts',
            'packages/net-policy/src/ip-test-fixtures.ts',
            'extensions/memory-core/src/memory/test-runtime-mocks.ts',
            'src/gateway/server-startup-post-attach.mocks.test-support.ts',
            'src/contracts/test-helpers/registry.ts',
            'src/test-utils/registry.ts',
            'src/gateway/__mocks__/runtime.ts',
            'test/setup-openclaw-runtime.ts',
            'test/vitest/vitest.gateway.config.ts',
            'vitest.config.ts', 'vite.config.ts', 'test.config.mts', 'test.setup.ts',
        ]
        base = self._commit_sources({name: 'before' for name in names}, 'helper baseline')
        head = self._commit_sources({**{name: 'after' for name in names},
                                     'src/gateway/new.test-helpers.ts': 'new helper',
                                     'src/gateway/production.ts': 'ordinary source'}, 'helper changes')
        changes = local_vitest.changed_test_sources(self.root, base, head)
        self.assertEqual([entry['path'] for entry in changes], sorted([*names, 'src/gateway/new.test-helpers.ts']))
        import hashlib
        for entry in changes:
            if entry['path'] == 'src/gateway/new.test-helpers.ts':
                self.assertIsNone(entry['base_sha256'])
                self.assertEqual(entry['head_sha256'], hashlib.sha256(b'new helper').hexdigest())
            else:
                self.assertEqual(entry['base_sha256'], hashlib.sha256(b'before').hexdigest())
                self.assertEqual(entry['head_sha256'], hashlib.sha256(b'after').hexdigest())

    def test_real_git_rejects_deleted_helper(self):
        name = 'src/gateway/test-helpers.ts'
        base = self._commit_sources({name: 'before'}, 'helper baseline')
        (self.root / name).unlink()
        head = self._commit_sources({}, 'delete helper')
        with self.assertRaisesRegex(local_vitest.GuardError, 'deleted or oversized'):
            local_vitest.changed_test_sources(self.root, base, head)

    def test_real_git_rejects_oversized_helper_before_or_after(self):
        name = 'src/gateway/shared-auth.test-helpers.ts'
        base = self._commit_sources({name: 'before'}, 'helper baseline')
        large = self._commit_sources({name: 'x' * 100001}, 'oversized helper')
        with self.assertRaisesRegex(local_vitest.GuardError, 'deleted or oversized'):
            local_vitest.changed_test_sources(self.root, base, large)
        small = self._commit_sources({name: 'small again'}, 'reduce helper')
        with self.assertRaisesRegex(local_vitest.GuardError, 'deleted or oversized'):
            local_vitest.changed_test_sources(self.root, large, small)

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
