import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, worktree_cleanup as cleanup


class WorktreeRecentChatGateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repository'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'tracked.txt').write_text('fixture contents\n', encoding='utf-8')
        cli.git(self.repo, 'add', 'tracked.txt')
        cli.git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Store fixture contents')
        self.base = cli.sha(self.repo, 'HEAD')
        self.tree = self.root / 'candidate worktree'
        cli.git(self.repo, 'worktree', 'add', '-b', 'candidate', str(self.tree), self.base)
        self.holds = self.root / 'holds.json'
        self.plan = self.root / 'plan.json'
        self.receipt = self.root / 'receipt.json'
        self.write_holds()
        live_usage = patch.object(cleanup.worktree_usage, 'scan',
                                  return_value=({'backend': 'fixture', 'status': 'available'}, {}))
        live_usage.start()
        self.addCleanup(live_usage.stop)

    def write_holds(self, *reviews):
        holds = {'schema': cli.SCHEMA, 'entries': []}
        if reviews:
            holds['reviewed_recent'] = [{'path': str(self.tree), 'sha256': digest} for digest in reviews]
        cli.write(self.holds, holds)

    def scan_result(self, *, status='available', at=None):
        matches = {} if at is None else {str(self.tree): [{
            'host': 'codex', 'session_id': 'fixture-session', 'last_modified_ns': at,
        }]}
        return {'status': status}, matches

    def row(self, snapshot):
        return next(row for row in snapshot['worktrees'] if row['path'] == str(self.tree))

    def inventory(self):
        return cleanup.inventory(self.repo, 'main', holds=self.holds, inspect_recent_chats=True)

    def freeze(self, *, inspect_recent_chats=True):
        return cleanup.freeze(self.repo, 'main', [str(self.tree)], self.plan, holds=self.holds,
                              inspect_recent_chats=inspect_recent_chats)

    def apply(self, approval):
        return cleanup.apply(approval['plan'], approval['sha256'], holds=self.holds, out=self.receipt)

    def assert_blocked(self, report):
        self.assertEqual(report['status'], 'blocked', report)
        self.assertEqual(report['removed'], {})
        self.assertTrue((self.tree / 'tracked.txt').is_file())
        self.assertEqual(cli.sha(self.repo, 'refs/heads/candidate'), self.base)

    def test_plain_holds_and_legacy_plan_do_not_scan_recent_chats(self):
        with patch.object(cleanup.worktree_recent, 'scan') as scan:
            snapshot = cleanup.inventory(self.repo, 'main', holds=self.holds)
            self.assertEqual(self.row(snapshot)['bucket'], 'candidate')
            self.freeze(inspect_recent_chats=False)
            plan = cli.read(self.plan)
            plan.pop('inspect_recent_chats', None)
            cli.write(self.plan, plan)
            report = self.apply({'plan': str(self.plan), 'sha256': cli.digest(self.plan)})
            scan.assert_not_called()
        self.assertEqual(report['status'], 'complete', report)
        self.assertFalse(self.tree.exists())
        self.assertEqual(cli.sha(self.repo, 'refs/heads/candidate'), self.base)

    def test_exact_recent_review_allows_only_the_frozen_session_evidence(self):
        result = self.scan_result(at=123)
        with patch.object(cleanup.worktree_recent, 'scan', return_value=result) as scan:
            snapshot = self.inventory()
            self.assertEqual(snapshot['recent_chats']['status'], 'available')
            found = self.row(snapshot)
            self.assertEqual(found['recent_chats'], result[1][str(self.tree)])
            self.assertEqual(found['bucket'], 'keep', found)
            self.assertIn('recent chat usage requires review', found['reasons'])
            with self.assertRaisesRegex(cleanup.CleanupError, 'recent chat usage requires review'):
                self.freeze()
            digest = found['recent_chat_sha256']
            self.assertRegex(digest, r'^[0-9a-f]{64}$')
            self.write_holds(digest)
            approval = self.freeze()
            self.assertEqual(approval['selected'][0]['recent_chat_sha256'], digest)
            report = self.apply(approval)
            self.assertGreaterEqual(scan.call_count, 4)
        self.assertEqual(report['status'], 'complete', report)
        self.assertEqual(set(report['removed']), {str(self.tree)})
        self.assertFalse(self.tree.exists())
        self.assertEqual(cli.sha(self.repo, 'refs/heads/candidate'), self.base)

    def test_real_scan_detects_sibling_activity_from_the_git_primary(self):
        codex_home = self.root / 'codex-fixture'
        sessions = codex_home / 'sessions' / '2026' / '11' / '28'
        sessions.mkdir(parents=True)
        matching = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        unrelated = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'

        def fixture(ident, cwd):
            metadata = {'type': 'session_meta', 'payload': {'id': ident, 'cwd': str(cwd)}}
            call = {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command',
                    'arguments': json.dumps({'cmd': 'read fixture sentinel', 'workdir': str(self.tree)})}}
            (sessions / f'rollout-{ident}.jsonl').write_text(
                '\n'.join(map(json.dumps, (metadata, call))) + '\n', encoding='utf-8')

        fixture(matching, self.repo)
        fixture(unrelated, self.root / 'outside-project')
        with patch.dict(os.environ, CODEX_HOME=str(codex_home),
                        CLAUDE_CONFIG_DIR=str(self.root / 'claude-fixture')):
            snapshot = self.inventory()
        row = self.row(snapshot)
        self.assertEqual(snapshot['recent_chats']['status'], 'available')
        self.assertEqual(row['bucket'], 'keep', row)
        self.assertIn('recent chat usage requires review', row['reasons'])
        self.assertEqual([(item['session_id'], item['access']) for item in row['recent_chats']],
                         [(matching, 'path-reference')])
        self.assertNotIn('read fixture sentinel', json.dumps(snapshot))

    def test_partial_recent_scan_cannot_approve_a_worktree(self):
        with patch.object(cleanup.worktree_recent, 'scan', return_value=self.scan_result(status='partial')):
            row = self.row(self.inventory())
            self.assertEqual(row['bucket'], 'keep', row)
            with self.assertRaises(cleanup.CleanupError):
                self.freeze()
        self.assertFalse(self.plan.exists())
        self.assertTrue((self.tree / 'tracked.txt').is_file())

    def test_review_of_recent_evidence_preserves_a_pinned_hold(self):
        with patch.object(cleanup.worktree_recent, 'scan', return_value=self.scan_result(at=123)):
            digest = self.row(self.inventory())['recent_chat_sha256']
            cli.write(self.holds, {'schema': cli.SCHEMA,
                                   'entries': [{'path': str(self.tree), 'reason': 'Pinned fixture session'}],
                                   'reviewed_recent': [{'path': str(self.tree), 'sha256': digest}]})
            row = self.row(self.inventory())
            self.assertEqual(row['bucket'], 'keep', row)
            self.assertIn('active or pinned usage', row['reasons'])
            with self.assertRaisesRegex(cleanup.CleanupError, 'active or pinned usage'):
                self.freeze()
        self.assertTrue((self.tree / 'tracked.txt').is_file())

    def test_new_recent_usage_after_freeze_keeps_worktree(self):
        with patch.object(cleanup.worktree_recent, 'scan', return_value=self.scan_result()) as scan:
            approval = self.freeze()
            self.assertTrue(scan.called)
            scan.return_value = self.scan_result(at=123)
            report = self.apply(approval)
        self.assert_blocked(report)
        self.assertIn('recent chat usage requires review', report['blocked'][str(self.tree)])

    def test_partial_scan_after_freeze_keeps_worktree(self):
        with patch.object(cleanup.worktree_recent, 'scan', return_value=self.scan_result()) as scan:
            approval = self.freeze()
            scan.return_value = self.scan_result(status='partial')
            report = self.apply(approval)
        self.assert_blocked(report)

    def test_updated_review_cannot_expand_frozen_evidence(self):
        with patch.object(cleanup.worktree_recent, 'scan', return_value=self.scan_result(at=123)) as scan:
            old_digest = self.row(self.inventory())['recent_chat_sha256']
            self.write_holds(old_digest)
            approval = self.freeze()
            scan.return_value = self.scan_result(at=124)
            changed = self.row(self.inventory())
            self.assertEqual(changed['bucket'], 'keep', changed)
            self.assertNotEqual(changed['recent_chat_sha256'], old_digest)
            self.write_holds(changed['recent_chat_sha256'])
            self.assertEqual(self.row(self.inventory())['bucket'], 'candidate')
            report = self.apply(approval)
        self.assert_blocked(report)


if __name__ == '__main__':
    unittest.main()
