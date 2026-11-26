import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, multiphase, phase_contract, phase_reverify, workflow


class PhaseContractTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repository'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        for name, body in {'app.mjs': 'export default 1;\n', 'unit.mjs': 'console.log("UNIT_PASS");\n',
                           'live.mjs': 'console.log("LIVE_PASS");\n', 'DOC.md': 'Run the application.\n'}.items():
            (self.repo / name).write_text(body)
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'pin existing files')
        self.baseline = cli.sha(self.repo, 'HEAD')
        self.id = 'P1'
        self.probe = 'Measure the existing app unit.'
        self.base_image = 'sha256:' + 'b' * 64
        self.head_image = 'sha256:' + 'c' * 64
        self.result_digest = 'd' * 64
        self.old = datetime.now(timezone.utc) - timedelta(hours=1)
        self.image_memory = 1024
        self.entry = {'id': self.id, 'base': 'trunk', 'depends_on': [], 'files': ['app.mjs'],
                      'change': 'Update the application output.', 'observed_result': 'Existing callers get the set value.',
                      'interaction': True, 'citations': [{'id': 'S1', 'line': 1}],
                      'verify': {'unit': {'scenario': 'Inspect the function output.', 'predicate': 'Function stays callable.',
                                          'marker': 'UNIT_PASS'},
                                 'live': {'scenario': 'Run the application.', 'predicate': 'Application exits normally.',
                                          'marker': 'LIVE_PASS'},
                                 'perf': {'probe': self.probe, 'metric': 'median_ms', 'max_ms': 250}}}
        self.plan_inputs = {'repo': str(self.repo), 'revision': self.baseline, 'request': 'Develop in one phase.',
                            'files': ['app.mjs:1-1'], 'scope': ['app.mjs'], 'obvious': False,
                            'open_questions': [], 'prototypes': [],
                            'plan': {'summary': 'Change output.', 'phases': [self.entry]}}
        source = self.root / 'plan-input.json'
        cli.write(source, self.plan_inputs)
        multiphase.plan(source, self.root / 'plan')
        self.bench_spec = {'claim': self.probe, 'scope': 'end-to-end', 'configuration': 'Controlled app run.',
                           'production_tuning': None, 'minimum_delta_pct': 25, 'rounds': 5,
                           'workload': {'helper': 'measure.mjs', 'input': 'Known input.', 'completed': 1,
                                        'digest': self.result_digest, 'max_ops_per_second': 1000,
                                        'plausibility': 'One observed call.'}}
        self.bench = self.root / 'benchmark.json'
        cli.write(self.bench, self.bench_spec)
        self.helper = self.root / 'measure.mjs'
        self.helper.write_text('console.log("fixture benchmark");\n')
        self.step = {'image': self.base_image, 'unit': ['node', '/app/unit.mjs'], 'live': ['node', '/app/live.mjs'],
                     'feature_doc': 'DOC.md', 'benchmark': self.reference(self.bench),
                     'helpers': {self.helper.name: self.reference(self.helper)}}
        self.manifest = self.root / 'dispatch.json'
        self.options = {'repo': str(self.repo), 'plan': self.reference(self.root / 'plan/result.json'),
                        'steps': {self.id: self.step}, 'seconds': 360, 'iterations': 2, 'destination': 'user/fork',
                        'memory_mb': self.image_memory}

    @staticmethod
    def reference(path):
        return {'path': str(path), 'sha256': cli.digest(path)}

    def load(self):
        cli.write(self.manifest, self.options)
        return phase_contract.load(self.manifest, cli.digest(self.manifest))

    def benchmark(self, directory, candidate, *, offset, elapsed=90):
        root = self.root / directory
        frozen = root / 'frozen'
        frozen.mkdir(parents=True)
        helper = frozen / self.helper.name
        helper.write_bytes(self.helper.read_bytes())
        helper_hash = cli.digest(helper)
        contract = frozen / 'contract.json'
        commits = {'baseline': self.baseline, 'candidate': candidate}
        cli.write(contract, {**self.bench_spec, 'schema': cli.SCHEMA, 'commits': commits,
                             'helpers': {helper.name: helper_hash}, 'memory_mb': self.image_memory, 'cpu_limit': 2})
        images = {'baseline': self.base_image, 'candidate': self.base_image if candidate == self.baseline else self.head_image}
        samples = []
        for index in range(1, 6):
            for side in ('baseline', 'candidate'):
                path = root / f'{index}-{side}.json'
                measured = {'kind': 'iboard-benchmark', 'unit': 'ms', 'completed': 1, 'errors': 0,
                            'digest': self.result_digest, 'elapsed_ms': 90 if side == 'baseline' else elapsed}
                cli.write(path, {'schema': cli.SCHEMA, 'commit': commits[side], 'container_created': True,
                                 'cleanup_confirmed': True, 'workspace_read_only': True, 'exit_code': 0,
                                 'elapsed_seconds': 0.5, 'image': images[side], 'helper_sha256': helper_hash,
                                 'stdout': json.dumps(measured),
                                 'started_at': (self.old + timedelta(seconds=offset + len(samples))).isoformat()})
                samples.append({'lane': 'workload', 'round': index, 'side': side, 'report': path.name,
                                'sha256': cli.digest(path), 'measurement': measured})
        result = root / 'result.json'
        cli.write(result, {'schema': cli.SCHEMA, 'outcome': 'no-measurable-difference', 'issues': [],
                           'order': 'baseline,candidate', 'samples': samples, 'commits': commits,
                           'images': images, 'contract_sha256': cli.digest(contract)})
        return self.reference(result)

    def bind(self):
        contract = self.load()
        measure = self.benchmark('baseline', self.baseline, offset=0)
        info = workflow.prepare(task_id='phase-one', repo=self.repo, tasks_root=self.root / 'tasks',
                                workspaces_root=self.root / 'worktrees', source='approved phase fixture', kind='feature',
                                surface='non-ui', iterations=2, seconds=300, implementer='fixture')
        task = Path(info['task'])
        data = cli.read(task)
        for criterion in phase_contract.criteria(self.entry):
            cli.criterion_add(argparse.Namespace(**criterion, reason='frozen intake'), data)
        issue = task.parent / 'issue.txt'
        issue.write_text(phase_contract.request(contract, self.id))
        phase_contract.bind(task, data, contract, self.id, self.baseline, issue, measure)
        cli.write(task, data)
        return task, data, measure

    def candidate(self, data, content='export default 2;\n'):
        workspace = Path(data['repo'])
        (workspace / 'app.mjs').write_text(content)
        cli.git(workspace, 'commit', '-qam', 'candidate')
        return cli.sha(workspace, 'HEAD')

    def record(self, task, data, candidate, before, benchmark_dir='candidate-bench'):
        proofs = {}
        for name, kind in (('unit', 'test-result'), ('live', 'resolution')):
            proof = task.parent / (name + '.json')
            cli.write(proof, {'schema': cli.SCHEMA, 'commit': candidate, 'container_created': True,
                              'cleanup_confirmed': True, 'workspace_read_only': True, 'exit_code': 0,
                              'image': self.head_image, 'source_mode': 'image', 'verified_image_id': self.head_image,
                              'source_verified_commit': candidate, 'workspace': data['repo'], 'kind': kind,
                              'actor': 'independent-verifier', 'command': self.step[name],
                              'source_root': '/app', 'memory_mb': self.image_memory, 'expected_exit': 0,
                              'expected_marker': self.entry['verify'][name]['marker'],
                              'stdout': self.entry['verify'][name]['marker'] + '\n'})
            cli.evidence_add(argparse.Namespace(file=str(proof), kind=kind, commit=candidate,
                                                actor='independent-verifier', description=name, criterion=[]), data)
            proofs[name] = self.reference(proof)
        return {'commit': candidate, 'baseline_perf': before,
                'perf': self.benchmark(benchmark_dir, candidate, offset=100, elapsed=120), **proofs}

    def test_request_successful_proof_and_review_are_frozen_at_publish(self):
        task, data, before = self.bind()
        context = phase_contract.read(data)
        self.assertEqual(phase_contract.task_plan(context)['resolution']['command'], self.step['live'])
        candidate = self.candidate(data)
        with self.assertRaisesRegex(phase_contract.PhaseContractError, 'lacks committed'):
            phase_contract.require_delivery(data, candidate)
        proof = self.record(task, data, candidate, before)
        observed = phase_contract.record_completion(task, data, proof)
        cli.write(task, data)
        self.assertEqual(observed['median_ms'], 120)
        self.assertEqual(phase_contract.require_delivery(data, candidate)['status'], 'awaiting-operator-review')
        with self.assertRaisesRegex(phase_contract.PhaseContractError, 'not authorized'):
            phase_contract.require_delivery(data, candidate, destination='other/fork')
        with self.assertRaisesRegex(phase_contract.PhaseContractError, 'awaits operator'):
            phase_contract.require_delivery(data, candidate, destination='user/fork')
        with patch('workbench.delivery._independent_verification'):
            phase_contract.approve_interaction(task, expected_candidate=candidate,
                                                expected_result_sha256=data['phase_result']['sha256'],
                                                approved_by='operator', reason='Reviewed the changed interaction.')
        data = cli.read(task)
        self.assertTrue(phase_contract.interaction_approved(data, candidate))
        self.assertEqual(phase_contract.require_delivery(data, candidate, destination='user/fork'), observed)
        approval = task.parent / phase_contract.APPROVAL_FILE
        approval.write_text(approval.read_text() + '\n')
        with self.assertRaisesRegex(phase_contract.PhaseContractError, 'digest'):
            phase_contract.require_delivery(data, candidate, destination='user/fork')

    def test_request_change_and_out_of_scope_commits_are_refused(self):
        task, data, _ = self.bind()
        candidate = self.candidate(data)
        self.assertEqual(phase_contract.check_scope(data, candidate), ['app.mjs'])
        later = self.candidate(data, 'export default 3;\n')
        self.assertEqual(phase_contract.check_scope(data, later), ['app.mjs'])
        workspace = Path(data['repo'])
        docs = workspace / 'DOC.md'
        original = docs.read_text()
        docs.write_text('Changed beyond scope.\n')
        with self.assertRaisesRegex(phase_contract.PhaseContractError, 'DOC.md'):
            phase_contract.check_scope(data)
        cli.git(workspace, 'commit', '-qam', 'out-of-scope edit')
        docs.write_text(original)
        cli.git(workspace, 'commit', '-qam', 'restore modified documentation')
        with self.assertRaisesRegex(phase_contract.PhaseContractError, 'DOC.md'):
            phase_contract.check_scope(data, cli.sha(workspace, 'HEAD'))
        issue = task.parent / 'issue.txt'
        issue.write_text(issue.read_text() + 'Another request.\n')
        with self.assertRaisesRegex(phase_contract.PhaseContractError, 'digest'):
            phase_contract.read(data)

    def test_verifies_pinned_sources_and_benchmark_inputs_before_dispatch(self):
        contract = self.load()
        self.assertEqual(contract['steps'][self.id]['scripts']['unit']['path'], 'unit.mjs')
        approved = cli.read(Path(contract['plan_path']))
        approved['sources']['S1']['text'] = 'Invented source'
        cli.write(Path(contract['plan_path']), approved)
        self.options['plan'] = self.reference(Path(contract['plan_path']))
        with self.assertRaisesRegex(phase_contract.PhaseContractError, 'source evidence differs'):
            self.load()

    def test_changed_performance_helper_blocks_proof(self):
        task, data, before = self.bind()
        candidate = self.candidate(data)
        proof = self.record(task, data, candidate, before)
        helper = Path(proof['perf']['path']).parent / 'frozen' / self.helper.name
        helper.write_text('console.log("unapproved benchmark");\n')
        with self.assertRaisesRegex(ValueError, 'helper changed'):
            phase_contract.record_completion(task, data, proof)
        self.assertNotIn('phase_result', data)

    def test_replay_binds_fresh_evidence_without_inheriting_operator_approval(self):
        original, data, before = self.bind()
        old_head = self.candidate(data)
        phase_contract.record_completion(original, data, self.record(original, data, old_head, before))
        local_policy = {'mode': 'openclaw-vitest-json-v1', 'files': ['cli.test.ts'], 'reason': 'Run local tests.'}
        data['local_gate'] = local_policy
        cli.write(original, data)
        cli.write(original.with_name('autopilot.json'), {'schema': cli.SCHEMA, 'phase': 'delivery',
                                                         'candidate_commit': old_head})
        old_tree = cli.git(self.repo, 'rev-parse', self.baseline + '^{tree}')
        new_base = cli.git(self.repo, 'commit-tree', old_tree, '-p', self.baseline, '-m', 'land an equivalent tree')
        receipt = self.root / 'replay'
        observed = []

        def launch(**options):
            keys = ('task_id', 'repo', 'tasks_root', 'workspaces_root', 'source', 'kind', 'surface',
                    'iterations', 'seconds')
            prepared = options['prepare'](**{name: options[name] for name in keys}, implementer='fixture')
            task = Path(prepared['task'])
            current = cli.read(task)
            for criterion in options['criteria']:
                cli.criterion_add(argparse.Namespace(**criterion, reason='replayed intake'), current)
            cli.write(task, current)
            request = task.with_name('issue.txt')
            request.write_bytes(Path(options['issue_file']).read_bytes())
            return options['run'](task, request, options['image'])

        def execute(task, request, image):
            current = cli.read(task)
            context = phase_contract.read(current)
            candidate = context['replayed_candidate']
            observed.append(candidate)
            self.assertEqual(context['parent'], new_base)
            self.assertEqual(context['baseline_perf'], before)
            self.assertFalse(phase_contract.interaction_approved(current, candidate))
            cli.git(current['repo'], 'merge', '--ff-only', candidate)
            phase_contract.record_completion(task, current, self.record(task, current, candidate, before, 'replay-bench'))
            cli.write(task, current)
            cli.write(task.with_name('autopilot.json'), {'schema': cli.SCHEMA, 'phase': 'delivery',
                                                         'candidate_commit': candidate})

        def independent(data, candidate):
            return data['phase_result']

        def select(current, head, files, reason):
            self.assertNotEqual(head, old_head)
            self.assertEqual((files, reason), (local_policy['files'], local_policy['reason']))
            current['local_gate'] = {**local_policy, 'base': current['base_commit']}

        with patch.object(cli, 'evidence_gate', side_effect=lambda data, head: (head, [])), \
                patch('workbench.delivery._independent_verification', side_effect=independent), \
                patch('workbench.local_gate._select', side_effect=select), \
                patch('workbench.local_gate.validate'), patch('workbench.local_gate.validate_configuration'):
            phase_contract.approve_interaction(original, expected_candidate=old_head,
                                                expected_result_sha256=data['phase_result']['sha256'],
                                                approved_by='operator', reason='Reviewed original candidate.')
            result = phase_reverify.run(original, new_base, receipt, seconds=180, launch=launch, execute=execute)
            self.assertEqual(result['status'], 'awaiting-operator-review', result)
            self.assertNotEqual(result['candidate'], old_head)
            self.assertEqual(observed, [result['candidate']])
            self.assertEqual(phase_reverify.run(original, new_base, receipt, seconds=180,
                                                launch=launch, execute=execute), result)
            self.assertEqual(observed, [result['candidate']])
            replayed = cli.read(Path(result['task']))
            self.assertEqual(replayed['local_gate'], {**local_policy, 'base': new_base})
            with self.assertRaisesRegex(phase_contract.PhaseContractError, 'awaits operator'):
                phase_contract.require_delivery(replayed, result['candidate'], destination='user/fork')
            replayed.pop('local_gate')
            with self.assertRaisesRegex(phase_contract.PhaseContractError, 'retain the original local test policy'):
                phase_contract.read(replayed)
