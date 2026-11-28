import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from workbench import benny_deploy, cli


class BennyDeployTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'project'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-b', 'main')
        self._commit('Initial project', '--allow-empty')
        self.pack = self.repo / benny_deploy.PACK

    def _commit(self, message, *options):
        cli.git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                'commit', '-m', message, *options)
        return cli.sha(self.repo, 'HEAD')

    def _install_and_commit(self):
        receipt = benny_deploy.install(self.repo)
        cli.git(self.repo, 'add', '--', str(benny_deploy.PACK))
        self._commit('Commit the Benny package')
        return receipt

    def test_readiness_from_a_fresh_checkout_requires_all_committed_files(self):
        plan = benny_deploy.preview(self.repo)
        self.assertEqual(plan['status'], 'ready-to-install')
        self.assertFalse(self.pack.exists())
        self.pack.mkdir(parents=True)
        (self.pack / 'user-note.md').write_text('Preserve this local file.\n')
        installed = benny_deploy.install(self.repo)
        with self.assertRaisesRegex(benny_deploy.DeployError, 'commit the regular'):
            benny_deploy.verify(self.repo)
        self.assertEqual(benny_deploy.install(self.repo)['create'], [])
        self.assertEqual((self.pack / 'user-note.md').read_text(), 'Preserve this local file.\n')
        cli.git(self.repo, 'add', '--', *installed['create'])
        revision = self._commit('Commit approved files')
        clone = self.root / 'fresh'
        cli.git(self.root, 'clone', '--quiet', str(self.repo), str(clone))
        self.assertFalse((clone / benny_deploy.PACK / 'user-note.md').exists())
        result = subprocess.run(
            [sys.executable, '-m', 'workbench.benny_deploy', 'verify', '--repo', '.'],
            cwd=clone, env={**os.environ, 'PYTHONPATH': str(benny_deploy.INSTALL_ROOT)},
            capture_output=True, check=True, timeout=40,
        )
        ready = json.loads(result.stdout)
        self.assertEqual((ready['repo'], ready['revision'], ready['status']), (str(clone), revision, 'ready'))
        self.assertEqual(ready['manifest_sha256'], installed['manifest_sha256'])
        self.assertEqual({role['trigger'] for role in ready['roles'].values()},
                         {'new-top-level-report', 'trusted-triage-reply'})
        self.assertEqual(len(ready['dependencies']), 9)
        pack = clone / benny_deploy.PACK
        manifest = json.loads((pack / benny_deploy.MANIFEST).read_text())
        self.assertEqual(manifest['activation'], 'operator-configured')
        for path, digest in manifest['files'].items():
            self.assertEqual(hashlib.sha256((pack / path).read_bytes()).hexdigest(), digest)

    def test_install_does_not_replace_a_managed_local_edit(self):
        path = self.pack / 'FOR_AGENTS.md'
        self.pack.mkdir(parents=True)
        path.write_text('Operator edits.\n')
        preview = benny_deploy.preview(self.repo)
        self.assertEqual(preview['conflicts'], [str(benny_deploy.PACK / path.name)])
        with self.assertRaisesRegex(benny_deploy.DeployError, 'conflicting managed'):
            benny_deploy.install(self.repo)
        self.assertEqual(list(self.pack.iterdir()), [path])
        self.assertEqual(path.read_text(), 'Operator edits.\n')

    def test_package_directory_and_file_symlinks_never_write_outside(self):
        outside = self.root / 'unrelated'
        outside.mkdir()
        (self.repo / '.iboard').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(benny_deploy.DeployError, 'real directory'):
            benny_deploy.install(self.repo)
        self.assertEqual(list(outside.iterdir()), [])
        (self.repo / '.iboard').unlink()
        self.pack.mkdir(parents=True)
        target = outside / 'private'
        target.write_text('Local configuration.\n')
        (self.pack / 'FOR_AGENTS.md').symlink_to(target)
        with self.assertRaisesRegex(benny_deploy.DeployError, 'safe regular file'):
            benny_deploy.install(self.repo)
        self.assertEqual(target.read_text(), 'Local configuration.\n')
        cli.git(self.repo, 'add', '--', str(benny_deploy.PACK / 'FOR_AGENTS.md'))
        self._commit('Reject a symbolic package')
        with self.assertRaisesRegex(benny_deploy.DeployError, 'commit the regular'):
            benny_deploy.verify(self.repo)

    def test_readiness_binds_one_commit_and_ignores_uncommitted_files(self):
        installed = self._install_and_commit()
        accepted = cli.sha(self.repo, 'HEAD')
        path = self.pack / benny_deploy.ROLES['reproduce']['instruction']
        path.write_text('Altered instructions.\n')
        self.assertEqual(benny_deploy.verify(self.repo)['manifest_sha256'], installed['manifest_sha256'])
        cli.git(self.repo, 'add', '--', str(path))
        self._commit('Change a managed instruction')
        with self.assertRaisesRegex(benny_deploy.DeployError, 'differs from'):
            benny_deploy.verify(self.repo)
        self.assertEqual(benny_deploy.verify(self.repo, accepted)['revision'], accepted)
        with self.assertRaisesRegex(benny_deploy.DeployError, 'full Git commit'):
            benny_deploy.verify(self.repo, accepted[:12])

    def test_missing_native_dependency_blocks_readiness(self):
        self._install_and_commit()
        real_find = benny_deploy.importlib.util.find_spec

        def find(name):
            return None if name == 'workbench.tdd' else real_find(name)

        with patch.object(benny_deploy.importlib.util, 'find_spec', side_effect=find):
            with self.assertRaisesRegex(benny_deploy.DeployError, 'workbench.tdd'):
                benny_deploy.verify(self.repo)


if __name__ == '__main__':
    unittest.main()
