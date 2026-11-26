from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from workbench import benchmark, cli, phase_runtime


TRUNK_IMAGE = 'sha256:' + 'a' * 64
CANDIDATE_IMAGE = 'sha256:' + 'b' * 64
DIGEST = 'c' * 64


class PhaseRuntimeTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        self.source = self.repo / 'app.js'
        self.source.write_text('export const words = 3;\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'trunk')
        self.trunk = cli.sha(self.repo, 'HEAD')

        self.helper = self.root / 'probe.mjs'
        self.helper.write_text('// approved probe\n')
        spec = {'claim': 'Measure live throughput.', 'scope': 'end-to-end',
                'configuration': 'A fixed fixture and input.', 'production_tuning': None,
                'minimum_delta_pct': 100, 'rounds': 5,
                'workload': {'helper': self.helper.name, 'input': 'A fixed input.',
                             'completed': 10, 'digest': DIGEST, 'max_ops_per_second': 1000,
                             'plausibility': 'Bound by the fixture runtime.'}}
        manifest = self.root / 'benchmark.json'
        manifest.write_text(json.dumps(spec))
        self.entry = {'id': 'P1', 'files': ['app.js'], 'interaction': False,
                      'verify': {'unit': {'marker': 'UNIT_PASS'}, 'live': {'marker': 'LIVE_PASS'},
                                 'perf': {'probe': spec['claim'], 'max_ms': 120}}}
        self.step = {'image': 'runtime:local', 'benchmark': self.ref(manifest),
                     'helpers': {self.helper.name: self.ref(self.helper)},
                     'unit': ['node', '/app/unit.mjs'], 'live': ['node', '/app/run.mjs']}
        self.contract = {'repo': str(self.repo), 'plan': {'baseline': self.trunk, 'phases': [self.entry]},
                         'steps': {'P1': self.step}, 'memory_mb': 384}
        self.runs = []
        self.attestations = []
        self.candidate_ms = 110

    def ref(self, path):
        return {'path': str(path), 'sha256': cli.digest(path)}

    def sandbox(self, path, image, command, timeout, **options):
        self.assertEqual((Path(path) / self.helper.name).read_bytes(), self.helper.read_bytes())
        self.assertEqual(command, ['node', '/workspace/probe.mjs'])
        self.assertEqual(options, {'workspace_read_only': True, 'source_root': '/app', 'memory_mb': 384})
        now = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=len(self.runs))
        self.runs.append(image)
        value = {'kind': 'iboard-benchmark', 'unit': 'ms', 'elapsed_ms': 100 if image == TRUNK_IMAGE else self.candidate_ms,
                 'completed': 10, 'errors': 0, 'digest': DIGEST}
        return {'image': image, 'command': command, 'stdout': json.dumps(value) + '\n', 'exit_code': 0,
                'started_at': now.isoformat(), 'elapsed_seconds': .2, 'container_created': True,
                'cleanup_confirmed': True, 'workspace_read_only': True}

    def runner(self, **kwargs):
        self.assertNotEqual(Path(kwargs['helpers']), self.helper.parent)
        self.assertNotEqual(Path(kwargs['manifest']), Path(self.step['benchmark']['path']))
        expected_image = TRUNK_IMAGE if kwargs['base'] != kwargs['revision'] else 'runtime:local'
        self.assertEqual(kwargs['image'], expected_image)

        def attest(repo, image, timeout, *, commit):
            self.assertEqual((repo, image, commit), (self.repo, expected_image, self.trunk))
            return TRUNK_IMAGE

        return benchmark.run(**kwargs, sandbox=self.sandbox, attester=attest)

    def prepare_candidate(self):
        before = phase_runtime.baseline(self.contract, 'P1', self.root / 'baseline', timeout=30, runner=self.runner)
        self.source.write_text('export const words = 4;\n')
        cli.git(self.repo, 'commit', '-qam', 'candidate')
        self.head = cli.sha(self.repo, 'HEAD')
        self.phase = {'contract': self.contract, 'entry': self.entry, 'step': self.step,
                      'parent': self.trunk, 'baseline_perf': before}
        self.data = {'repo': str(self.repo), 'evidence': []}
        self.unit = self.write_acceptance('unit', 'test-result')
        self.live = self.write_acceptance('live', 'resolution')

    def write_acceptance(self, lane, kind):
        path = self.root / f'{lane}.json'
        marker = self.entry['verify'][lane]['marker']
        cli.write(path, {'schema': cli.SCHEMA, 'commit': self.head, 'image': CANDIDATE_IMAGE,
                         'source_mode': 'image', 'verified_image_id': CANDIDATE_IMAGE,
                         'source_verified_commit': self.head, 'command': self.step[lane],
                         'kind': kind, 'actor': 'independent-verifier', 'expected_marker': marker,
                         'expected_exit': 0, 'stdout': marker + '\n', 'exit_code': 0,
                         'workspace_read_only': True, 'container_created': True, 'cleanup_confirmed': True})
        self.data['evidence'].append({**self.ref(path), 'kind': kind, 'commit': self.head,
                                      'actor': 'independent-verifier'})
        return path

    def attest(self, repo, image, timeout, *, commit):
        self.attestations.append((repo, image, commit))
        return image

    def run_candidate(self, **kwargs):
        options = {'timeout': 30, 'runner': self.runner, 'attest': self.attest}
        return phase_runtime.candidate(self.data, self.phase, self.head, CANDIDATE_IMAGE,
                                       self.unit, self.ref(self.live), self.root / 'candidate', **(options | kwargs))

    def test_candidate_uses_earlier_trunk_and_the_exact_attested_image(self):
        self.prepare_candidate()
        record = self.run_candidate()
        self.assertEqual(record['commit'], self.head)
        self.assertEqual(record['baseline_perf'], self.phase['baseline_perf'])
        self.assertEqual(record['unit'], self.ref(self.unit))
        self.assertEqual(record['live'], self.ref(self.live))
        self.assertEqual(record['perf'], self.ref(self.root / 'candidate' / 'result.json'))
        self.assertEqual(self.runs, [TRUNK_IMAGE] * 10 + [TRUNK_IMAGE, CANDIDATE_IMAGE] * 5)
        self.assertEqual(self.attestations, [(self.repo, CANDIDATE_IMAGE, self.head)])

    def test_changed_helper_blocks_before_running_any_measurement(self):
        self.helper.write_text('// unapproved edit\n')
        with self.assertRaisesRegex(phase_runtime.PhaseRuntimeError, 'helper changed since approval'):
            phase_runtime.baseline(self.contract, 'P1', self.root / 'baseline', timeout=30, runner=self.runner)
        self.assertFalse(self.runs)
        self.assertFalse((self.root / 'baseline').exists())

    def test_receipts_must_belong_to_the_independent_task(self):
        self.prepare_candidate()
        self.data['evidence'][0]['superseded'] = True
        with self.assertRaisesRegex(phase_runtime.PhaseRuntimeError, 'missing from the candidate task evidence'):
            self.run_candidate()
        self.assertEqual(self.runs, [TRUNK_IMAGE] * 10)

    def test_tampered_trunk_evidence_blocks_candidate_reuse(self):
        self.prepare_candidate()
        path = Path(self.phase['baseline_perf']['path'])
        result = cli.read(path)
        result['outcome'] = 'faster'
        cli.write(path, result)
        with self.assertRaisesRegex(phase_runtime.PhaseRuntimeError, 'does not match the authorized digest'):
            self.run_candidate()
        self.assertEqual(self.runs, [TRUNK_IMAGE] * 10)

    def test_out_of_budget_performance_retains_candidate_evidence(self):
        self.prepare_candidate()
        self.candidate_ms = 140
        with self.assertRaisesRegex(phase_runtime.PhaseRuntimeError, 'median 140.000 ms exceeds 120 ms'):
            self.run_candidate()
        result = cli.read(self.root / 'candidate' / 'result.json')
        self.assertEqual(result['images']['candidate'], CANDIDATE_IMAGE)
        self.assertEqual(len(result['samples']), 10)

    def test_candidate_attestation_must_return_the_tested_digest(self):
        self.prepare_candidate()
        with self.assertRaisesRegex(phase_runtime.PhaseRuntimeError, 'no longer matches its immutable identity'):
            self.run_candidate(attest=lambda *args, **kwargs: TRUNK_IMAGE)
        result = cli.read(self.root / 'candidate' / 'result.json')
        self.assertIn('no longer matches its immutable identity', result['issues'][0])
        self.assertEqual(self.runs, [TRUNK_IMAGE] * 10)
