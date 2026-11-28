import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from functools import partial

from workbench import benchmark, cli, perf_issue, workflow


IMAGE = 'sha256:' + 'b' * 64
NEW_IMAGE = 'sha256:' + 'c' * 64
DIGEST = 'a' * 64


class PerfIssueTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        repo = self.root / 'repo'
        repo.mkdir()
        cli.git(repo, 'init', '-q', '-b', 'main')
        cli.git(repo, 'config', 'user.name', 'Bench')
        cli.git(repo, 'config', 'user.email', 'bench@example.invalid')
        (repo / 'app.js').write_text('runWork(); // old schedule\n', encoding='utf-8')
        cli.git(repo, 'add', '.')
        cli.git(repo, 'commit', '-qm', 'baseline')
        prepared = workflow.prepare(task_id='perf-issue', repo=repo, tasks_root=self.root / 'tasks',
                                    workspaces_root=self.root / 'workspaces', source='Observed slow fixture',
                                    kind='feature', surface='non-ui', iterations=2, seconds=600, implementer='editor')
        self.task = Path(prepared['task'])
        self.worktree = Path(prepared['workspace'])
        self.helpers = self.root / 'helpers'
        self.helpers.mkdir()
        for name in ('measure.mjs', 'profile.mjs', 'user.mjs'):
            (self.helpers / name).write_text(f'// {name}\n', encoding='utf-8')
        self.manifest = self.root / 'benchmark.json'
        self.spec = {'claim': 'Complete the fixture request.', 'scope': 'end-to-end',
                     'configuration': 'Pinned fixture.', 'production_tuning': 'Same runtime and input.',
                     'minimum_delta_pct': 5, 'rounds': 5, 'profile': 'profile.mjs',
                     'workload': {'helper': 'measure.mjs', 'input': 'Fixed payload', 'completed': 2,
                                  'digest': DIGEST, 'max_ops_per_second': 1000, 'plausibility': 'Fixture rate ceiling.'}}
        self.calls = 0
        self.new = {'measure.mjs': 60, 'profile.mjs': 65, 'user.mjs': 87}
        self.runner = partial(benchmark.run, sandbox=self.sandbox,
                              attester=lambda *_args, **_kw: IMAGE, derive=lambda *_: NEW_IMAGE)

    def sandbox(self, path, image, command, timeout, **options):
        self.calls += 1
        helper = Path(command[-1]).name
        ms = self.new[helper] if image == NEW_IMAGE else 100
        record = {'kind': 'iboard-benchmark', 'unit': 'ms', 'elapsed_ms': ms,
                  'completed': 2, 'errors': 0, 'digest': DIGEST}
        if helper == 'profile.mjs':
            record.update(kind='iboard-benchmark-profile', limiter='Fixture work',
                          hotspot={'path': 'app.js', 'contains': 'runWork()'},
                          observations=[{'metric': 'worker time', 'value': ms, 'unit': 'ms'}])
        stdout = json.dumps(record)
        return {'image': image, 'command': command, 'stdout': stdout, 'exit_code': 0,
                'elapsed_seconds': .3, 'container_created': True, 'cleanup_confirmed': True,
                'source_root': options['source_root'], 'memory_mb': options['memory_mb'],
                'workspace_read_only': True, 'started_at': cli.now(), 'timed_out': False,
                'create_stdout': f'{self.calls:064x}', 'create_exit_code': 0,
                'stdout_truncated': False, 'stderr_truncated': False,
                'stdout_sha256': hashlib.sha256(stdout.encode()).hexdigest(),
                'stderr_sha256': hashlib.sha256(b'').hexdigest()}

    def freeze(self, **kwargs):
        self.manifest.write_text(json.dumps(self.spec), encoding='utf-8')
        return perf_issue.freeze(self.task, self.manifest, self.helpers, IMAGE, self.root / 'before',
                                 runner=self.runner, **kwargs)

    def edit(self):
        (self.worktree / 'app.js').write_text('runWork(); // new schedule\n', encoding='utf-8')
        cli.git(self.worktree, 'commit', '-qam', 'candidate')
        return cli.sha(self.worktree, 'HEAD')

    def compare(self, head):
        return perf_issue.compare_candidate(cli.read(self.task), head, self.root / 'after',
                                             timeout=180, runner=self.runner)

    def test_rechecks_all_raw_receipts_before_delivery(self):
        plan = self.freeze(minimum_improvement_pct=30)
        self.assertEqual(plan['baseline_ms'], 100)
        self.assertEqual(plan['hotspot']['line'], 1)
        head = self.edit()
        data = cli.read(self.task)
        self.assertTrue(any('performance evidence did not pass' in msg for msg in cli.evidence_gate(data, head)[1]))
        ref, metric = self.compare(head)
        data['performance_result'] = ref
        self.assertEqual((metric['baseline_ms'], metric['candidate_ms'], metric['delta_pct']), (100, 60, 40))
        self.assertEqual(metric, perf_issue.require(data, head))
        self.assertFalse(any('performance evidence did not pass' in msg for msg in cli.evidence_gate(data, head)[1]))
        raw = self.root / 'after' / 'profile-candidate.json'
        raw.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(cli.WorkbenchError, 'digest changed'):
            perf_issue.require(data, head)

    def test_component_gain_cannot_hide_a_user_path_regression(self):
        self.spec.update(scope='component', end_to_end={**self.spec['workload'], 'helper': 'user.mjs'})
        self.freeze()
        self.new['user.mjs'] = 130
        with self.assertRaisesRegex(ValueError, 'end-to-end workload regressed.*Retained measurement'):
            self.compare(self.edit())

    def test_success_requires_the_frozen_target_and_retains_failed_measurements(self):
        self.freeze(minimum_improvement_pct=70)
        with self.assertRaisesRegex(ValueError, 'failed to reproduce the required improvement'):
            self.compare(self.edit())
        self.assertTrue((self.root / 'after' / 'result.json').is_file())

    def test_freeze_must_precede_edits(self):
        self.edit()
        with self.assertRaisesRegex(ValueError, 'clean task baseline'):
            self.freeze()
        self.assertNotIn('performance_contract', cli.read(self.task))


if __name__ == '__main__':
    unittest.main()
