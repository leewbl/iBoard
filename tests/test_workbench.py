import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from workbench import delivery

CLI = Path(__file__).resolve().parents[1] / 'workbench' / 'cli.py'

class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        (self.repo / 'app.txt').write_text('broken\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.task = self.root / 'task.json'
        self.run_cli('init', '--task', str(self.task), '--id', 't1', '--repo', str(self.repo), '--kind', 'bug', '--source', 'issue 1', '--surface', 'non-ui', '--implementer', 'developer', '--iterations', '3', '--seconds', '3600')
        self.run_cli('criterion-add', '--task', str(self.task), '--id', 'C1', '--text', 'reproduces then resolves', '--reason', 'issue acceptance')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def run_cli(self, *args, code=0):
        result = subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stderr + result.stdout)
        return result

    def evidence(self, kind, commit, actor='developer', criterion=None):
        path = self.root / f'{kind}.txt'
        path.write_text(f'{kind} evidence\n')
        args = ['evidence-add', '--task', str(self.task), '--kind', kind, '--file', str(path), '--commit', commit, '--actor', actor, '--description', kind]
        if criterion:
            args.extend(['--criterion', criterion])
        self.run_cli(*args)
        return path

    def finished_task(self):
        self.evidence('reproduction', self.base)
        (self.repo / 'app.txt').write_text('fixed\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fix')
        head = self.git('rev-parse', 'HEAD').strip()
        for kind in ('resolution', 'test-result', 'runtime-log'):
            self.evidence(kind, head)
        self.evidence('independent-review', head, actor='verifier', criterion='C1')
        return head

    def test_missing_evidence_blocks(self):
        out = self.run_cli('gate', '--task', str(self.task), '--commit', 'HEAD', code=1)
        issues = json.loads(out.stdout)['issues']
        self.assertTrue(any('reproduction' in issue for issue in issues))

    def test_removing_limits_requires_paused_task_and_records_reason(self):
        args = ['remove-limits', '--task', str(self.task), '--reason', 'user authorized unlimited task']
        self.run_cli(*args, code=2)
        self.run_cli('pause', '--task', str(self.task), '--reason', 'change task budget')
        self.run_cli(*args)
        record = json.loads(self.task.read_text())
        self.assertIsNone(record['limits']['seconds'])
        self.assertIsNone(record['limits']['iterations'])
        self.assertEqual(record['events'][-1]['old_limits'], {'seconds': 3600, 'iterations': 3})
        self.run_cli('resume', '--task', str(self.task))
        for _ in range(4):
            self.run_cli('iteration', '--task', str(self.task))
        self.assertEqual(json.loads(self.task.read_text())['iterations'], 4)

    def test_existing_skip_policy_is_locked_audited_and_bound_to_evidence(self):
        basis = self.root / 'policy.txt'
        basis.write_text('CI selected=false lanes are proven by the fixed aggregate gate.\n')
        args = ['ci-skip-policy', '--task', str(self.task), '--policy', 'openclaw-selected-v1',
                '--reason', 'preserve selected checks', '--source', 'CI investigation', '--basis', str(basis),
                '--required-ci-workflow', 'CI']
        self.run_cli(*args, code=2)
        self.run_cli('pause', '--task', str(self.task), '--reason', 'configure policy')
        self.run_cli(*args)
        record = json.loads(self.task.read_text())
        self.assertEqual(record['required_ci_workflows'], ['CI'])
        self.assertTrue(delivery._recorded_skip_policy(record))
        self.assertEqual(record['events'][-1]['source'], 'CI investigation')
        self.run_cli(*args, code=2)
        basis.write_text('altered evidence\n')
        self.assertFalse(delivery._recorded_skip_policy(record))

    def test_policy_command_does_not_replace_existing_ci_requirement(self):
        basis = self.root / 'policy.txt'; basis.write_text('basis\n')
        record = json.loads(self.task.read_text())
        record['required_ci_workflows'] = ['CI', 'Security Review']
        self.task.write_text(json.dumps(record))
        self.run_cli('pause', '--task', str(self.task), '--reason', 'configure policy')
        self.run_cli('ci-skip-policy', '--task', str(self.task), '--policy', 'openclaw-selected-v1',
                     '--reason', 'test', '--source', 'evidence', '--basis', str(basis),
                     '--required-ci-workflow', 'CI', code=2)
        self.assertEqual(json.loads(self.task.read_text())['required_ci_workflows'], ['CI', 'Security Review'])

    def test_gate_requires_valid_unchanged_evidence(self):
        head = self.finished_task()
        out = self.run_cli('gate', '--task', str(self.task), '--commit', head)
        self.assertTrue(json.loads(out.stdout)['passed'])
        (self.root / 'resolution.txt').write_text('tampered')
        out = self.run_cli('gate', '--task', str(self.task), '--commit', head, code=1)
        self.assertTrue(any('changed' in issue for issue in json.loads(out.stdout)['issues']))

    def test_criteria_change_invalidates_review_and_keeps_history(self):
        head = self.finished_task()
        basis = self.root / 'basis.txt'
        basis.write_text('investigation shows condition needs clarification')
        self.run_cli('criterion-change', '--task', str(self.task), '--id', 'C1', '--text', 'clarified condition', '--reason', 'investigation evidence', '--basis', str(basis))
        out = self.run_cli('gate', '--task', str(self.task), '--commit', head, code=1)
        self.assertTrue(any('independent review' in issue for issue in json.loads(out.stdout)['issues']))
        record = json.loads(self.task.read_text())
        self.assertEqual(record['criterion_changes'][-1]['old'], 'reproduces then resolves')

    def test_independent_reviewer_must_differ(self):
        path = self.root / 'review.txt'
        path.write_text('review')
        self.run_cli('evidence-add', '--task', str(self.task), '--kind', 'independent-review', '--file', str(path), '--commit', 'HEAD', '--actor', 'developer', '--description', 'review', '--criterion', 'C1', code=2)

    def test_budget_pauses_task_and_preserves_record(self):
        for _ in range(3):
            self.run_cli('iteration', '--task', str(self.task))
        self.run_cli('iteration', '--task', str(self.task), code=2)
        record = json.loads(self.task.read_text())
        self.assertEqual(record['status'], 'paused')
        self.assertEqual(record['iterations'], 3)
        self.run_cli('gate', '--task', str(self.task), '--commit', 'HEAD', code=1)

    def test_time_limit_extension_is_explicit_and_audited(self):
        self.run_cli('extend-time', '--task', str(self.task), '--seconds', '7200', '--reason', 'user', code=2)
        self.run_cli('pause', '--task', str(self.task), '--reason', 'limit reached')
        self.run_cli('extend-time', '--task', str(self.task), '--seconds', '3600', '--reason', 'user', code=2)
        self.run_cli('extend-time', '--task', str(self.task), '--seconds', '7200', '--reason', 'user granted extra time')
        record = json.loads(self.task.read_text())
        self.assertEqual(record['status'], 'paused')
        self.assertEqual(record['limits']['seconds'], 7200)
        self.assertEqual(record['events'][-1]['old_seconds'], 3600)
        self.assertEqual(record['events'][-1]['new_seconds'], 7200)
        self.assertEqual(record['events'][-1]['reason'], 'user granted extra time')
        self.run_cli('resume', '--task', str(self.task))
        self.assertEqual(json.loads(self.task.read_text())['status'], 'running')

    def test_feature_requires_document_test_and_surface_evidence(self):
        task = json.loads(self.task.read_text())
        task['kind'] = 'feature'
        task['surface'] = 'ui'
        self.task.write_text(json.dumps(task))
        (self.repo / 'app.txt').write_text('new feature\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'feature')
        head = self.git('rev-parse', 'HEAD').strip()
        for kind in ('feature-doc', 'test-result', 'screenshot'):
            self.evidence(kind, head)
        self.evidence('independent-review', head, actor='verifier', criterion='C1')
        self.run_cli('gate', '--task', str(self.task), '--commit', head)
        (self.root / 'screenshot.txt').unlink()
        out = self.run_cli('gate', '--task', str(self.task), '--commit', head, code=1)
        self.assertTrue(any('missing or unreadable' in issue for issue in json.loads(out.stdout)['issues']))

    def test_review_and_evidence_do_not_follow_new_head(self):
        head = self.finished_task()
        (self.repo / 'app.txt').write_text('changed again\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'followup')
        out = self.run_cli('gate', '--task', str(self.task), '--commit', 'HEAD', code=1)
        self.assertTrue(any('resolution' in issue for issue in json.loads(out.stdout)['issues']))
        out = self.run_cli('gate', '--task', str(self.task), '--commit', head, code=1)
        self.assertTrue(any('current HEAD' in issue for issue in json.loads(out.stdout)['issues']))

    def test_dirty_worktree_blocks(self):
        head = self.finished_task()
        (self.repo / 'app.txt').write_text('uncommitted\n')
        out = self.run_cli('gate', '--task', str(self.task), '--commit', head, code=1)
        self.assertTrue(any('not clean' in issue for issue in json.loads(out.stdout)['issues']))

if __name__ == '__main__':
    unittest.main()
