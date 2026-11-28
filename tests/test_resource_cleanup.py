import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workbench import resource_cleanup as cleanup


class SimulatorFixture:
    def __init__(self, rows=(), status='unavailable', collections=None):
        self.rows = list(rows)
        self.status = status
        self.collections = collections or {}
        self.calls = []

    def inventory(self):
        return {'status': self.status, 'tool': '/fixture/simctl' if self.status != 'unavailable' else None,
                'resources': [{**row, 'reasons':list(row['reasons'])} for row in self.rows],
                'collections':self.collections}

    def remove(self, row):
        self.calls.append(row['id'])


class ResourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / 'isolated'
        self.home.mkdir()
        self.plan = self.root / 'plan.json'
        self.receipt = self.root / 'receipt.json'
        self.sim = SimulatorFixture()

    def cache(self, relative):
        target = self.home / relative
        target.mkdir(parents=True)
        nested = target / 'nested'
        nested.mkdir()
        (nested / 'data').write_text('disposable cache data\n')
        return target

    def freeze(self, *paths):
        return cleanup.freeze(list(map(str, paths)), self.plan, home=self.home, simulator=self.sim)

    def apply(self, approved):
        return cleanup.apply(approved['plan'], approved['sha256'], out=self.receipt, home=self.home,
                             simulator=self.sim)

    def test_removes_only_exact_cache_and_keeps_external_symlink_target(self):
        chosen = self.cache('Library/Developer/Xcode/DerivedData/Build to remove')
        other = self.cache('Library/Developer/Xcode/DerivedData/Build to save')
        saved = self.root / 'user-owned.txt'
        saved.write_text('keep this file\n')
        (chosen / 'link').symlink_to(saved)
        observed = cleanup.inventory(home=self.home, simulator=self.sim, measure_size=True)
        self.assertEqual(observed['simulator']['status'], 'unavailable')
        self.assertEqual({entry['id'] for entry in observed['resources']}, {str(chosen), str(other)})
        approved = self.freeze(chosen)
        actual = self.apply(approved)
        self.assertEqual(actual['status'], 'complete', actual)
        self.assertEqual(list(actual['removed']), [str(chosen)])
        self.assertFalse(chosen.exists())
        self.assertTrue(other.is_dir())
        self.assertEqual(saved.read_text(), 'keep this file\n')
        self.assertEqual(self.apply(approved)['removed'], actual['removed'])

    def test_nested_change_invalidates_frozen_cache_even_if_root_did_not_change(self):
        target = self.cache('Library/Application Support/Cursor/snapshots/roots/scoped root')
        approved = self.freeze(target)
        original = target.stat().st_mtime_ns
        (target / 'nested/data').write_text('updated nested snapshot\n')
        self.assertEqual(target.stat().st_mtime_ns, original)
        report = self.apply(approved)
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('tree changed', report['blocked'][str(target)])
        self.assertTrue(target.is_dir())

    def test_refuses_selected_symlink_and_artifacts_inside_cache_roots(self):
        target = self.cache('Library/Developer/Xcode/iOS DeviceSupport/known')
        link = target.parent / 'edge'
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(cleanup.ResourceError, 'indirect'):
            self.freeze(link)
        with self.assertRaisesRegex(cleanup.ResourceError, 'outside cache roots'):
            cleanup.freeze([str(target)], target / 'plan.json', home=self.home, simulator=self.sim)
        self.assertTrue(target.is_dir())

    def test_process_opening_cache_after_approval_keeps_it(self):
        target = self.cache('Library/Application Support/Cursor/snapshots/roots/new hold')
        if sys.platform != 'darwin' and not sys.platform.startswith('linux'):
            self.skipTest('requires current process descriptor inspection')
        approved = self.freeze(target)
        code = 'import sys; f=open(sys.argv[1]); print("ready",flush=True); sys.stdin.read()'
        with subprocess.Popen([sys.executable,'-c',code,os.fspath(target/'nested/data')], cwd=self.root,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as process:
            try:
                self.assertEqual(process.stdout.readline().strip(), 'ready')
                report = self.apply(approved)
                self.assertIn(str(target), report['blocked'])
                self.assertTrue(target.is_dir())
            finally:
                process.stdin.close()
                process.wait(timeout=5)
        self.assertEqual(self.apply(approved)['status'], 'complete')

    def test_simulator_pending_is_observed_with_no_destructive_retry_or_partial_absence(self):
        identifier = 'simulator:runtime:abcd0000-0000-0000-0000-000000000001'
        row = {'id':identifier, 'kind':'simulator-runtime', 'identity':{'build':'fixture'}, 'path':None,
               'estimated_bytes':10, 'reasons':[]}
        self.sim = SimulatorFixture([row], 'available')
        approved = self.freeze(identifier)
        result = self.apply(approved)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(self.sim.calls, [identifier])
        alternate = cleanup.apply(approved['plan'], approved['sha256'], out=self.root / 'different-receipt.json',
                                  home=self.home, simulator=self.sim)
        self.assertEqual(alternate['status'], 'pending')
        self.assertEqual(self.sim.calls, [identifier])
        copied = self.root / 'copied-plan.json'
        copied.write_bytes(Path(approved['plan']).read_bytes())
        with self.assertRaisesRegex(cleanup.ResourceError, 'original approval path'):
            cleanup.apply(copied, approved['sha256'], out=self.root / 'different-receipt.json',
                          home=self.home, simulator=self.sim)
        self.assertEqual(self.sim.calls, [identifier])
        self.sim.status, self.sim.rows = 'partial', []
        unavailable = self.apply(approved)
        self.assertEqual(unavailable['status'], 'blocked')
        self.assertIn(identifier, unavailable['pending'])
        self.assertEqual(unavailable['removed'], {})
        self.sim.status = 'available'
        result = self.apply(approved)
        self.assertEqual(result['status'], 'complete')
        self.assertIn(identifier, result['removed'])
        self.assertEqual(self.sim.calls, [identifier])

    def test_nested_change_during_final_usage_scan_still_blocks_removal(self):
        target = self.cache('Library/Developer/Xcode/DerivedData/late file change')
        approved = self.freeze(target)
        observations = []

        def usage(paths):
            observations.append(paths)
            if len(observations) == 2:
                (target / 'nested/data').write_text('changed during live scan\n')
            return {'status':'available'}, {}

        with patch.object(cleanup.worktree_usage, 'scan', side_effect=usage):
            result = self.apply(approved)
        self.assertIn('tree changed', result['blocked'][str(target)])
        self.assertTrue(target.is_dir())

    def test_replacement_parent_cannot_confirm_a_pending_cache_removed(self):
        target = self.cache('Library/Application Support/Cursor/snapshots/roots/scoped root')
        approved = self.freeze(target)
        with patch.object(cleanup, '_remove_cache', side_effect=OSError('fixture removal interrupted')):
            first = self.apply(approved)
        self.assertIn(str(target), first['pending'])
        moved = self.root / 'retained original cache parent'
        target.parent.rename(moved)
        target.parent.mkdir()
        updated = self.apply(approved)
        self.assertEqual(updated['removed'], {})
        self.assertIn('parent changed', updated['blocked'][str(target)])
        self.assertTrue((moved / target.name).is_dir())
