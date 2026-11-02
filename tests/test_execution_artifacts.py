import copy
import json
from pathlib import Path
import tempfile
import unittest
from workbench import execution_artifacts as artifacts


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.task = self.root / 'task.json'; self.task.write_text('{}')
        self.data = {'kind': 'bug', 'surface': 'ui', 'repo': str(self.root), 'base_commit': 'base'}
        self.plan = {'execution_artifact': {'version': 1, 'language': 'javascript', 'source': '// evidence\n'}}
        for mode in artifacts.MODES:
            self.plan[mode] = {'command': ['node', artifacts.CONTAINER_PATH, mode]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_freeze_is_separate_and_exact_and_review_includes_source(self):
        ref = artifacts.freeze(self.task, self.plan, self.data, 'image')
        path = self.root / ref['path']
        self.assertEqual(path.read_bytes(), b'// evidence\n')
        self.assertEqual(artifacts.freeze(self.task, self.plan, self.data, 'image'), ref)
        self.assertIn('// evidence\n', artifacts.review(self.task, self.plan, ref, self.data, 'image', 'head'))
        self.assertEqual(ref['bytes'], len(path.read_bytes()))
        self.assertFalse(self.root.joinpath('harness.mjs').exists())

    def test_rejects_malformed_source_mode_and_commands(self):
        variants = []
        for key, value in [('version', 2), ('language', 'python'), ('source', ''),
                           ('source', '\0'), ('source', 'x' * 32769), ('host_path', '/tmp/x')]:
            p = copy.deepcopy(self.plan); p['execution_artifact'][key] = value; variants.append(p)
        for value in (None, 'not a check', [], 1):
            p = copy.deepcopy(self.plan); p['reproduction'] = value; variants.append(p)
        for command in (['node', '-e', 'fake'], ['env', 'node', artifacts.CONTAINER_PATH, 'screenshot'],
                        ['node', '--import', '/app/../tmp/x', artifacts.CONTAINER_PATH, 'screenshot'],
                        ['node', artifacts.CONTAINER_PATH, 'tests']):
            p = copy.deepcopy(self.plan); p['screenshot']['command'] = command; variants.append(p)
        for plan in variants:
            with self.subTest(plan=plan), self.assertRaises(artifacts.ArtifactError):
                artifacts.validate_plan(plan, self.data, 'image')
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.validate_plan(self.plan, {**self.data, 'kind': 'feature'}, 'image')
        for surface, mode in [('non-ui', 'image'), ('ui', 'workspace')]:
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.validate_plan(self.plan, {**self.data, 'surface': surface}, mode)

    def test_ref_mutation_file_change_symlink_and_escape_are_rejected(self):
        ref = artifacts.freeze(self.task, self.plan, self.data, 'image')
        for changed in ({**ref, 'path': '../escape.mjs'}, {**ref, 'sha256': '0' * 64}):
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.verify(self.task, self.plan, changed, self.data, 'image', 'head')
        path = self.root / ref['path']; path.chmod(0o600); path.write_text('changed')
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.verify(self.task, self.plan, ref, self.data, 'image', 'head')
        path.unlink(); target = self.root / 'source'; target.write_text('// evidence\n'); path.symlink_to(target)
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.freeze(self.task, self.plan, self.data, 'image')

    def test_loader_must_be_same_tracked_regular_file_at_base_and_head(self):
        import subprocess
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.root), *args], text=True).strip()
        git('init', '-q'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
        (self.root / 'loader.mjs').write_text('// loader\n')
        git('add', 'loader.mjs'); git('commit', '-qm', 'loader fixture')
        self.data['base_commit'] = git('rev-parse', 'HEAD')
        for mode in artifacts.MODES:
            self.plan[mode]['command'] = ['node', '--import', '/app/loader.mjs', artifacts.CONTAINER_PATH, mode]
        ref = artifacts.freeze(self.task, self.plan, self.data, 'image')
        self.assertEqual(artifacts.verify(self.task, self.plan, ref, self.data, 'image', 'HEAD')['loader']['path'], '/app/loader.mjs')
        (self.root / 'loader.mjs').unlink(); (self.root / 'loader.mjs').symlink_to('target')
        git('add', 'loader.mjs'); git('commit', '-qm', 'symlink fixture')
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.verify(self.task, self.plan, ref, self.data, 'image', 'HEAD')
        self.plan['tests']['command'] = ['node', artifacts.CONTAINER_PATH, 'tests']
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.validate_plan(self.plan, self.data, 'image')
