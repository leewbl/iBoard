import json
from pathlib import Path
import tempfile
import unittest

from workbench import cli, project_verify


IMAGE = 'sha256:' + 'a' * 64


class ProjectVerificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'app.txt').write_text('\nregistered doctor\nregistered first\nregistered second\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'fixture')
        self.revision = cli.sha(self.repo, 'HEAD')
        self.helpers = self.root / 'helpers'
        self.helpers.mkdir()
        self.sequence = []
        self.fail = set()
        self.no_cleanup = set()
        checks = []
        for name in ('doctor', 'first', 'second'):
            marker = f'JOURNEY_{name.upper()}_PASS'
            helper = name + '.sh'
            (self.helpers / helper).write_text(f"printf '%s\\n' '{marker}'\n")
            checks.append({'id': name, 'helper': helper, 'marker': marker, 'reach': 'Open the app.',
                           'drive': 'Run a user command.', 'observe': 'Inspect the result.',
                           'gotchas': 'A fixture instance is required.',
                           'source': [{'path': 'app.txt', 'contains': f'registered {name}'}]})
        self.contract = {'project': 'fixture', 'surface': 'Fixture CLI', 'launch': 'Use an isolated workdir.',
                         'cleanup': 'Remove test data.', 'doctor': checks[0], 'features': checks[1:]}
        self.manifest = self.root / 'input.json'
        self.manifest.write_text(json.dumps(self.contract))

    def attester(self, repo, image, timeout, *, commit):
        self.assertEqual(repo, self.repo)
        self.assertEqual(commit, self.revision)
        self.assertEqual(image, 'local-image')
        return IMAGE

    def sandbox(self, scratch, image, command, timeout, **opts):
        self.assertEqual(image, IMAGE)
        self.assertEqual(opts['source_root'], '/app')
        self.assertTrue(opts['workspace_read_only'])
        helper = Path(command[-1]).name
        self.assertEqual([path.name for path in Path(scratch).iterdir()], [helper])
        key = Path(helper).stem
        self.sequence.append(key)
        return {'image': image, 'command': command, 'workspace_read_only': True,
                'container_created': True, 'cleanup_confirmed': key not in self.no_cleanup,
                'exit_code': int(key in self.fail), 'stdout': f'JOURNEY_{key.upper()}_PASS\n'}

    def run_mode(self, mode, name, **kwargs):
        return project_verify.run(mode, repo=self.repo, revision=self.revision, image='local-image',
                                  out=self.root / name, memory_mb=512,
                                  sandbox=self.sandbox, attester=self.attester, **kwargs)

    def create(self):
        result = self.run_mode('create', 'created', manifest=self.manifest, helpers=self.helpers, journey='second')
        self.assertEqual(result['outcome'], 'verified')
        return self.root / 'created' / result['bundle']

    def test_generation_proves_one_feature_and_anchors_every_feature(self):
        bundle = self.create()
        self.assertEqual(self.sequence, ['doctor', 'second'])
        self.assertTrue((bundle / 'helpers' / 'first.sh').stat().st_mode & 0o111)
        self.assertIn('(second.md)', (bundle / 'features' / 'README.md').read_text())
        report = cli.read(bundle.parent / 'result.json')
        self.assertEqual(report['source']['first'][0]['line'], 3)
        for row in report['runs']:
            self.assertEqual(cli.digest(bundle.parent / row['report']), row['sha256'])
        self.assertEqual(cli.git(self.repo, 'status', '--porcelain'), '')

    def test_map_drift_requires_all_journeys_to_pass_before_exporting_a_correction(self):
        bundle = self.create()
        (bundle / 'features' / 'README.md').write_text('# Missing map\n')
        blocked = self.run_mode('maintain', 'drift', skill=bundle)
        self.assertEqual(blocked['outcome'], 'blocked')
        self.assertEqual(blocked['map_drift'], ['features/README.md'])
        self.assertEqual(blocked['features'], {'first': 'passed', 'second': 'passed'})
        self.fail.add('first')
        failed = self.run_mode('maintain', 'failed-fix', skill=bundle, manifest=self.manifest, helpers=self.helpers)
        self.assertEqual(failed['outcome'], 'blocked')
        self.assertEqual(failed['bundle'], 'draft')
        self.assertEqual(failed['features'], {'first': 'behavior-unverified', 'second': 'passed'})
        self.assertEqual(self.sequence[-4:], ['doctor', 'first', 'doctor', 'second'])
        self.assertFalse((self.root / 'failed-fix' / 'changes.patch').exists())
        self.fail.clear()
        passed = self.run_mode('maintain', 'fixed', skill=bundle, manifest=self.manifest, helpers=self.helpers)
        self.assertEqual(passed['outcome'], 'changed')
        self.assertEqual(passed['replaces_unsafe_entries'], [])
        self.assertIn('after/features/README.md', (self.root / 'fixed' / 'changes.patch').read_text())
        self.assertEqual((bundle / 'features' / 'README.md').read_text(), '# Missing map\n')
        clean = self.run_mode('maintain', 'clean', skill=self.root / 'fixed' / passed['bundle'])
        self.assertEqual(clean['outcome'], 'clean')

    def test_missing_source_or_cleanup_blocks_release_despite_pass_marker(self):
        self.contract['features'][0]['source'][0]['contains'] = 'removed anchor'
        self.manifest.write_text(json.dumps(self.contract))
        self.no_cleanup.add('second')
        result = self.run_mode('create', 'blocked', manifest=self.manifest, helpers=self.helpers, journey='second')
        self.assertEqual(result['outcome'], 'blocked')
        self.assertEqual(result['source_drift'], ['first'])
        self.assertEqual(result['features'], {'second': 'behavior-unverified'})
        self.assertFalse((self.root / 'blocked' / 'verify-fixture').exists())

    def test_symlinked_helper_directory_is_neither_read_nor_executed(self):
        bundle = self.create()
        (bundle / 'helpers').rename(bundle / 'relocated')
        (bundle / 'helpers').symlink_to(self.helpers, target_is_directory=True)
        self.sequence.clear()
        result = self.run_mode('maintain', 'blocked', skill=bundle)
        self.assertEqual(result['outcome'], 'blocked')
        self.assertIn('helpers', result['map_drift'])
        self.assertEqual(self.sequence, [])

    def test_missing_feature_helper_is_recorded_and_doctor_allows_the_next_journey(self):
        bundle = self.create()
        (bundle / 'helpers' / 'first.sh').unlink()
        result = self.run_mode('maintain', 'missing-helper', skill=bundle)
        self.assertEqual(result['outcome'], 'blocked')
        self.assertEqual(self.sequence[-3:], ['doctor', 'doctor', 'second'])
        failed = cli.read(self.root / 'missing-helper' / 'first.json')
        self.assertEqual(failed['diagnostic'], 'harness-input')
        self.assertTrue(result['features']['second'] == 'passed')
