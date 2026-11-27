import json
from pathlib import Path
import tempfile
import unittest

from workbench import cli, project_triage


BASE_IMAGE = 'sha256:' + 'a' * 64
CANDIDATE_IMAGE = 'sha256:' + 'b' * 64


class ProjectTriageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'app.txt').write_text('old behavior\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'known good')
        self.base = cli.sha(self.repo, 'HEAD')
        (self.repo / 'app.txt').write_text('new behavior\n')
        cli.git(self.repo, 'commit', '-qam', 'candidate')
        self.revision = cli.sha(self.repo, 'HEAD')
        self.helpers = self.root / 'helpers'
        self.helpers.mkdir()
        (self.helpers / 'doctor.sh').write_text('printf DOCTOR_PASS\n')
        (self.helpers / 'main.sh').write_text('printf MAIN_PASS\n')
        self.oracle = self.root / 'oracle.sh'
        self.oracle.write_text('printf ORACLE_PASS\n')
        fields = {'reach': 'Open CLI.', 'drive': 'Read named item.', 'observe': 'Item is listed.',
                  'gotchas': 'Use test data.', 'source': [{'path': 'app.txt', 'contains': 'behavior'}]}
        doctor = {**fields, 'id': 'doctor', 'helper': 'doctor.sh', 'marker': 'DOCTOR_PASS'}
        main = {**fields, 'id': 'main', 'helper': 'main.sh', 'marker': 'MAIN_PASS', 'failure_marker': 'MAIN_FAIL'}
        self.manifest = self.root / 'contract.json'
        self.manifest.write_text(json.dumps({'project': 'fixture', 'surface': 'CLI', 'launch': 'Launch CLI.',
                                             'cleanup': 'Remove test data.', 'doctor': doctor, 'features': [main]}))
        self.states = {}
        self.calls = []
        self.derivations = []
        self.reuse_container = False

    def attester(self, repo, image, timeout, *, commit):
        self.assertEqual(commit, self.base)
        self.assertEqual(repo, self.repo)
        return BASE_IMAGE

    def derive(self, repo, base, head, image_id, timeout):
        self.assertEqual((repo, base, head, image_id), (self.repo, self.base, self.revision, BASE_IMAGE))
        self.derivations.append(head)
        return CANDIDATE_IMAGE

    def sandbox(self, workspace, image, argv, timeout, **kwargs):
        self.assertIn(image, {BASE_IMAGE, CANDIDATE_IMAGE})
        self.assertTrue(kwargs['workspace_read_only'])
        self.assertEqual(kwargs['source_root'], '/app')
        script = Path(argv[-1]).name
        self.assertEqual([item.name for item in Path(workspace).iterdir()], [script])
        key = {'doctor.sh': 'DOCTOR', 'main.sh': 'MAIN', 'independent-probe.sh': 'ORACLE'}[script]
        self.calls.append((image, key))
        action = self.states.get((image, key), 'pass')
        output = {
            'pass': (0, key + '_PASS', True, False),
            'observed': (1, key + '_FAIL', True, False),
            'crash': (1, 'process aborted', True, False),
            'lost-cleanup': (1, key + '_FAIL', False, False),
            'truncated': (1, key + '_FAIL', True, True),
        }
        exit_code, line, cleanup, truncated = output[action]
        return {'image': image, 'command': argv, 'workspace_read_only': True, 'container_created': True,
                'cleanup_confirmed': cleanup, 'stdout': line + '\n', 'exit_code': exit_code,
                'stdout_truncated': truncated, 'stderr_truncated': False, 'source_root': '/app',
                'memory_mb': kwargs['memory_mb'], 'timed_out': False, 'create_exit_code': 0,
                'create_stdout': f'{1 if self.reuse_container else len(self.calls):064x}\n',
                'runtime_diagnostics': {'state':{'status':'exited', 'exit_code':exit_code, 'oom_killed':False}}}

    def invoke(self, name, **override):
        args = dict(repo=self.repo, base=self.base, revision=self.revision, image='runtime:local',
                    manifest=self.manifest, helpers=self.helpers, journey='main', oracle=self.oracle,
                    oracle_marker='ORACLE_PASS', oracle_failure_marker='ORACLE_FAIL', out=self.root / name,
                    memory_mb=512, attester=self.attester, sandbox=self.sandbox, derive=self.derive)
        return project_triage.run(**(args | override))

    def test_real_revision_paths_require_repeated_confirmed_failures_for_regression(self):
        self.states[(CANDIDATE_IMAGE, 'MAIN')] = self.states[(CANDIDATE_IMAGE, 'ORACLE')] = 'observed'
        result = self.invoke('regression')
        self.assertEqual(result['outcome'], 'product-regression')
        self.assertEqual(result['comparison'], 'source-overlay')
        self.assertEqual(self.derivations, [self.revision])
        self.assertEqual(self.calls.count((CANDIDATE_IMAGE, 'DOCTOR')), 5)
        for side, details in result['rounds'].items():
            for row in details['runs']:
                report = self.root / 'regression' / side / row['report']
                self.assertEqual(cli.digest(report), row['sha256'])
        self.assertEqual(cli.git(self.repo, 'status', '--porcelain'), '')

    def test_same_image_controls_show_harness_gap_without_product_regression(self):
        self.states[(BASE_IMAGE, 'MAIN')] = 'observed'
        result = self.invoke('driver', revision=self.base)
        self.assertEqual(result['outcome'], 'harness-gap')
        self.assertEqual(result['comparison'], 'same-image-replay')
        self.assertEqual(self.derivations, [])

    def test_missing_marker_or_cleanup_cannot_confirm_product_regression(self):
        for mode in ('crash', 'lost-cleanup', 'truncated'):
            with self.subTest(mode=mode):
                self.states[(CANDIDATE_IMAGE, 'MAIN')] = 'observed'
                self.states[(CANDIDATE_IMAGE, 'ORACLE')] = mode
                result = self.invoke(mode)
                self.assertEqual(result['outcome'], 'inconclusive')
                self.assertEqual(result['rounds']['candidate']['observations']['oracle'], ['unclassified'] * 2)

    def test_failed_doctor_blocks_candidate_drives(self):
        self.states[(CANDIDATE_IMAGE, 'DOCTOR')] = 'crash'
        result = self.invoke('environment')
        self.assertEqual(result['outcome'], 'environment-blocked')
        self.assertNotIn((CANDIDATE_IMAGE, 'MAIN'), self.calls)

    def test_probe_cannot_reuse_the_primary_script(self):
        self.oracle.write_bytes((self.helpers / 'main.sh').read_bytes())
        with self.assertRaisesRegex(ValueError, 'own implementation'):
            self.invoke('copied')

    def test_existing_fix_requires_both_probes_to_fail_twice_before_and_pass_twice_after(self):
        source = {'url': 'https://github.com/owner/repo/pull/1', 'kind': 'pull'}
        self.states[(BASE_IMAGE, 'MAIN')] = self.states[(BASE_IMAGE, 'ORACLE')] = 'observed'
        result = self.invoke('verified-fix', existing_fix=(source, b'Original report.'))
        self.assertEqual(result['outcome'], 'artifact-unconfirmed')
        self.assertEqual(result['runtime_outcome'], 'confirmed')
        self.assertEqual(result['artifact'], source)
        self.assertEqual(cli.digest(self.root/'verified-fix/frozen/report.txt'), result['report_sha256'])
        for side, image in (('baseline', BASE_IMAGE), ('candidate', CANDIDATE_IMAGE)):
            for probe in ('MAIN', 'ORACLE'):
                self.assertEqual(self.calls.count((image, probe)), 2)
            self.assertTrue(all(run['cleanup_confirmed'] for run in result['rounds'][side]['runs']))

    def test_existing_fix_distinguishes_no_change_from_an_inconclusive_run(self):
        frozen = {'kind': 'commit'}, b'Original report.'
        self.states.update({(image, probe): 'observed' for image in (BASE_IMAGE, CANDIDATE_IMAGE)
                            for probe in ('MAIN', 'ORACLE')})
        self.assertEqual(self.invoke('unchanged', existing_fix=frozen)['runtime_outcome'], 'insufficient-fix')
        self.states[(CANDIDATE_IMAGE, 'ORACLE')] = 'crash'
        self.assertEqual(self.invoke('crash-after', existing_fix=frozen)['runtime_outcome'], 'inconclusive')
        self.states[(BASE_IMAGE, 'MAIN')] = 'pass'
        self.assertEqual(self.invoke('healthy-before', existing_fix=frozen)['runtime_outcome'], 'inconclusive')

    def test_existing_fix_rejects_reused_containers(self):
        self.reuse_container = True
        self.states[(BASE_IMAGE, 'MAIN')] = self.states[(BASE_IMAGE, 'ORACLE')] = 'observed'
        result = self.invoke('duplicate', existing_fix=({'kind': 'commit'}, b'Original report.'))
        self.assertEqual(result['runtime_outcome'], 'environment-blocked')
