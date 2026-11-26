import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from workbench import cli, phase_replay


class PhaseReplayTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '--quiet', '-b', 'main')
        cli.git(self.repo, 'config', 'user.name', 'Replay Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        self.trunk = self.commit('Original trunk', {'app.txt': 'before\n', 'base.txt': 'old\n'})
        cli.git(self.repo, 'switch', '-c', 'original-phase')
        self.old_base = self.commit('Prerequisite', {'base.txt': 'ready\n'})
        self.commit('Implement phase', {'app.txt': 'after\n'})
        self.old_head = self.commit('Document phase', {'FEATURE.md': 'Phase documentation\n'})
        cli.git(self.repo, 'switch', 'main')
        self.new_base = self.commit('Squash the prerequisite', {'base.txt': 'ready\n'})
        self.out = self.root / 'replay'
        self.inputs = {'trunk': self.trunk, 'old_base': self.old_base, 'old_head': self.old_head,
                       'new_base': self.new_base, 'files': ['app.txt', 'FEATURE.md']}

    def commit(self, message, changes, *, repo=None):
        repo = repo or self.repo
        for path, body in changes.items():
            (repo / path).write_text(body, encoding='utf-8')
        cli.git(repo, 'add', '--', *changes)
        cli.git(repo, 'commit', '-m', message)
        return cli.sha(repo, 'HEAD')

    def replay(self, **overrides):
        return phase_replay.prepare(self.repo, **{**self.inputs, **overrides}, out=self.out, timeout=60)

    @staticmethod
    def reference(report):
        return {'path': report['report'], 'sha256': report['sha256']}

    def validate(self, report, **overrides):
        return phase_replay.validate(self.reference(report), repo=self.repo, **{**self.inputs, **overrides})

    def test_replay_reuses_exact_sequence_after_squashed_prerequisite(self):
        before = cli.git(self.repo, 'show-ref', '--heads')
        self.assertNotEqual(self.old_base, self.new_base)
        self.assertEqual(cli.git(self.repo, 'rev-parse', self.old_base + '^{tree}'),
                         cli.git(self.repo, 'rev-parse', self.new_base + '^{tree}'))
        report = self.replay()
        self.assertEqual(report['status'], 'replayed', report)
        self.assertEqual(report['changed'], ['FEATURE.md', 'app.txt'])
        self.assertEqual(len(report['commit_patches']), 2)
        worktree = Path(report['worktree'])
        self.assertEqual(cli.sha(worktree, 'HEAD~2'), self.new_base)
        self.assertEqual(cli.git(worktree, 'status', '--porcelain'), '')
        self.assertEqual((worktree / 'app.txt').read_text(), 'after\n')
        self.assertEqual(self.validate(report)['candidate'], report['candidate'])
        self.assertEqual(cli.git(self.repo, 'show-ref', '--heads'), before)
        self.assertEqual(cli.sha(self.repo, 'HEAD'), self.new_base)
        self.assertEqual(cli.git(self.repo, 'status', '--porcelain'), '')

    def test_conflict_retains_detached_worktree_and_does_not_change_refs(self):
        conflict = self.commit('Conflicting main edit', {'app.txt': 'changed on main\n'})
        before = cli.git(self.repo, 'show-ref', '--heads')
        report = self.replay(new_base=conflict)
        self.assertEqual(report['status'], 'blocked')
        self.assertNotIn('candidate', report)
        self.assertIn('CONFLICT', (self.out / 'rebase.log').read_text())
        self.assertIn('UU app.txt', cli.git(Path(report['worktree']), 'status', '--porcelain'))
        self.assertEqual(cli.git(self.repo, 'show-ref', '--heads'), before)
        self.assertEqual((self.repo / 'app.txt').read_text(), 'changed on main\n')

    def test_duplicate_patch_blocks_rather_than_accepting_an_empty_replay(self):
        duplicate = self.commit('Phase already applied', {'app.txt': 'after\n', 'FEATURE.md': 'Phase documentation\n'})
        report = self.replay(new_base=duplicate)
        self.assertEqual(report['status'], 'blocked')
        self.assertNotIn('candidate', report)
        self.assertTrue((self.out / 'rebase.log').is_file())

    def test_rejects_merges_in_the_original_phase(self):
        cli.git(self.repo, 'switch', '-c', 'side-change', self.old_head)
        self.commit('Side change', {'app.txt': 'another authorized change\n'})
        cli.git(self.repo, 'switch', 'original-phase')
        cli.git(self.repo, 'merge', '--no-ff', '-m', 'Merge another change', 'side-change')
        report = self.replay(old_head=cli.sha(self.repo, 'HEAD'))
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('linear history', report['reason'])
        self.assertFalse(Path(report['worktree']).exists())

    def test_rejects_out_of_scope_and_completed_proof_tampering(self):
        separate = self.root / 'outside'
        before = self.replay(files=['app.txt'])
        self.assertEqual(before['status'], 'blocked')
        self.assertIn('outside the approved phase', before['reason'])
        self.assertFalse(Path(before['worktree']).exists())
        self.out = separate
        report = self.replay()
        self.assertEqual(report['status'], 'replayed')
        path = Path(report['report'])
        value = json.loads(path.read_text())
        value['changed'] = ['app.txt']
        cli.write(path, value)
        with self.assertRaisesRegex(phase_replay.ReplayError, 'receipt digest changed'):
            self.validate(report)
        forged = {**report, 'sha256': cli.digest(path)}
        with self.assertRaisesRegex(phase_replay.ReplayError, 'verified patch sequence'):
            self.validate(forged)

    def test_ignores_no_whitespace_changes_to_the_authorized_patch(self):
        report = self.replay()
        self.assertEqual(report['status'], 'replayed')
        cli.git(self.repo, 'switch', '-c', 'altered')
        self.commit('Whitespace change would pass stable patch ID', {'app.txt': 'after \n'})
        altered = self.commit('Document phase', {'FEATURE.md': 'Phase documentation\n'})
        path = Path(report['report'])
        value = json.loads(path.read_text())
        value['candidate'] = altered
        cli.write(path, value)
        with self.assertRaisesRegex(phase_replay.ReplayError, 'verified patch sequence'):
            self.validate({**report, 'sha256': cli.digest(path)})
