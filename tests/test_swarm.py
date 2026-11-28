from pathlib import Path
import tempfile
import threading
import time
import unittest

from workbench import cli, delivery, swarm


class SwarmTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.repo = self.root / 'source'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        for key, value in (('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid'),
                           ('commit.gpgsign', 'false')):
            cli.git(self.repo, 'config', key, value)
        (self.repo / 'probe.mjs').write_text('console.log("PROBE_OK");\n')
        (self.repo / 'value').write_text('baseline\n')
        self.base = self.commit('Original revision')
        (self.repo / 'value').write_text('updated\n')
        self.head = self.commit('Updated revision')

    def commit(self, message):
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', message)
        return cli.sha(self.repo, 'HEAD')

    def lane(self, ident, *, role='coverage', revision=None):
        return {'id': ident, 'role': role, 'revision': revision or self.head,
                'command': ['node', '/workspace/probe.mjs'], 'marker': 'PROBE_OK', 'exit_code': 0}

    def freeze(self, name, lanes, *, shape='coverage', rule=None):
        manifest = self.root / f'{name}.json'
        cli.write(manifest, {'schema': cli.SCHEMA, 'predicate': 'All selected entry points complete.',
                             'shape': shape, 'race_rule': rule, 'parallelism': 2, 'seconds': 30,
                             'memory_mb': 192, 'image': 'sha256:' + 'b' * 64, 'lanes': lanes})
        return swarm.freeze(manifest, self.repo, self.root / f'plan-{name}')

    def execute(self, approval, name, runner):
        return swarm.run(**approval, out=self.root / f'run-{name}', sandbox=runner)

    def verify(self, approval, result):
        return swarm.audit(**approval, report=result['report'], report_sha256=result['sha256'])

    def sandbox(self, worktree, image, command, timeout, *, memory_mb, workspace_read_only):
        return {'workspace': str(worktree), 'image': image, 'command': command, 'memory_mb': memory_mb,
                'workspace_read_only': workspace_read_only, 'container_created': True,
                'cleanup_confirmed': True, 'exit_code': 0, 'stdout': 'PROBE_OK\n', 'stderr': ''}

    def test_coverage_is_parallel_with_exact_distinct_worktrees_and_auditable_receipts(self):
        approval = self.freeze('coverage', [self.lane('base', revision=self.base), self.lane('head')])
        both_started = threading.Barrier(2, timeout=8)

        def sandbox(worktree, *args, **kwargs):
            both_started.wait()
            expect = self.base if worktree.parent.name == 'base' else self.head
            self.assertEqual(cli.sha(worktree, 'HEAD'), expect)
            return self.sandbox(worktree, *args, **kwargs)

        result = self.execute(approval, 'coverage', sandbox)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(self.verify(approval, result)['status'], 'PASS')
        for lane in result['lanes']:
            run = cli.read(Path(lane['attempts'][0]['path']))
            self.assertFalse(Path(run['worktree']).exists())
        Path(result['lanes'][0]['attempts'][0]['path']).write_text('{}')
        with self.assertRaisesRegex(swarm.SwarmError, 'digest changed'):
            self.verify(approval, result)

    def test_retry_on_missing_provenance_but_not_on_a_real_failure_or_unknown_cleanup(self):
        approval = self.freeze('retry', [self.lane('retry'), self.lane('defect'), self.lane('cleanup')])

        def sandbox(worktree, *args, **kwargs):
            raw = self.sandbox(worktree, *args, **kwargs)
            lane = worktree.parent.name
            if lane == 'retry' and worktree.name == 'worktree-1':
                raw['image'] = 'unspecified'
            if lane == 'defect':
                raw['stdout'] = 'Unexpected visible result\n'
            if lane == 'cleanup':
                raw['cleanup_confirmed'] = False
            return raw

        result = self.execute(approval, 'retry', sandbox)
        self.assertEqual(result['status'], 'ISSUES')
        self.assertEqual(result['issues'], ['defect'])
        self.assertEqual(result['gaps'], ['cleanup'])
        self.assertEqual([len(lane['attempts']) for lane in result['lanes']], [2, 1, 1])
        self.assertEqual(self.verify(approval, result)['status'], 'ISSUES')

    def test_first_pass_uses_independently_recorded_completion_order(self):
        lanes = [self.lane('slow', role='race'), self.lane('fast', role='race')]
        approval = self.freeze('race', lanes, shape='race', rule='first-pass')

        def sandbox(worktree, *args, **kwargs):
            peer = worktree.parent.parent / 'fast' / 'attempt-1.json'
            if worktree.parent.name == 'slow':
                deadline = time.monotonic() + 8
                while not peer.exists():
                    if time.monotonic() >= deadline:
                        self.fail('fast arm never finished')
                    time.sleep(.03)
            return self.sandbox(worktree, *args, **kwargs)

        result = self.execute(approval, 'race', sandbox)
        self.assertEqual(result['completion_order'], ['fast', 'slow'])
        self.assertEqual(result['winner'], 'fast')
        self.assertEqual(self.verify(approval, result)['verified']['winner'], 'fast')
        path = Path(result['report'])
        modified = cli.read(path)
        modified['completion_order'] = ['slow', 'fast']
        modified['winner'] = 'slow'
        cli.write(path, modified)
        result['sha256'] = cli.digest(path)
        with self.assertRaisesRegex(swarm.SwarmError, 'completion order'):
            self.verify(approval, result)

    def test_rank_all_and_best_of_do_not_hide_a_missing_required_coverage_lane(self):
        lanes = [self.lane('required'), self.lane('better', role='race'), self.lane('unknown', role='race')]

        def sandbox(worktree, *args, **kwargs):
            raw = self.sandbox(worktree, *args, **kwargs)
            if worktree.parent.name == 'better':
                raw['stdout'] += 'IBOARD_SCORE=95\n'
            else:
                raw['container_created'] = False
            return raw

        for rule in ('rank-all', 'best-of'):
            with self.subTest(rule=rule):
                approval = self.freeze(rule, lanes, shape='mixed', rule=rule)
                result = self.execute(approval, rule, sandbox)
                self.assertEqual(result['status'], 'BLOCKED')
                self.assertEqual(result['gaps'], ['required', 'unknown'])
                self.assertEqual(result['winner'], 'better' if rule == 'best-of' else None)
                self.assertEqual([len(lane['attempts']) for lane in result['lanes']], [2, 1, 2])
                self.assertEqual(self.verify(approval, result)['status'], 'BLOCKED')

    def test_race_rejects_changed_driver_or_different_command(self):
        lanes = [self.lane('base', role='race', revision=self.base), self.lane('head', role='race')]
        lanes[-1]['command'].append('different')
        with self.assertRaisesRegex(swarm.SwarmError, 'same frozen measurement method'):
            self.freeze('different-command', lanes, shape='race', rule='rank-all')
        (self.repo / 'probe.mjs').write_text('// changed driver\n')
        lanes[-1] = self.lane('head', role='race', revision=self.commit('Changed harness'))
        with self.assertRaisesRegex(swarm.SwarmError, 'identical driver'):
            self.freeze('different-driver', lanes, shape='race', rule='rank-all')

    def test_task_gate_requires_the_frozen_coverage_on_the_exact_candidate(self):
        manifest = self.root / 'approved.json'
        cli.write(manifest, {'schema': cli.SCHEMA, 'predicate': 'Every required boundary is exercised.',
                             'shape': 'coverage', 'race_rule': None, 'parallelism': 2, 'seconds': 15,
                             'memory_mb': 192, 'image': 'sha256:' + 'b' * 64,
                             'lanes': [self.lane('current', revision='candidate'),
                                       self.lane('control', revision='baseline')]})
        reference = {'path': str(manifest), 'sha256': cli.digest(manifest)}
        data = {'repo': str(self.repo), 'base_commit': self.base, 'coverage_swarm': reference}
        proof = swarm.verify_task(data, self.head, self.root / 'delivery-evidence', seconds=10, sandbox=self.sandbox)
        swarm.require_task(data, self.head, proof)
        with self.assertRaisesRegex(swarm.SwarmError, 'exact revisions'):
            swarm.require_task(data, self.base, proof)
        with self.assertRaisesRegex(swarm.SwarmError, 'evidence is missing'):
            swarm.require_task(data, self.head, None)
        task = self.root / 'task.json'
        cli.write(task.with_name('input-stage.json'), {'schema': cli.SCHEMA, 'coverage_swarm': reference})
        with self.assertRaisesRegex(delivery.DeliveryError, 'approved task intake'):
            delivery.require_refactor_intake(task, {'kind': 'bug'})


if __name__ == '__main__':
    unittest.main()
