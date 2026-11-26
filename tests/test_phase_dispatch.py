from pathlib import Path
import tempfile
from threading import Barrier, Lock
import unittest
from unittest.mock import patch

from workbench import cli, phase_contract, phase_dispatch, start


class PhaseDispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Phase Fixture')
        cli.git(self.repo, 'config', 'user.email', 'phase@example.invalid')
        (self.repo / 'trunk.txt').write_text('Original tree.\n')
        cli.git(self.repo, 'add', 'trunk.txt')
        cli.git(self.repo, 'commit', '-qm', 'Pin fixture trunk')
        self.trunk = cli.sha(self.repo, 'HEAD')
        self.out = self.root / 'dispatch'
        self.manifest = self.root / 'authorize.json'
        self.plan = self.root / 'plan.json'
        cli.write(self.manifest, {'schema': cli.SCHEMA, 'authorization': 'fixture'})
        cli.write(self.plan, {'schema': cli.SCHEMA, 'status': 'planned'})
        self.digest = cli.digest(self.manifest)
        self.entries = [self.entry('A', 'trunk', (), interaction=True),
                        self.entry('B', 'A', ('A',)),
                        self.entry('C', 'trunk', ()),
                        self.entry('D', 'B', ('B', 'C'))]
        self.contract = {
            'repo': str(self.repo), 'plan': {'baseline': self.trunk, 'phases': self.entries,
                                            'request': 'Exercise the staged changes.'},
            'manifest_path': str(self.manifest), 'manifest_sha256': self.digest,
            'plan_path': str(self.plan), 'plan_sha256': cli.digest(self.plan),
            'seconds': 180, 'iterations': 2, 'memory_mb': 512, 'destination': None,
            'steps': {entry['id']: {'image': 'pinned-fixture-image', 'feature_doc': entry['files'][0],
                                    'unit': ['node', '/app/unit.mjs'], 'live': ['node', '/app/live.mjs']}
                      for entry in self.entries},
        }
        self.measured, self.launched, self.resumed, self.audited = [], [], [], []
        self.tasks = {}
        self.approved = set()
        self.fail_once = set()
        replacements = {
            'load': self.load,
            'bind': self.bind,
            'read': self.read,
            'verify_baseline': self.verify_baseline,
            'verify_completion': self.verify_completion,
            'interaction_approved': self.interaction_approved,
        }
        for name, substitute in replacements.items():
            stub = patch.object(phase_contract, name, side_effect=substitute)
            self.addCleanup(stub.stop)
            stub.start()
        gate = patch.object(cli, 'evidence_gate', side_effect=self.evidence_gate)
        self.addCleanup(gate.stop)
        gate.start()
        independent = patch.object(phase_dispatch.delivery, '_independent_verification',
                                   side_effect=self.independent_verification)
        self.addCleanup(independent.stop)
        independent.start()

    @staticmethod
    def entry(ident, base, needs, interaction=False):
        return {'id': ident, 'base': base, 'depends_on': list(needs), 'interaction': interaction,
                'files': [f'{ident}.txt'], 'change': f'Implement {ident}.',
                'observed_result': f'{ident} is available.',
                'verify': {name: {'scenario': f'Exercise {name} in {ident}.',
                                  'predicate': f'{ident} {name} succeeds.',
                                  'marker': f'{ident}_{name.upper()}_PASS'} for name in ('unit', 'live')}}

    def load(self, manifest, expected_sha256):
        self.assertEqual(Path(manifest), self.manifest)
        self.assertEqual(cli.digest(manifest), expected_sha256)
        return self.contract

    @staticmethod
    def ref(path):
        return {'path': str(path), 'sha256': cli.digest(path)}

    def measure(self, contract, phase_id, out, *, timeout):
        self.assertEqual(contract['plan']['baseline'], self.trunk)
        self.assertGreater(timeout, 0)
        self.measured.append(phase_id)
        path = Path(out) / 'result.json'
        cli.write(path, {'schema': cli.SCHEMA, 'phase': phase_id, 'baseline': self.trunk})
        return self.ref(path)

    def verify_baseline(self, contract, phase_id, reference):
        path = Path(reference['path'])
        if self.ref(path) != reference or cli.read(path)['phase'] != phase_id:
            raise ValueError('baseline proof changed')

    def bind(self, task_path, data, contract, phase_id, parent, request, baseline_perf, **kwargs):
        task = Path(task_path)
        self.assertEqual(data['base_commit'], parent)
        self.assertEqual(cli.sha(data['repo'], 'HEAD'), parent)
        self.verify_baseline(contract, phase_id, baseline_perf)
        binding = task.with_name('phase-binding.json')
        cli.write(binding, {'schema': cli.SCHEMA, 'phase': phase_id, 'parent': parent,
                            'baseline_perf': baseline_perf, 'request_sha256': cli.digest(request)})
        data['phase_binding'] = self.ref(binding)
        if kwargs.get('visual_baseline'):
            data['visual_baseline'] = kwargs['visual_baseline']
        self.tasks[phase_id] = task

    def read(self, data):
        binding = cli.read(Path(data['phase_binding']['path']))
        entry = next(item for item in self.entries if item['id'] == binding['phase'])
        return {**binding, 'contract': self.contract, 'entry': entry,
                'step': self.contract['steps'][entry['id']], 'task_path': self.tasks[entry['id']]}

    def verify_completion(self, data, candidate):
        phase = self.read(data)['entry']
        self.audited.append(phase['id'])
        reference = data['phase_result']
        path = Path(reference['path'])
        if cli.digest(path) != reference['sha256'] or cli.read(path)['commit'] != candidate:
            raise ValueError('phase proof changed')
        return {'commit': candidate, 'changes': phase['files'], 'median_ms': 25,
                'status': 'awaiting-operator-review' if phase['interaction'] else 'evidence-verified'}

    def interaction_approved(self, data, candidate):
        return candidate in self.approved

    def evidence_gate(self, data, expected):
        actual = cli.sha(data['repo'], 'HEAD')
        return actual, [] if actual == expected else ['candidate changed']

    def independent_verification(self, data, candidate):
        reference = data['fixture_independent_review']
        path = Path(reference['path'])
        if self.ref(path) != reference or cli.read(path)['commit'] != candidate:
            raise ValueError('independent proof changed')
        return reference

    def launch(self, **options):
        self.assertEqual(options.get('pr_repo'), None)
        self.assertEqual(options['source_mode'], 'image')
        self.assertTrue(options['build_source_image'])
        self.assertLessEqual(options['seconds'], self.contract['seconds'])
        prepared = options['prepare'](**{key: options[key] for key in (
            'task_id', 'repo', 'tasks_root', 'workspaces_root', 'source', 'kind', 'surface',
            'iterations', 'seconds')}, implementer='fixture')
        task = Path(prepared['task'])
        issue = task.with_name('issue.txt')
        issue.write_bytes(Path(options['issue_file']).read_bytes())
        data = cli.read(task)
        data['criteria'] = options['criteria']
        cli.write(task, data)
        self.launched.append(data['base_commit'])
        return options['run'](task, issue, options['image'], source_mode='image',
                              build_source_image=True, memory_mb=options['memory_mb'])

    def execute(self, task, issue, image, **options):
        task = Path(task)
        data = cli.read(task)
        phase = self.read(data)
        ident = phase['entry']['id']
        self.assertEqual(cli.digest(issue), phase['request_sha256'])
        state = task.with_name('autopilot.json')
        data['status'] = 'paused'
        if ident in self.fail_once:
            self.fail_once.remove(ident)
            cli.write(state, {'schema': cli.SCHEMA, 'phase': 'plan', 'phase_binding': data['phase_binding']})
            cli.write(task, data)
            raise start.StartError('synthetic interruption before model work')
        worktree = Path(data['repo'])
        filename = f'{ident}.txt'
        (worktree / filename).write_text(f'Scoped change for {ident}.\n')
        cli.git(worktree, 'add', filename)
        cli.git(worktree, 'commit', '-qm', f'Complete phase {ident}')
        head = cli.sha(worktree, 'HEAD')
        proof = task.with_name('phase-result.json')
        cli.write(proof, {'schema': cli.SCHEMA, 'commit': head})
        data['phase_result'] = self.ref(proof)
        independent = task.with_name('independent-review.json')
        cli.write(independent, {'schema': cli.SCHEMA, 'commit': head, 'verdict': 'PASS'})
        data['fixture_independent_review'] = self.ref(independent)
        cli.write(state, {'schema': cli.SCHEMA, 'phase': 'delivery', 'candidate_commit': head,
                          'phase_binding': data['phase_binding']})
        cli.write(task, data)
        return cli.read(state)

    def resume(self, task, *, run):
        task = Path(task)
        data = cli.read(task)
        ident = self.read(data)['entry']['id']
        self.resumed.append(ident)
        return run(task, task.with_name('issue.txt'), self.contract['steps'][ident]['image'])

    def dispatch(self, **options):
        return phase_dispatch.run(self.manifest, self.digest, self.out, launch=self.launch,
                                  resume=self.resume, measure=self.measure, execute=self.execute, **options)

    def test_freezes_all_visual_baselines_before_launch_and_never_recaptures_after_work_begins(self):
        for ident in ('A', 'B'):
            self.contract['steps'][ident]['visual_parity'] = {'path': str(self.root / f'{ident}.json'),
                                                              'sha256': 'a' * 64, 'driver_sha256': 'b' * 64}
        calls = []

        def capture(repo, base, image, manifest, out, seconds, memory):
            self.assertEqual(self.launched, [])
            self.assertEqual((repo, base, memory), (str(self.repo), self.trunk, 512))
            calls.append(manifest.stem)
            if calls == ['A', 'B']:
                raise ValueError('browser capture failed')
            out.mkdir(parents=True)
            report = out / 'baseline.json'
            cli.write(report, {'phase': manifest.stem, 'base': base})
            return self.ref(report)

        with patch.object(phase_contract, 'verify_visual_baseline') as audit:
            blocked = self.dispatch(capture=capture)
            self.assertEqual(blocked['phases']['B']['reason'], 'browser capture failed')
            self.assertEqual(self.measured, [])
            waiting = self.dispatch(capture=capture)
            self.assertEqual(waiting['status'], 'waiting', waiting)
            self.assertEqual(calls, ['A', 'B', 'B'])
            journal_path = self.out / 'run.json'
            journal = cli.read(journal_path)
            self.assertEqual(waiting['phases']['A']['visual_baseline'], journal['phases']['A']['visual_baseline'])
            audit.reset_mock()
            self.assertEqual(self.dispatch(capture=capture)['status'], 'waiting')
            self.assertEqual([entry.kwargs['before'] for entry in audit.call_args_list],
                             [journal['visual_frozen_at']] * 2)
            del journal['phases']['B']['visual_baseline']
            cli.write(journal_path, journal)
            blocked = self.dispatch(capture=capture)
            self.assertIn('missing after component work began', blocked['phases']['B']['reason'])
            self.assertEqual(calls, ['A', 'B', 'B'])

    def assert_parent(self, result, phase, expected):
        self.assertEqual(cli.sha(self.repo, result['phases'][phase]['commit'] + '^'), expected)

    def test_parallel_editors_use_separate_worktrees_and_one_verification_lane(self):
        self.contract['parallelism'] = 2
        self.entries.append(self.entry('E', 'trunk', ()))
        self.contract['steps']['E'] = {**self.contract['steps']['C'], 'feature_doc': 'E.txt'}
        together = Barrier(2, timeout=5)
        sandbox = Lock()
        entered, worktrees = [], {}
        execute, measure = self.execute, self.measure

        def measure_exclusively(*args, **kwargs):
            self.assertTrue(sandbox.acquire(blocking=False))
            try:
                return measure(*args, **kwargs)
            finally:
                sandbox.release()

        def execute_in_parallel(task, issue, image, *, phase_verification, **options):
            data = cli.read(task)
            ident = self.read(data)['entry']['id']
            worktrees[ident] = data['repo']
            entered.append(ident)
            if ident in ('A', 'C'):
                together.wait()
            else:
                self.assertEqual(set(entered), {'A', 'C', 'E'})
            with phase_verification():
                self.assertTrue(sandbox.acquire(blocking=False))
                try:
                    return execute(task, issue, image, **options)
                finally:
                    sandbox.release()

        self.execute, self.measure = execute_in_parallel, measure_exclusively
        result = self.dispatch()
        self.assertEqual(list(result['phases']), ['A', 'B', 'C', 'D', 'E'])
        self.assertEqual(result['phases']['B']['status'], 'waiting-dependencies')
        self.assertEqual(result['phases']['E']['status'], 'verified')
        self.assertEqual(set(worktrees), {'A', 'C', 'E'})
        self.assertEqual(len(set(worktrees.values())), 3)
        journal = cli.read(self.out / 'run.json')
        self.assertEqual(journal['phases']['B']['result'], result['phases']['B'])
        self.assertCountEqual(self.measured, ['A', 'C', 'E'])
        self.assertEqual(self.dispatch(), result)
        self.assertCountEqual(entered, ['A', 'C', 'E'])

    def test_waits_for_approved_interaction_and_uses_exact_declared_git_base(self):
        first = self.dispatch()
        self.assertEqual(first['status'], 'waiting')
        self.assertEqual({key: row['status'] for key, row in first['phases'].items()}, {
            'A': 'awaiting-operator-review', 'B': 'waiting-dependencies',
            'C': 'verified', 'D': 'waiting-dependencies'})
        self.assertEqual(self.measured, ['A', 'C'])
        self.assertEqual(self.launched, [self.trunk, self.trunk])

        first_commit = first['phases']['A']['commit']
        self.approved.add(first_commit)
        final = self.dispatch()
        self.assertEqual(final['status'], 'verified')
        second_commit = final['phases']['B']['commit']
        self.assert_parent(final, 'B', first_commit)
        self.assert_parent(final, 'C', self.trunk)
        self.assert_parent(final, 'D', second_commit)
        self.assertEqual(self.launched, [self.trunk, self.trunk, first_commit, second_commit])
        self.assertEqual(self.measured, ['A', 'C', 'B', 'D'])
        self.assertEqual(self.dispatch()['status'], 'verified')
        self.assertEqual(self.measured, ['A', 'C', 'B', 'D'])
        self.assertEqual(len(self.launched), 4)
        self.assertEqual(self.resumed, [])
        self.assertEqual(cli.sha(self.repo, 'HEAD'), self.trunk)
        self.assertEqual(cli.git(self.repo, 'status', '--porcelain'), '')

    def test_rechecks_prior_proof_and_withholds_dependents_if_it_changed(self):
        first = self.dispatch()
        candidate = first['phases']['A']['commit']
        self.approved.add(candidate)
        proof = self.tasks['A'].with_name('phase-result.json')
        proof.write_bytes(proof.read_bytes() + b' ')

        final = self.dispatch()
        self.assertEqual(final['status'], 'blocked')
        self.assertEqual(final['phases']['A']['status'], 'blocked')
        self.assertEqual(final['phases']['B']['status'], 'waiting-dependencies')
        self.assertEqual(final['phases']['D']['status'], 'waiting-dependencies')
        self.assertEqual(final['phases']['C']['status'], 'verified')
        self.assertEqual(self.measured, ['A', 'C'])
        self.assertEqual(self.launched, [self.trunk, self.trunk])
        self.assertEqual(self.resumed, [])
        self.assertGreaterEqual(self.audited.count('A'), 2)

    def test_dispatch_selects_approved_local_gate_before_binding_and_execution(self):
        policy = {'files': ['src/gateway/channel-health-policy.test.ts'], 'reason': 'Check Gateway health policy.'}
        self.contract['steps']['A']['local_gate'] = policy
        seen = []

        def select(data, head, *, files, reason):
            seen.append(head)
            data['local_gate'] = {'files': files, 'reason': reason}

        execute = self.execute

        def check(task, issue, image, **options):
            data = cli.read(task)
            if self.read(data)['entry']['id'] == 'A':
                self.assertEqual(data['local_gate'], policy)
                self.assertEqual(seen, [self.trunk])
            return execute(task, issue, image, **options)

        self.execute = check
        with patch('workbench.local_gate._select', side_effect=select):
            result = self.dispatch()
        self.assertEqual(result['phases']['A']['status'], 'awaiting-operator-review', result)
        self.assertEqual(seen, [self.trunk])

    def test_rechecks_prior_independent_review_before_dispatching_dependents(self):
        first = self.dispatch()
        self.approved.add(first['phases']['A']['commit'])
        review = self.tasks['A'].with_name('independent-review.json')
        review.write_bytes(review.read_bytes() + b' ')

        final = self.dispatch()
        self.assertEqual(final['status'], 'blocked')
        self.assertIn('independent proof changed', final['phases']['A']['reason'])
        self.assertEqual(final['phases']['B']['status'], 'waiting-dependencies')
        self.assertEqual(self.measured, ['A', 'C'])
        self.assertEqual(len(self.launched), 2)

    def test_dependency_changed_during_baseline_measurement_blocks_launch(self):
        first = self.dispatch()
        self.approved.add(first['phases']['A']['commit'])
        independent_review = self.tasks['A'].with_name('independent-review.json')
        measure = self.measure

        def change_prior_review(contract, phase_id, out, *, timeout):
            result = measure(contract, phase_id, out, timeout=timeout)
            if phase_id == 'B':
                independent_review.write_bytes(independent_review.read_bytes() + b' ')
            return result

        self.measure = change_prior_review
        result = self.dispatch()
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('independent proof changed', result['phases']['B']['reason'])
        self.assertEqual(result['phases']['D']['status'], 'waiting-dependencies')
        self.assertEqual(self.measured, ['A', 'C', 'B'])
        self.assertEqual(self.launched, [self.trunk, self.trunk])

    def test_retained_paused_phase_resumes_without_repeating_baseline_or_launch(self):
        self.fail_once.add('A')
        first = self.dispatch()
        self.assertEqual(first['status'], 'blocked')
        self.assertEqual(first['phases']['A']['status'], 'blocked')
        self.assertEqual(self.measured, ['A', 'C'])
        self.assertEqual(self.launched, [self.trunk, self.trunk])

        second = self.dispatch()
        self.assertEqual(second['status'], 'waiting')
        self.assertEqual(second['phases']['A']['status'], 'awaiting-operator-review')
        self.assertEqual(self.resumed, ['A'])
        self.assertEqual(self.measured, ['A', 'C'])
        self.assertEqual(self.launched, [self.trunk, self.trunk])
