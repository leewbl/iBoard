from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, worktree_cleanup as cleanup


THREAD_ID = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
CHILD_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
UNRELATED_ID = 'cccccccc-cccc-cccc-cccc-cccccccccccc'


class WorktreeCodexPinGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'repository'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'tracked.txt').write_text('independent fixture\n', encoding='utf-8')
        cli.git(self.repo, 'add', 'tracked.txt')
        cli.git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Store fixture contents')
        self.head = cli.sha(self.repo, 'HEAD')
        self.tree = self.root / 'candidate worktree'
        cli.git(self.repo, 'worktree', 'add', '-b', 'candidate', str(self.tree), self.head)
        self.holds = self.root / 'holds.json'
        cli.write(self.holds, {'schema': cli.SCHEMA, 'entries': []})
        self.plan = self.root / 'plan.json'
        self.receipt = self.root / 'receipt.json'
        self.initial = self.root / 'initial-pins.json'
        self.refreshed = self.root / 'refreshed-pins.json'
        self.approval_time = datetime.now(timezone.utc)
        self.initial_time = self.approval_time - timedelta(seconds=10)
        self.refreshed_time = self.approval_time + timedelta(microseconds=1)
        self.snapshot(self.initial, self.initial_time)
        live_usage = patch.object(cleanup.worktree_usage, 'scan',
                                  return_value=({'backend': 'fixture', 'status': 'available'}, {}))
        live_usage.start()
        self.addCleanup(live_usage.stop)

    def snapshot(self, path, instant, *, host='local-host', repo=None, status='available', threads=None):
        cli.write(path, {'schema': cli.SCHEMA, 'source': 'codex-app:list_threads',
                         'repo': str(self.repo if repo is None else repo), 'host_id': host,
                         'captured_at': instant.isoformat(), 'status': status, 'threads': threads or []})

    def pinned_scan(self):
        return {'status': 'available'}, {str(self.tree): [
            {'thread_id': THREAD_ID, 'session_id': CHILD_ID, 'access': 'path-reference'},
        ]}

    def empty_scan(self):
        return {'status': 'available'}, {}

    def inventory(self, snapshot):
        return cleanup.inventory(self.repo, 'main', holds=self.holds, codex_pins=snapshot)

    def freeze(self, snapshot):
        with patch.object(cli, 'now', return_value=self.approval_time.isoformat()):
            return cleanup.freeze(self.repo, 'main', [str(self.tree)], self.plan, holds=self.holds,
                                  codex_pins=snapshot)

    def apply(self, approval, snapshot):
        return cleanup.apply(approval['plan'], approval['sha256'], holds=self.holds,
                             out=self.receipt, codex_pins=snapshot)

    def row(self, snapshot):
        return next(row for row in snapshot['worktrees'] if row['path'] == str(self.tree))

    def assert_worktree_retained(self):
        self.assertEqual((self.tree / 'tracked.txt').read_text(encoding='utf-8'), 'independent fixture\n')
        self.assertEqual(cli.sha(self.repo, 'refs/heads/candidate'), self.head)

    def assert_preflight_blocks(self, approval, snapshot):
        try:
            report = self.apply(approval, snapshot)
        except cleanup.CleanupError:
            pass
        else:
            self.assertEqual(report['status'], 'blocked', report)
            self.assertEqual(report['removed'], {})
        self.assert_worktree_retained()

    def test_pinned_descendant_evidence_protects_linked_worktree(self):
        self.snapshot(self.initial, self.initial_time, threads=[{'id': THREAD_ID, 'cwd': str(self.repo)}])
        result = self.pinned_scan()
        with patch.object(cleanup.worktree_pins, 'scan', return_value=result) as scan:
            row = self.row(self.inventory(self.initial))
            self.assertEqual(row['codex_pin_matches'], result[1][str(self.tree)])
            self.assertEqual(row['bucket'], 'keep', row)
            self.assertIn('pinned Codex session', row['reasons'])
            paths, snapshot = scan.call_args.args
            self.assertEqual(list(paths), [str(self.repo), str(self.tree)])
            self.assertEqual(snapshot['host_id'], 'local-host')
            self.assertEqual(snapshot['threads'], [{'id': THREAD_ID, 'cwd': str(self.repo)}])
            with self.assertRaisesRegex(cleanup.CleanupError, 'pinned Codex session'):
                self.freeze(self.initial)
        self.assertFalse(self.plan.exists())
        self.assert_worktree_retained()

    def test_partial_transcript_observation_cannot_clear_worktree(self):
        with patch.object(cleanup.worktree_pins, 'scan', return_value=({'status': 'partial'}, {})):
            row = self.row(self.inventory(self.initial))
            self.assertEqual(row['bucket'], 'keep', row)
            self.assertIn('Codex pin usage could not be verified', row['reasons'])
            with self.assertRaisesRegex(cleanup.CleanupError, 'Codex pin usage could not be verified'):
                self.freeze(self.initial)
        self.assertFalse(self.plan.exists())
        self.assert_worktree_retained()

    def test_real_transcript_scan_follows_pinned_descendant_in_another_project(self):
        other = self.root / 'candidate worktree-extra'
        cli.git(self.repo, 'worktree', 'add', '-b', 'other', str(other), self.head)
        codex = self.root / 'codex-fixture'
        archived = codex / 'archived_sessions'
        active = codex / 'sessions' / '2025' / '01' / '01'
        outside = self.root / 'other-project'

        def transcript(directory, ident, *events, **metadata):
            directory.mkdir(parents=True, exist_ok=True)
            header = {'type': 'session_meta', 'payload': {'id': ident, 'cwd': str(outside), **metadata}}
            text = '\n'.join(json.dumps(event) for event in (header, *events)) + '\n'
            (directory / f'rollout-{ident}.jsonl').write_text(text, encoding='utf-8')

        actions = {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command',
                   'arguments': json.dumps({'cmd': 'fixture command details', 'workdir': str(self.tree)})}}
        transcript(archived, THREAD_ID)
        transcript(active, CHILD_ID, actions, parent_thread_id=THREAD_ID)
        transcript(active, UNRELATED_ID, actions, cwd=str(other))
        self.snapshot(self.initial, self.initial_time, threads=[{'id': THREAD_ID, 'cwd': None}])
        with patch.dict(os.environ, CODEX_HOME=str(codex)):
            snapshot = self.inventory(self.initial)
            row = self.row(snapshot)
            self.assertEqual(row['codex_pin_matches'], self.pinned_scan()[1][str(self.tree)])
            other_row = next(entry for entry in snapshot['worktrees'] if entry['path'] == str(other))
            self.assertEqual(other_row['bucket'], 'candidate', other_row)
            self.assertNotIn('fixture command details', json.dumps(snapshot))
            self.assertNotIn(UNRELATED_ID, json.dumps(snapshot))
            with self.assertRaisesRegex(cleanup.CleanupError, 'pinned Codex session'):
                self.freeze(self.initial)
        self.assert_worktree_retained()

    def test_apply_requires_snapshot_strictly_newer_than_approval(self):
        with patch.object(cleanup.worktree_pins, 'scan', return_value=self.empty_scan()):
            approval = self.freeze(self.initial)
            self.assert_preflight_blocks(approval, None)
            self.assert_preflight_blocks(approval, self.initial)
            self.snapshot(self.refreshed, self.approval_time)
            self.assert_preflight_blocks(approval, self.refreshed)

    def test_reimported_native_export_retains_original_observation_time(self):
        source = self.root / 'native.json'
        initial_stamp = self.initial_time.isoformat().replace('+00:00', 'Z')
        cli.write(source, {'schemaVersion': 4, 'observedAt': initial_stamp, 'pinnedThreads': [],
                           'unavailableHosts': [], 'unavailableSources': []})
        exported = self.root / 'first-export.json'
        cleanup.worktree_pins.capture(self.repo, source, 'local-host', exported)
        approval = self.freeze(exported)
        cleanup.worktree_pins.capture(self.repo, source, 'local-host', self.refreshed)
        self.assertEqual(cli.read(self.refreshed)['captured_at'], initial_stamp)
        self.assert_preflight_blocks(approval, self.refreshed)

    def test_apply_rejects_snapshot_scoped_to_another_host_or_repository(self):
        with patch.object(cleanup.worktree_pins, 'scan', return_value=self.empty_scan()):
            approval = self.freeze(self.initial)
            for scope in ({'host': 'other-host'}, {'repo': self.root / 'other-repository'}):
                with self.subTest(scope=scope):
                    self.snapshot(self.refreshed, self.refreshed_time, **scope)
                    self.assert_preflight_blocks(approval, self.refreshed)

    def test_new_pin_after_approval_blocks_the_approved_worktree(self):
        with patch.object(cleanup.worktree_pins, 'scan', return_value=self.empty_scan()) as scan:
            approval = self.freeze(self.initial)
            self.snapshot(self.refreshed, self.refreshed_time, threads=[{'id': THREAD_ID, 'cwd': str(self.repo)}])
            scan.return_value = self.pinned_scan()
            report = self.apply(approval, self.refreshed)
            self.assertGreaterEqual(scan.call_count, 2)
        self.assertEqual(report['status'], 'blocked', report)
        self.assertEqual(report['removed'], {})
        self.assertIn('pinned Codex session', report['blocked'][str(self.tree)])
        self.assert_worktree_retained()

    def test_apply_blocks_when_refreshed_observation_is_partial(self):
        with patch.object(cleanup.worktree_pins, 'scan', return_value=self.empty_scan()) as scan:
            approval = self.freeze(self.initial)
            self.snapshot(self.refreshed, self.refreshed_time, status='partial')
            scan.return_value = {'status': 'partial'}, {}
            self.assert_preflight_blocks(approval, self.refreshed)

    def test_refreshed_unpinned_snapshot_removes_only_approved_worktree(self):
        other = self.root / 'candidate worktree-extra'
        cli.git(self.repo, 'worktree', 'add', '-b', 'other', str(other), self.head)
        with patch.object(cleanup.worktree_pins, 'scan', return_value=self.empty_scan()):
            approval = self.freeze(self.initial)
            self.snapshot(self.refreshed, self.refreshed_time)
            report = self.apply(approval, self.refreshed)
        self.assertEqual(report['status'], 'complete', report)
        self.assertEqual(set(report['removed']), {str(self.tree)})
        self.assertFalse(self.tree.exists())
        self.assertTrue((other / 'tracked.txt').is_file())
        self.assertEqual(cli.sha(self.repo, 'refs/heads/candidate'), self.head)

    def test_plan_without_codex_scope_remains_compatible(self):
        with patch.object(cleanup.worktree_pins, 'scan') as scan:
            approval = self.freeze(None)
            report = self.apply(approval, None)
            scan.assert_not_called()
        self.assertEqual(report['status'], 'complete', report)
        self.assertFalse(self.tree.exists())
        self.assertEqual(cli.sha(self.repo, 'refs/heads/candidate'), self.head)


if __name__ == '__main__':
    unittest.main()
