import copy
import json
from pathlib import Path
import tempfile
import unittest

from workbench import arena, cli, feature_arena, feature_context, interrogate, investigate
from workbench.prompt_profiles import PSTACK_COMMIT


class FeatureArenaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        self.app = self.repo / 'app.js'
        self.app.write_text('export const label = (input) => input;\n')
        (self.repo / 'check.js').write_text('import { label } from "./app.js";\n'
                                           'if (label("  Alpha  ") !== "alpha") process.exit(1);\n'
                                           'console.log("NORMALIZED");\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'baseline')
        self.baseline, sources = investigate._collect(self.repo, 'HEAD', ['app.js'], history=False, remaining=lambda: 10)
        self.request = self.root / 'issue.txt'
        self.request.write_bytes(b'Normalize label text.\r\n')
        self.runtime = ['Normalize surrounding whitespace.', 'Normalize uppercase input.']
        self.options = {key: self.option(owner, expectation) for key, owner, expectation in
                        [('A', 'Label module', self.runtime[0]), ('B', 'Input adapter', self.runtime[1])]}
        self.design = self.root / 'architect.json'
        cli.write(self.design, {'schema': cli.SCHEMA, 'status': 'contested', 'commit': self.baseline,
                                'pstack_commit': PSTACK_COMMIT, 'question': 'Where should labels be normalized?',
                                'implementation_scope': ['app.js'], 'alternatives': self.options,
                                'comparison': {'decision': 'contested', 'tradeoff': 'Keep the public API.',
                                               'reasons': {'A': 'Direct owner.', 'B': 'Separate boundary.'},
                                               'risks': []},
                                'sources': sources})
        self.criteria = [{'id': f'case-{index}', 'text': text} for index, text in enumerate(self.runtime)]
        self.approval = {'design': {'path': 'architect.json', 'sha256': cli.digest(self.design)},
                         'review_model': 'gpt-fixture', 'criteria': self.criteria,
                         'checkpoint': {'blocking': 'Pin the script.', 'workstreams': 'Compare isolated alternatives.',
                                        'shared_state': 'No shared writes.', 'decomposition': 'n/a: one feature.'},
                         'arena': {'image': 'offline-image', 'seconds': 120,
                                   'check': {'command': ['node', '/workspace/check.js'],
                                             'marker': 'NORMALIZED', 'exit_code': 0},
                                   'runners': [{'provider': 'glm'}, {'provider': 'glm'}],
                                   'judge_model': 'gpt-fixture',
                                   'rubric': [{'id': f'criterion-{i}', 'text': f'Criterion {i}'} for i in range(3)]}}
        self.manifest = self.root / 'manifest.json'
        self.out = self.root / 'out'
        self.runs = []

    @staticmethod
    def option(owner, expectation):
        return {'title': owner, 'summary': 'Normalize input without changing callers.',
                'modules': [{'name': owner, 'duty': 'Own normalization.'},
                            {'name': 'Consumer', 'duty': 'Consume normalized labels.'}],
                'state': [{'name': 'input', 'owner': owner}],
                'interfaces': [{'signature': 'label(input)', 'owner': owner, 'caller': 'Consumer'}],
                'tradeoffs': ['A canonical transformation has a stable result.'],
                'risks': ['Verify real app behavior.'],
                'verification': [{'kind': 'static', 'expectation': f'Keep normalization in the {owner}.'},
                                 {'kind': 'runtime', 'expectation': expectation}],
                'findings': [{'text': 'The app currently returns input unchanged.', 'basis': 'observed',
                              'citations': [{'id': 'S1', 'line': 1}]}]}

    def edit(self, workspace, prompt, timeout, **kwargs):
        shape, _ = json.JSONDecoder().raw_decode(prompt.split('ASSIGNED DESIGN:\n', 1)[1])
        if shape['title'] == 'Label module':
            code = 'export const label = (input) => input.trim().toLowerCase();\n'
        else:
            code = ('const normalizeInput = (input) => input.trim().toLowerCase();\n'
                    'export const label = (input) => normalizeInput(input);\n')
        (workspace / 'app.js').write_text(code)
        return {'exit_code': 0, 'stdout': json.dumps({'status': 'ready', 'rationale': 'Use the assigned boundary.'})}

    def sandbox(self, workspace, image, command, timeout, *, workspace_read_only, source_root, **kwargs):
        self.runs.append(workspace)
        passed = '.trim().toLowerCase()' in (workspace / 'app.js').read_text()
        return {'workspace': str(workspace), 'image': image, 'command': command, 'source_root': source_root,
                'workspace_read_only': workspace_read_only, 'stdout': 'NORMALIZED' if passed else 'UNNORMALIZED',
                'container_created': True, 'cleanup_confirmed': True, 'exit_code': 0 if passed else 1}

    def score(self, prompt):
        packet = json.loads(prompt.split('\n', 1)[1])
        self.assertTrue(all(set(item) == {'id', 'patch', 'sandbox_check'} for item in packet['candidates']))
        winning = next(item['id'] for item in packet['candidates'] if 'normalizeInput' in item['patch'])
        return {'candidates': [{'id': item['id'], 'scores': [
            {'criterion': criterion['id'], 'score': 4 if item['id'] == winning else 3,
             'why': 'Prefer the explicit adapter.'} for criterion in packet['rubric']]}
            for item in packet['candidates']], 'recommendation': winning}

    def judge(self, prompt, model, timeout):
        if prompt.startswith('Independently verify the selected'):
            packet = json.loads(prompt.split('\n', 1)[1])
            self.assertEqual(packet['approved_design'], self.options['B'])
            self.assertTrue(packet['independent_rerun']['passed'])
            return {'verdict': 'PASS', 'reason': 'The immutable entrypoint exercises the exported label.'}
        return self.score(prompt)

    def run_arena(self, manifest, out, judge=None):
        return arena.run(manifest, out, editor=self.edit, judge=judge or self.judge,
                         picker=lambda prompt, timeout: json.dumps(self.score(prompt)), sandbox=self.sandbox)

    def resolve(self, **kwargs):
        cli.write(self.manifest, self.approval)
        run = kwargs.pop('run', self.run_arena)
        return feature_arena.resolve(self.manifest, cli.digest(self.manifest), self.repo, self.request,
                                     self.out, run=run, **kwargs)

    def validate(self, result, design):
        return feature_arena.validate_receipt(result['report'], result, repo=self.repo, baseline=self.baseline,
                                              design=design, design_sha256=result['design_sha256'])

    def test_resolves_to_exact_winner_and_rechecks_native_offline_proof(self):
        result = self.resolve(seconds_limit=90)
        self.assertEqual(result['status'], 'resolved', result.get('reason'))
        self.assertEqual(result['selected_design'], 'B')
        self.assertEqual(json.loads((self.out / 'arena' / 'contract.json').read_text())['seconds'], 90)
        handoff = json.loads(Path(result['handoff']).read_text())
        self.assertEqual(handoff['arena']['sha256'], cli.digest(result['report']))
        design = cli.read(Path(handoff['design']['path']))
        self.assertEqual(design['selected'], self.options['B'])
        self.assertEqual(design['arena_resolution'], {'selected': 'B', 'source_sha256': cli.digest(self.design)})
        evidence = self.validate(result, design)
        self.assertEqual(evidence['candidate'], result['candidate'])
        self.assertEqual(evidence['criteria'], self.criteria)
        self.assertEqual(evidence['request_sha256'], cli.digest(self.request))
        self.assertEqual(evidence['source_status'], 'contested')
        self.assertEqual(len(self.runs), 4)
        self.assertEqual(cli.sha(self.repo, 'HEAD'), self.baseline)
        self.assertEqual(cli.git(self.repo, 'status', '--porcelain'), '')
        mutated = copy.deepcopy(design)
        mutated['selected'] = self.options['A']
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'exact verified winning'):
            self.validate(result, mutated)
        proof = self.out / 'arena' / 'independent' / 'sandbox.json'
        report = cli.read(proof)
        report['workspace_read_only'] = False
        cli.write(proof, report)
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'artifact digest'):
            self.validate(result, design)

    def test_rejects_missing_runtime_criteria_and_ambiguous_scope_before_running(self):
        self.approval['criteria'] = self.criteria[:1]
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'both proposed architectures'):
            self.resolve()
        self.approval['criteria'] = self.criteria
        self.approval['arena']['files'] = ['check.js']
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'cannot replace'):
            self.resolve()
        self.assertFalse(self.out.exists())
        self.assertEqual(self.runs, [])

    def test_source_changed_after_approval_cannot_be_resolved(self):
        self.app.write_text('export const label = (input) => String(input);\n')
        cli.git(self.repo, 'commit', '-qam', 'move HEAD')
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'exact Feature baseline'):
            self.resolve()
        self.assertEqual(self.runs, [])

    def test_feature_cannot_start_arena_with_a_different_model_profile(self):
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'same approved profile'):
            self.resolve(expected_model_profile='a' * 64)
        self.assertEqual(self.runs, [])
        self.assertFalse(self.out.exists())

    def test_failed_independent_review_withholds_the_design_handoff(self):
        def reject_final(prompt, model, timeout):
            if prompt.startswith('Independently verify the selected'):
                return {'verdict': 'FAIL', 'reason': 'The independent challenge rejected the behavior.'}
            return self.judge(prompt, model, timeout)

        result = self.resolve(run=lambda manifest, out: self.run_arena(manifest, out, judge=reject_final))
        self.assertEqual(result['status'], 'blocked')
        self.assertNotIn('handoff', result)
        self.assertNotIn('candidate', result)
        self.assertFalse((self.out / 'handoff.json').exists())
        self.assertFalse((self.out / 'design.json').exists())
        self.assertEqual(len(self.runs), 4)

    def challenge(self, *, matched):
        self.approval['interrogate'] = {'image': 'offline-image', 'review_model': 'gpt-fixture', 'probes': [
            {'id': 'boundary', 'command': ['node', '/workspace/check.js'], 'marker': 'NORMALIZED',
             'exit_code': 0, 'intent': 'Exercise surrounding spaces and case in the real label API.'}]}

        def sandbox(workspace, image, command, timeout, **options):
            report = self.sandbox(workspace, image, command, timeout, **options)
            report.update(create_exit_code=0, timed_out=False, stdout_truncated=False, stderr_truncated=False,
                          memory_mb=options['memory_mb'], stdout='NORMALIZED' if matched else 'WRONG LABEL')
            return report

        answer = {'summary': 'Examined both runs of the approved probe.', 'verdict': 'clear', 'checks': [
            {'id': 'boundary', 'verdict': 'clear', 'reason': 'The probe calls the changed exported API.',
             'observations': ['a', 'b']}]}

        def run(**options):
            return interrogate.run(**options, sandbox=sandbox, resolve_image=lambda *_: 'sha256:' + 'a' * 64,
                                   glm=lambda *_: json.dumps(answer), openai=lambda *_: answer)

        return run

    def test_interrogation_receipt_is_bound_to_the_selected_handoff(self):
        result = self.resolve(challenge_runner=self.challenge(matched=True))
        self.assertEqual(result['status'], 'resolved', result.get('reason'))
        context = feature_context.load_input(result['handoff'], result['handoff_sha256'], self.repo)
        self.assertEqual(context['arena']['interrogation'], result['interrogation'])
        self.assertEqual(context['arena']['candidate'], result['candidate'])
        proof = self.out / 'interrogate' / 'boundary-b.json'
        proof.write_bytes(proof.read_bytes() + b' ')
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'evidence digest'):
            feature_context.load_input(result['handoff'], result['handoff_sha256'], self.repo)

    def test_failed_interrogation_withholds_an_otherwise_verified_candidate(self):
        result = self.resolve(challenge_runner=self.challenge(matched=False))
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('Interrogate withheld', result['reason'])
        self.assertFalse((self.out / 'handoff.json').exists())
        self.assertEqual(cli.read(self.out / 'arena' / 'result.json')['status'], 'verified')
        self.assertEqual(cli.read(self.out / 'interrogate' / 'result.json')['findings'], ['boundary'])
