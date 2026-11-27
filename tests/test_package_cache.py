import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workbench import package_cache
from workbench import poteto_mode
from workbench.poteto_routes import PLAYBOOKS


class PackageCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.plan = self.root / 'plan.json'
        self.receipt = self.root / 'receipt.json'

    def write_file(self, relative, data='cache sample'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def apply(self, approval, receipt=None):
        return package_cache.apply(self.plan, approval['sha256'], out=receipt or self.receipt)

    @unittest.skipUnless(shutil.which('uv'), 'requires local uv')
    def test_native_uv_only_prunes_approved_cache_and_records_evidence(self):
        stale = self.write_file('uv/unused-v999/sample')
        kept = self.write_file('other/unused-v999/sample')
        tag = self.write_file('uv/CACHEDIR.TAG', package_cache.CACHE_SIGNATURE.decode() + '\n')
        approval = package_cache.freeze('uv', cache_dir=self.root / 'uv', out=self.plan)
        result = self.apply(approval)
        self.assertEqual(result['status'], 'complete', result)
        self.assertLess(result['observed_cache_bytes_delta'], 0)
        self.assertFalse(stale.exists())
        self.assertTrue(kept.is_file())
        self.assertTrue(tag.is_file())
        stale = self.write_file('uv/unused-v999/recreated')
        another_receipt = self.root / 'replay.json'
        replay = self.apply(approval, another_receipt)
        self.assertEqual(result['sha256'], replay['sha256'])
        self.assertTrue(stale.is_file())

    @unittest.skipUnless(shutil.which('uv'), 'requires local uv')
    def test_approval_rejects_linked_untagged_and_changed_cache(self):
        marker = self.write_file('uv/CACHEDIR.TAG')
        with self.assertRaisesRegex(ValueError, 'signature'):
            package_cache.freeze('uv', cache_dir=marker.parent, out=self.plan)
        marker.write_text(package_cache.CACHE_SIGNATURE.decode())
        link = self.root / 'linked'
        link.symlink_to(marker.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'indirect'):
            package_cache.freeze('uv', cache_dir=link, out=self.plan)
        approval = package_cache.freeze('uv', cache_dir=marker.parent, out=self.plan)
        added = self.write_file('uv/unused-v999/added')
        with self.assertRaisesRegex(ValueError, 'contents or plan changed'):
            self.apply(approval)
        self.assertTrue(added.is_file())
        self.assertFalse(self.receipt.exists())

    @unittest.skipUnless(shutil.which('uv'), 'requires local uv')
    def test_second_approval_cannot_run_concurrently_against_the_same_cache(self):
        self.write_file('uv/CACHEDIR.TAG', package_cache.CACHE_SIGNATURE.decode())
        approval = package_cache.freeze('uv', cache_dir=self.root / 'uv', out=self.plan)
        with package_cache._cache_locks(json.loads(self.plan.read_text())):
            with self.assertRaisesRegex(ValueError, 'another cleanup'):
                self.apply(approval)
        self.assertEqual(self.apply(approval)['status'], 'complete')

    @unittest.skipUnless(shutil.which('uv'), 'requires local uv')
    def test_unknown_native_outcome_is_never_reissued(self):
        self.write_file('uv/CACHEDIR.TAG', package_cache.CACHE_SIGNATURE.decode())
        approval = package_cache.freeze('uv', cache_dir=self.root / 'uv', out=self.plan)
        with patch.object(package_cache, '_issue', side_effect=package_cache.ResourceError('unexpected exit')) as call:
            first = self.apply(approval)
            second = self.apply(approval, self.root / 'other.json')
        self.assertEqual(call.call_count, 1)
        self.assertEqual(first['sha256'], second['sha256'])
        self.assertEqual(first['status'], 'pending')
        self.assertEqual(first['detail'], 'unexpected exit')
        self.assertEqual(json.loads(self.receipt.read_text())['roots'], {'cache': str(self.root / 'uv')})

    @unittest.skipUnless(shutil.which('pnpm'), 'requires local pnpm')
    def test_native_pnpm_prunes_only_explicit_store_and_metadata(self):
        metadata = self.write_file('pnpm-meta/metadata-v999/sample')
        kept = self.write_file('other/metadata-v999/sample')
        scopes = {'store_dir': self.root / 'store', 'cache_dir': self.root / 'pnpm-meta'}
        observed = package_cache.inventory('pnpm', **scopes)
        versioned = Path(observed['roots']['store'])
        versioned.mkdir(parents=True)
        stale = self.write_file(versioned.relative_to(self.root) / 'tmp/sample')
        approval = package_cache.freeze('pnpm', out=self.plan, **scopes)
        result = self.apply(approval)
        self.assertEqual(result['status'], 'complete', result)
        self.assertFalse(stale.exists())
        self.assertFalse(metadata.exists())
        self.assertTrue(kept.is_file())

    @unittest.skipUnless(shutil.which('brew'), 'requires local Homebrew')
    def test_native_brew_directory_discovers_only_completed_downloads(self):
        download = self.write_file('brew/downloads/' + 'a' * 64 + '--sample.tar.gz')
        kept = self.write_file('brew/downloads/' + 'b' * 64 + '--keep.tar.gz')
        incomplete = self.write_file('brew/downloads/' + 'c' * 64 + '--sample.incomplete')
        unrelated = self.write_file('brew/Cask/installed')
        options = {'cache_dir': self.root / 'brew'}
        self.assertEqual(package_cache.inventory('brew', **options)['downloads'], [str(download), str(kept)])
        with self.assertRaisesRegex(ValueError, 'exact Homebrew download'):
            package_cache.freeze('brew', out=self.plan, **options)
        approval = package_cache.freeze('brew', select=str(download), out=self.plan, **options)
        self.assertEqual(self.apply(approval)['status'], 'complete')
        self.assertFalse(download.exists())
        self.assertTrue(kept.is_file() and incomplete.is_file() and unrelated.is_file())

    @unittest.skipUnless(shutil.which('yarn'), 'requires native Yarn')
    def test_native_yarn_preserves_project_cache_and_global_siblings(self):
        binary = shutil.which('yarn')
        version = package_cache._run(binary, '--version')[0]
        project = self.write_file('project/.yarn/cache/keep.zip')
        packages = self.root / 'packages'
        if version.startswith('1.'):
            stale = self.write_file('packages/v6/entry/cache')
            other_version = self.write_file('packages/v5/entry/cache')
        else:
            stale = self.write_file('packages/cache/example.zip')
            kept = self.write_file('packages/dlx/keep')
        approval = package_cache.freeze('yarn', cache_dir=packages, out=self.plan)
        result = self.apply(approval)
        self.assertEqual(result['status'], 'complete', result)
        self.assertTrue(project.is_file())
        self.assertFalse(stale.exists())
        if version.startswith('1.'):
            self.assertFalse(other_version.exists())
        else:
            self.assertTrue(kept.is_file())

    def test_playbook_distinguishes_observation_and_approved_apply(self):
        actions = PLAYBOOKS['worktree-cleanup'].actions
        self.assertEqual(actions['package-cache-inventory'].effect, 'observe')
        self.assertEqual(actions['package-cache-freeze'].effect, 'local')
        self.assertEqual(actions['package-cache-apply'].effect, 'approved-cleanup')

    @unittest.skipUnless(shutil.which('uv'), 'requires local uv')
    def test_poteto_plan_passes_the_frozen_approval_digest_through_to_native_apply(self):
        stale = self.write_file('uv/unused-v999/stale')
        self.write_file('uv/CACHEDIR.TAG', package_cache.CACHE_SIGNATURE.decode())
        request = self.write_file('request.txt', 'Prune the approved temporary uv cache.')
        selected = poteto_mode.select(request=request, playbook='worktree-cleanup', out=self.root / 'selected.json')
        reference = {'step': 1, 'path': str(self.plan)}
        manifest = {'playbook': 'worktree-cleanup', 'seconds': 40, 'steps': [
            {'action': 'package-cache-freeze',
             'args': ['--manager', 'uv', '--cache-dir', str(stale.parents[1]), '--out', str(self.plan)],
             'outputs': [str(self.plan)]},
            {'action': 'package-cache-apply',
             'args': ['--plan', {**reference, 'value': 'path'}, '--sha256', {**reference, 'value': 'sha256'},
                      '--out', str(self.receipt)],
             'outputs': [str(self.receipt)],
             'expect': [{'path': str(self.receipt), 'field': ['status'], 'equals': 'complete'}]},
        ]}
        file = self.write_file('workflow.json', json.dumps(manifest))
        frozen = poteto_mode.freeze(selection=selected['path'], sha256=selected['sha256'],
                                    manifest=file, out=self.root / 'workflow')
        command = [sys.executable, '-m', 'workbench.poteto_mode', 'run',
                   '--plan', frozen['path'], '--sha256', frozen['sha256']]
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.receipt.read_text())['status'], 'complete')
        self.assertFalse(stale.exists())


if __name__ == '__main__':
    unittest.main()
