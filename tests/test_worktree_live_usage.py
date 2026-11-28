from contextlib import ExitStack, contextmanager
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, worktree_cleanup as cleanup


HOLDER = '''
import sys

opened = open(sys.argv[1], 'rb') if len(sys.argv) == 2 else None
print('ready', flush=True)
sys.stdin.buffer.read()
if opened is not None:
    opened.close()
'''


needs_live_probe = unittest.skipUnless(shutil.which('lsof') or Path('/proc/self/fd').is_dir(),
                                       'needs a local process inspection backend')


class WorktreeLiveUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'repository'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'tracked.txt').write_text('retained in every checkout\n', encoding='utf-8')
        cli.git(self.repo, 'add', 'tracked.txt')
        cli.git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Store fixture contents')
        self.base = cli.sha(self.repo, 'HEAD')
        self.holds = self.root / 'holds.json'
        cli.write(self.holds, {'schema': cli.SCHEMA, 'entries': []})

    def tree(self, branch, directory=None):
        path = self.root / (directory or branch + ' worktree')
        cli.git(self.repo, 'worktree', 'add', '-b', branch, str(path), self.base)
        return path

    @contextmanager
    def process(self, *, cwd=None, opened=None):
        command = [sys.executable, '-c', HOLDER]
        if opened is not None:
            command.append(str(opened))
        with subprocess.Popen(command, cwd=cwd or self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as child:
            try:
                ready, _, _ = select.select([child.stdout], [], [], 10)
                self.assertEqual(child.stdout.readline() if ready else None, b'ready\n')
                yield child.pid
            finally:
                child.stdin.close()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)

    def inventory(self):
        result = cleanup.inventory(self.repo, 'main', holds=self.holds)
        self.assertTrue(result['live_usage']['backend'])
        self.assertTrue(result['live_usage']['status'])
        return {row['path']: row for row in result['worktrees']}

    def freeze(self, path):
        return cleanup.freeze(self.repo, 'main', [str(path)], self.root / 'plan.json', holds=self.holds)

    def assert_retained_for(self, row, pid, access):
        self.assertEqual(row['bucket'], 'keep', row)
        self.assertIn('live local process or Claude session', row['reasons'])
        self.assertIn((pid, access), {(item['pid'], item['access']) for item in row['live_usage']})

    @needs_live_probe
    def test_inventory_protects_working_directory_and_open_file(self):
        current = self.tree('current')
        opened = self.tree('opened')
        with self.process(cwd=current) as cwd_pid, self.process(opened=opened / 'tracked.txt') as file_pid:
            rows = self.inventory()
            self.assert_retained_for(rows[str(current)], cwd_pid, 'cwd')
            self.assert_retained_for(rows[str(opened)], file_pid, 'open-file')
            with self.assertRaisesRegex(cleanup.CleanupError, 'live local process or Claude session'):
                self.freeze(opened)
        self.assertTrue(current.is_dir())
        self.assertTrue(opened.is_dir())

    @needs_live_probe
    def test_open_file_in_sibling_with_same_prefix_has_no_false_match(self):
        idle = self.tree('idle', 'candidate worktree')
        busy = self.tree('busy', 'candidate worktree-backup')
        with self.process(opened=busy / 'tracked.txt') as pid:
            rows = self.inventory()
            self.assertEqual(rows[str(idle)]['bucket'], 'candidate', rows[str(idle)])
            self.assertEqual(rows[str(idle)]['live_usage'], [])
            self.assert_retained_for(rows[str(busy)], pid, 'open-file')

    @needs_live_probe
    def test_apply_rechecks_after_approval_and_can_resume_when_process_exits(self):
        candidate = self.tree('candidate')
        approval = self.freeze(candidate)
        receipt = self.root / 'receipt.json'
        holds_before = self.holds.read_bytes()

        def apply():
            return cleanup.apply(approval['plan'], approval['sha256'], holds=self.holds, out=receipt)

        with self.process(opened=candidate / 'tracked.txt'):
            blocked = apply()
            self.assertEqual(blocked['status'], 'blocked', blocked)
            self.assertEqual(blocked['removed'], {})
            self.assertIn('live local process or Claude session', blocked['blocked'][str(candidate)])
            self.assertTrue((candidate / 'tracked.txt').is_file())
            self.assertEqual(cli.sha(self.repo, 'refs/heads/candidate'), self.base)

        resumed = apply()
        self.assertEqual(resumed['status'], 'complete', resumed)
        self.assertEqual(set(resumed['removed']), {str(candidate)})
        self.assertFalse(candidate.exists())
        self.assertEqual(cli.sha(self.repo, 'refs/heads/candidate'), self.base)
        self.assertEqual(self.holds.read_bytes(), holds_before)

    def test_unavailable_usage_scan_keeps_tree_out_of_cleanup_plan(self):
        candidate = self.tree('candidate')
        failure = {'backend': 'lsof', 'status': 'unavailable'}
        with patch.object(cleanup.worktree_usage, 'scan', return_value=(failure, {})):
            row = self.inventory()[str(candidate)]
            self.assertEqual(row['bucket'], 'keep', row)
            self.assertIn('live local usage could not be verified', row['reasons'])
            with self.assertRaisesRegex(cleanup.CleanupError, 'live local usage could not be verified'):
                self.freeze(candidate)
        self.assertTrue((candidate / 'tracked.txt').is_file())

    @needs_live_probe
    def test_apply_queries_live_usage_again_after_last_status_check(self):
        candidate = self.tree('candidate')
        approval = self.freeze(candidate)
        original_status = cleanup._status
        candidate_checks = 0
        with ExitStack() as stack:
            def inspect(path):
                nonlocal candidate_checks
                result = original_status(path)
                if Path(path) == candidate:
                    candidate_checks += 1
                    if candidate_checks == 2:
                        stack.enter_context(self.process(opened=candidate / 'tracked.txt'))
                return result

            stack.enter_context(patch.object(cleanup, '_status', side_effect=inspect))
            report = cleanup.apply(approval['plan'], approval['sha256'], holds=self.holds,
                                   out=self.root / 'receipt.json')
            self.assertGreaterEqual(candidate_checks, 2)
            self.assertEqual(report['status'], 'blocked', report)
            self.assertIn(str(candidate), report['blocked'])
            self.assertEqual(report['removed'], {})
            self.assertTrue((candidate / 'tracked.txt').is_file())


if __name__ == '__main__':
    unittest.main()
