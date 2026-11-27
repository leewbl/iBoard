from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, swarm, worktree_cleanup as cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / 'repository'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q', '-b', 'main')
        for key, value in (('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid'),
                           ('commit.gpgsign', 'false')):
            cli.git(self.repo, 'config', key, value)
        (self.repo / '.gitignore').write_text('compiled.bin\n')
        (self.repo / 'code').write_text('initial\n')
        self.base = self.commit(self.repo, 'Initial source')
        self.holds = self.root / 'holds.json'
        self.mark([])
        self.task_root = self.root / 'tasks'
        self.task_root.mkdir()

    def commit(self, path, label):
        cli.git(path, 'add', '.')
        cli.git(path, 'commit', '-qm', label)
        return cli.sha(path, 'HEAD')

    def tree(self, name, *, detached=False):
        path = self.root / (name + ' worktree')
        mode = ('--detach',) if detached else ('-b', name)
        cli.git(self.repo, 'worktree', 'add', *mode, str(path), self.base)
        return path

    def mark(self, entries):
        cli.write(self.holds, {'schema': cli.SCHEMA,
                               'entries': [{'path': str(p), 'reason': reason} for p, reason in entries]})

    def freeze(self, *selected):
        return cleanup.freeze(self.repo, 'main', [str(p) for p in selected], self.root / 'plan.json',
                              holds=self.holds, tasks_roots=[self.task_root])

    def apply(self, approve):
        return cleanup.apply(approve['plan'], approve['sha256'], holds=self.holds, out=self.root / 'receipt.json')

    def test_finds_all_registered_paths_and_classifies_retained_references(self):
        passing = self.tree('passing', detached=True)
        pinned = self.tree('pinned')
        paused = self.tree('paused')
        tracked = self.tree('tracked')
        scratch = self.tree('scratch')
        ignored = self.tree('ignored')
        unmerged = self.tree('unmerged')
        prs = self.tree('open-pr')
        (unmerged / 'change').write_text('private branch commit\n')
        head = self.commit(unmerged, 'Unmerged branch')
        (tracked / 'code').write_text('user edits\n')
        (scratch / 'raw results').write_text('keep evidence\n')
        (ignored / 'compiled.bin').write_text('generated but unapproved\n')
        record = self.task_root / 'dispatch-1' / 'tasks' / 'case' / 'task.json'
        cli.write(record, {'schema': cli.SCHEMA, 'id': 'case', 'repo': str(paused), 'status': 'paused'})
        self.mark([(pinned / 'code', 'Pinned task source selection')])

        class Reader:
            def open_pulls(self, full_name):
                return [{'number': 18, 'headRefName': 'open-pr', 'headRefOid': head}]

        result = cleanup.inventory(self.repo, 'main', holds=self.holds, tasks_roots=[self.task_root],
                                   destination='fixture/repo', reader=Reader(), measure_size=True)
        rows = {row['path']: row for row in result['worktrees']}
        self.assertEqual(rows[str(passing)]['bucket'], 'candidate')
        self.assertGreater(rows[str(passing)]['measured_bytes'], 0)
        for path, reason in (
                (self.repo, 'primary or current checkout'), (pinned, 'active or pinned usage'),
                (paused, 'retained iBoard task or verification evidence'), (tracked, 'tracked work in progress'),
                (scratch, 'untracked files'), (ignored, 'ignored files'),
                (unmerged, 'HEAD not proven merged into selected base'), (prs, 'open PR')):
            with self.subTest(path=path):
                self.assertEqual(rows[str(path)]['bucket'], 'keep')
                self.assertIn(reason, rows[str(path)]['reasons'])
        self.assertEqual(rows[str(scratch)]['file_samples']['untracked'], ['raw results'])
        self.assertTrue(all(p.exists() for p in (passing, pinned, paused, tracked, scratch, ignored, unmerged, prs)))

    def test_removes_only_approved_tree_preserving_branch_and_durable_receipt(self):
        safe = self.tree('safe')
        other = self.tree('other')
        approve = self.freeze(safe)
        self.assertTrue(safe.exists())
        report = self.apply(approve)
        self.assertEqual(report['status'], 'complete', report)
        self.assertEqual(set(report['removed']), {str(safe)})
        self.assertFalse(safe.exists())
        self.assertTrue(other.exists())
        self.assertEqual(cli.sha(self.repo, 'refs/heads/safe'), self.base)
        self.assertIsInstance(report['observed_free_bytes_delta'], int)
        self.assertEqual(self.apply(approve)['removed'], report['removed'])

    def test_live_reader_requests_the_pr_head_commit(self):
        reader = cleanup.pr_watch.GitHubReader()
        head = {'number': 18, 'headRefName': 'feature', 'headRefOid': self.base}
        with patch.object(reader, '_call', return_value=[head]) as query:
            self.assertEqual(cleanup._pr_heads('fixture/repo', reader), [head])
        self.assertIn('headRefOid', query.call_args.args[-1].split(','))
        with patch.object(reader, '_call', return_value=[{'number': 18, 'headRefName': 'feature'}]):
            with self.assertRaisesRegex(cleanup.CleanupError, 'missing head identities'):
                cleanup._pr_heads('fixture/repo', reader)

    def test_rechecks_new_usage_and_untracked_content_at_apply(self):
        pinned = self.tree('pinned')
        modified = self.tree('modified')
        approve = self.freeze(pinned, modified)
        self.mark([(pinned, 'The checkout is now in use')])
        (modified / 'new-output').write_text('untracked evidence\n')
        report = self.apply(approve)
        self.assertEqual(report['status'], 'blocked', report)
        self.assertEqual(report['removed'], {})
        self.assertIn('active or pinned usage', report['blocked'][str(pinned)])
        self.assertIn('untracked files', report['blocked'][str(modified)])
        self.assertTrue((modified / 'new-output').exists())

    def test_base_drift_and_digest_change_refuse_any_removal(self):
        safe = self.tree('safe')
        approve = self.freeze(safe)
        (self.repo / 'code').write_text('new main\n')
        self.commit(self.repo, 'Advance base')
        report = self.apply(approve)
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('base changed', report['blocked']['approval'][0])
        self.assertTrue(safe.exists())
        plan = Path(approve['plan'])
        plan.write_bytes(plan.read_bytes() + b' ')
        with self.assertRaisesRegex(swarm.SwarmError, 'digest changed'):
            self.apply(approve)
        self.assertTrue(safe.exists())

    def test_locks_and_receipt_inside_worktree_are_not_approved(self):
        safe = self.tree('safe')
        cli.git(self.repo, 'worktree', 'lock', str(safe))
        with self.assertRaisesRegex(cleanup.CleanupError, 'locked'):
            self.freeze(safe)
        cli.git(self.repo, 'worktree', 'unlock', str(safe))
        approve = self.freeze(safe)
        with self.assertRaisesRegex(cleanup.CleanupError, 'outside linked worktrees'):
            cleanup.apply(approve['plan'], approve['sha256'], holds=self.holds, out=safe / 'receipt.json')
        self.assertTrue(safe.exists())


if __name__ == '__main__':
    unittest.main()
