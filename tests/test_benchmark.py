import json
from pathlib import Path
import tempfile
import unittest

from workbench import benchmark, cli


DIGEST = 'a' * 64
BASE_IMAGE = 'sha256:' + 'b' * 64
NEW_IMAGE = 'sha256:' + 'c' * 64


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        app = self.repo / 'app.js'
        app.write_text('runWork(); // old schedule\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'baseline')
        self.base = cli.sha(self.repo, 'HEAD')
        app.write_text('runWork(); // new schedule\n')
        cli.git(self.repo, 'commit', '-qam', 'candidate')
        self.candidate = cli.sha(self.repo, 'HEAD')
        self.helpers = self.root / 'helpers'
        self.helpers.mkdir()
        for name in ('measure.mjs', 'profile.mjs', 'whole.mjs'):
            (self.helpers / name).write_text(f'// {name}\n')
        self.spec = {'claim': 'Compare checked elapsed time.', 'scope': 'end-to-end',
                     'configuration': 'Fixed fixture, release configuration.',
                     'production_tuning': 'Both use the same release command and limits.',
                     'minimum_delta_pct': 5, 'rounds': 5,
                     'workload': {'helper': 'measure.mjs', 'input': 'A fixed input.',
                                  'completed': 10, 'digest': DIGEST, 'max_ops_per_second': 1000,
                                  'plausibility': 'Fixture cap from host configuration.'},
                     'profile': 'profile.mjs'}
        self.calls = []
        self.override = {}
        self.base_times = [100, 102, 99, 100, 101]
        self.candidate_times = [60, 63, 61, 62, 64]

    def attest(self, repo, image, timeout, *, commit):
        self.assertEqual((repo, commit), (self.repo, self.base))
        return BASE_IMAGE

    def derive(self, repo, base, head, image, timeout):
        self.assertEqual((repo, base, head, image), (self.repo, self.base, self.candidate, BASE_IMAGE))
        return NEW_IMAGE

    def sandbox(self, path, image, command, timeout, **options):
        name = Path(command[-1]).name
        self.assertTrue(options['workspace_read_only'])
        self.assertEqual(options['source_root'], '/app')
        self.assertEqual([file.name for file in Path(path).iterdir()], [name])
        counter = self.calls.count((image, name))
        self.calls.append((image, name))
        values = self.base_times if image == BASE_IMAGE else self.candidate_times
        record = {'kind': 'iboard-benchmark', 'unit': 'ms', 'elapsed_ms': values[counter % len(values)],
                  'completed': 10, 'errors': 0, 'digest': DIGEST}
        if name == 'profile.mjs':
            record.update(kind='iboard-benchmark-profile', limiter='Fixture scheduler wait',
                          hotspot={'path': 'app.js', 'contains': 'runWork()'},
                          observations=[{'metric': 'scheduler wait', 'value': 20, 'unit': 'ms'}])
        record.update(self.override)
        return {'image': image, 'command': command, 'stdout': json.dumps(record) + '\n', 'exit_code': 0,
                'elapsed_seconds': .2, 'container_created': True, 'cleanup_confirmed': True,
                'workspace_read_only': True}

    def invoke(self, name, **overrides):
        manifest = self.root / 'contract.json'
        manifest.write_text(json.dumps(self.spec))
        arguments = dict(repo=self.repo, base=self.base, revision=self.candidate, image='runtime:local',
                         helpers=self.helpers, manifest=manifest, out=self.root / name,
                         sandbox=self.sandbox, attester=self.attest, derive=self.derive)
        return benchmark.run(**(arguments | overrides))

    def test_alternating_runs_report_median_and_range_and_exclude_profiling(self):
        result = self.invoke('compare')
        self.assertEqual(result['outcome'], 'faster')
        self.assertEqual(self.calls[:10], [(BASE_IMAGE, 'measure.mjs'), (NEW_IMAGE, 'measure.mjs')] * 5)
        self.assertEqual(self.calls[-2:], [(BASE_IMAGE, 'profile.mjs'), (NEW_IMAGE, 'profile.mjs')])
        self.assertEqual(result['comparisons']['workload']['baseline'], {'median_ms': 100, 'range_ms': [99, 102]})
        self.assertEqual(result['comparisons']['workload']['candidate'], {'median_ms': 62, 'range_ms': [60, 64]})
        self.assertEqual(result['profiles']['candidate']['source']['line'], 1)
        for sample in result['samples']:
            self.assertEqual(cli.digest(self.root / 'compare' / sample['report']), sample['sha256'])

    def test_same_commit_overlapping_samples_do_not_claim_a_benefit(self):
        result = self.invoke('control', revision=self.base)
        self.assertEqual(result['outcome'], 'no-measurable-difference')
        self.assertEqual(set(result['images'].values()), {BASE_IMAGE})

    def test_unmeasured_limiter_tuning_and_real_user_path_prevent_directional_claim(self):
        del self.spec['profile']
        self.spec.update(scope='component', production_tuning=None)
        result = self.invoke('under-evidenced')
        self.assertEqual(result['outcome'], 'inconclusive')
        self.assertEqual(result['comparisons']['workload']['observed'], 'faster')
        self.assertEqual(len(result['issues']), 3)

    def test_work_errors_digest_and_ceiling_are_checked_before_reporting_time(self):
        for index, fields in enumerate(({'errors': 1}, {'digest': 'b' * 64}, {'completed': 0},
                                        {'elapsed_ms': .005}, {'elapsed_ms': 215})):
            with self.subTest(fields=fields):
                self.override = fields
                result = self.invoke(f'bad-{index}')
                self.assertEqual(result['outcome'], 'inconclusive')
                self.assertFalse(result['comparisons'])
                self.assertTrue((self.root / f'bad-{index}' / result['samples'][0]['report']).exists())
