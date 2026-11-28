import argparse
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from workbench import architect, autopilot, cli, delivery, feature_context, prototype, workflow
from workbench.prompt_profiles import PSTACK_COMMIT


class FeatureContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        self.source = self.repo / 'driver.py'
        self.source.write_text('def run(data):\n    return data\n')
        (self.repo / '.gitignore').write_text('ignored.txt\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'pin baseline')
        self.baseline = cli.sha(self.repo, 'HEAD')
        self.task = self.root / 'task' / 'task.json'
        self.manifest = self.root / 'handoff.json'
        self.design = self.root / 'design.json'
        self.criterion = {'id': 'result', 'text': 'Run the real CLI against the fixed input.'}
        verification = [{'kind': 'static', 'expectation': 'Check that the method changes the value.'},
                        {'kind': 'runtime', 'expectation': self.criterion['text']}]
        self.approved = {'schema': cli.SCHEMA, 'status': 'selected', 'commit': self.baseline,
                         'pstack_commit': PSTACK_COMMIT, 'implementation_scope': ['driver.py'],
                         'obligations': [{'id': f'C{number}', **row}
                                         for number, row in enumerate(verification, 1)],
                         'sources': {'S1': {'kind': 'source', 'start': 1, 'end': 2,
                                            'text': 'EXCLUDED RAW SOURCE'}},
                         'selected': {'title': 'Driver interface', 'summary': 'Update the fixed input.',
                                      'modules': [{'name': 'Driver', 'duty': 'Own the input.'},
                                                  {'name': 'Runtime', 'duty': 'Execute the interface.'}],
                                      'state': [{'name': 'input', 'owner': 'Driver'}],
                                      'interfaces': [{'signature': 'run(data)', 'owner': 'Driver', 'caller': 'CLI'}],
                                      'tradeoffs': ['A stable interface limits the change.'],
                                      'risks': ['Runtime verification remains.'], 'verification': verification,
                                      'findings': [{'text': 'The function returns its input.', 'basis': 'observed',
                                                    'citations': [{'id': 'S1', 'line': 2}]}]}}
        cli.write(self.design, self.approved)
        self.input = {'design': self.ref(self.design, relative=True), 'review_model': 'gpt-fixture',
                      'criteria': [self.criterion],
                      'checkpoint': {'blocking': 'Shape is approved.', 'workstreams': 'Use one worktree.',
                                     'shared_state': 'Driver owns input.', 'decomposition': 'n/a: one path.'}}
        self.state = {'kind': 'feature', 'base_commit': self.baseline, 'repo': str(self.repo),
                      'criteria': [{**self.criterion, 'revision': 1}], 'events': []}

    def ref(self, path, *, relative=False):
        return {'path': path.name if relative else str(path), 'sha256': cli.digest(path)}

    def load(self):
        cli.write(self.manifest, self.input)
        return feature_context.load_input(self.manifest, cli.digest(self.manifest), self.repo)

    def freeze(self):
        return feature_context.freeze(self.task, self.state, self.load())

    def commit(self):
        self.source.write_text('def run(data):\n    return data + 1\n')
        cli.git(self.repo, 'commit', '-qam', 'implement candidate')
        return cli.sha(self.repo, 'HEAD')

    def test_feature_binding_is_private_and_detects_metadata_and_criteria_drift(self):
        self.assertIsNone(feature_context.read(self.state))
        metadata = self.freeze()
        context = feature_context.read(self.state)
        self.assertEqual(context['scope'], ['driver.py'])
        self.assertEqual(context['design'], self.approved)
        self.assertEqual(feature_context.runtime_criteria(context), {'result'})
        self.assertTrue(all(stat.S_IMODE(Path(metadata[key]).stat().st_mode) == 0o600
                            for key in ('path', 'design_path')))
        prompt = feature_context.prompt(context)
        self.assertIn('run(data)', prompt)
        self.assertIn(self.criterion['text'], prompt)
        self.assertNotIn('EXCLUDED RAW SOURCE', prompt)
        self.state['criteria'][0]['text'] = 'Only check startup.'
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'criteria changed'):
            feature_context.read(self.state)
        self.state['criteria'][0]['text'] = self.criterion['text']
        self.state.pop('feature_context')
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'binding changed'):
            feature_context.read(self.state)

    def test_handoff_requires_approved_digest_exact_baseline_and_runtime_acceptance(self):
        self.input['criteria'] = [{'id': 'result', 'text': 'Only check startup.'}]
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'runtime expectation'):
            self.load()
        self.input['criteria'] = [self.criterion]
        self.input['checkpoint']['decomposition'] = 'n/a'
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'reason'):
            self.load()
        self.input['checkpoint']['decomposition'] = 'One path.'
        self.input['design']['sha256'] = 'a' * 64
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'digest'):
            self.load()
        self.input['design'] = self.ref(self.design, relative=True)
        self.commit()
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'exact checkout baseline'):
            self.load()

    def test_scope_covers_staged_unstaged_ignored_paths_and_the_committed_candidate(self):
        self.freeze()
        self.source.write_text('def run(data):\n    return data + 1\n')
        cli.git(self.repo, 'add', 'driver.py')
        self.source.write_text('def run(data):\n    return data\n')
        self.assertEqual(feature_context.check_scope(self.state), ['driver.py'])
        ignored = self.repo / 'ignored.txt'
        ignored.write_text('outside scope\n')
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'ignored.txt'):
            feature_context.check_scope(self.state)
        ignored.unlink()
        candidate = self.commit()
        self.assertEqual(feature_context.check_scope(self.state, candidate), ['driver.py'])
        (self.repo / 'outside.txt').write_text('outside\n')
        cli.git(self.repo, 'add', 'outside.txt')
        cli.git(self.repo, 'commit', '-qm', 'exceed scope')
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'outside.txt'):
            feature_context.check_scope(self.state, cli.sha(self.repo, 'HEAD'))

    def test_native_prototype_contributes_a_summary_and_rechecks_its_source_receipts(self):
        path = self.root / 'experiment.json'
        out = self.root / 'experiment'
        question = 'Which scratch option retains the input?'
        image = 'sha256:' + '1' * 64
        cli.write(path, {'repo': str(self.repo), 'revision': self.baseline, 'question': question,
                         'surface': 'behavior', 'image': 'fixture:local', 'entry': 'driver.mjs',
                         'criteria': 'Check the retained input.', 'sources': ['driver.py:1-2'],
                         'variants': [{'id': 'a', 'label': 'A', 'approach': 'Reset the input.'},
                                      {'id': 'b', 'label': 'B', 'approach': 'Retain the input.'}]})
        prepared = self.root / 'prepared'
        prepared.mkdir()
        (prepared / 'driver.mjs').write_text('SECRET PROTOTYPE CODE')

        def sandbox(workspace, image, command, timeout, **options):
            self.assertTrue(options['workspace_read_only'])
            answer = {'kind': 'iboard-prototype', 'variant': command[2], 'surface': 'behavior',
                      'observation': {'retained': command[2] == 'b'}}
            return {'image': image, 'command': command, 'stdout': json.dumps(answer), 'exit_code': 0,
                    'container_created': True, 'cleanup_confirmed': True, 'workspace_read_only': True}

        def recommend(prompt, model, timeout, **options):
            return {'choice': 'b', 'conclusion': 'Keep the input.',
                    'tradeoffs': [{'variant': 'a', 'evidence': 'a', 'tradeoff': 'Drops input.'},
                                  {'variant': 'b', 'evidence': 'b', 'tradeoff': 'Retains input.'}]}

        result = prototype.run(path, out, 'gpt-fixture', prepared=prepared, sandbox=sandbox,
                               reviewer=recommend, resolve_image=lambda name, timeout: image)
        self.assertEqual(result['status'], 'recommended')
        ref = self.ref(out / 'result.json')
        self.input['prototypes'] = [ref]
        self.freeze()
        context = feature_context.read(self.state)
        self.assertEqual(context['prototypes'], [{'choice': 'b', 'conclusion': 'Keep the input.',
                                                 'question': question, 'sha256': ref['sha256']}])
        self.assertNotIn('SECRET PROTOTYPE CODE', feature_context.prompt(context))
        self.assertFalse((self.task.parent / 'driver.mjs').exists())
        (out / 'a.json').write_text('{}')
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'recorded digest'):
            self.load()

    def test_review_matches_native_receipt_and_does_not_clear_pending_runtime(self):
        metadata = self.freeze()
        candidate = self.commit()
        out = self.root / 'review'

        def review_diff(prompt, model, timeout):
            data = json.loads(prompt.rsplit('\n', 1)[-1])
            line = next(int(row.split(':', 1)[0]) for row in data['diff'].splitlines()
                        if '+    return data + 1' in row)
            return {'summary': 'The driver transforms its input.',
                    'checks': [{'id': 'C1', 'status': 'supported', 'reason': 'Changed return.', 'diff_lines': [line]}]}

        receipt = architect.review(repo=self.repo, revision=candidate, design=metadata['design_path'],
                                   design_sha256=metadata['design_sha256'], model='gpt-fixture',
                                   out=out, openai=review_diff)
        path = out / 'result.json'
        reference = self.ref(path)
        verified = feature_context.verify_review(self.state, candidate, reference)
        self.assertEqual(verified, receipt)
        self.assertEqual(verified['runtime_pending'], ['C2'])
        receipt['verification']['checks'][-1]['status'] = 'supported'
        cli.write(path, receipt)
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'digest'):
            feature_context.verify_review(self.state, candidate, reference)
        with self.assertRaisesRegex(feature_context.FeatureContextError, 'pending runtime'):
            feature_context.verify_review(self.state, candidate, self.ref(path))

    def _run_autopilot_with_handoff(self, *, arena_candidate=False):
        task_info = workflow.prepare(task_id='feature-case', repo=self.repo, tasks_root=self.root / 'tasks',
                                     workspaces_root=self.root / 'workspaces', source='feature request',
                                     kind='feature', surface='non-ui', iterations=2, seconds=120,
                                     implementer='fixture')
        task = Path(task_info['task'])
        workspace = Path(task_info['workspace'])
        issue = task.parent / 'issue.txt'
        issue.write_text('Change the fixed driver input and preserve its interface.\n')
        selected = None
        if arena_candidate:
            candidate_tree = self.root / 'arena-candidate'
            cli.git(self.repo, 'worktree', 'add', '--detach', str(candidate_tree), self.baseline)
            (candidate_tree / 'driver.py').write_text('def run(data):\n    return data + 1\n')
            cli.git(candidate_tree, 'commit', '-qam', 'candidate from Arena')
            selected = cli.sha(candidate_tree, 'HEAD')
            self.approved['arena_resolution'] = {'selected': 'B', 'source_sha256': 'f' * 64}
            cli.write(self.design, self.approved)
            self.input['design'] = self.ref(self.design)
            receipt = self.root / 'arena-resolution.json'
            cli.write(receipt, {'schema': cli.SCHEMA, 'status': 'resolved'})
            self.input['arena'] = self.ref(receipt)
            binding = patch('workbench.feature_arena.validate_receipt', return_value={
                'candidate': selected, 'request_sha256': cli.digest(issue), 'selected_design': 'B',
                'source_status': 'contested', 'criteria': self.input['criteria'],
                'checkpoint': self.input['checkpoint'], 'review_model': self.input['review_model'],
                'prototypes': [], 'image': 'local-image', 'source_mode': 'workspace', 'interrogation': None,
            })
            binding.start()
            self.addCleanup(binding.stop)
        data = cli.read(task)
        cli.criterion_add(argparse.Namespace(**self.criterion, reason='frozen handoff'), data)
        feature_context.freeze(task, data, self.load())
        cli.write(task, data)
        spec = {'command': ['python3', '/workspace/driver.py'], 'marker': 'OBSERVED', 'exit_code': 0}
        planning = []

        def model(repo, prompt, timeout, *, read_only):
            self.assertIn('APPROVED FEATURE HANDOFF', prompt)
            if 'Return only JSON:' in prompt:
                planning.append(prompt)
                plan = {'status': 'ready', 'criteria': [self.criterion], 'resolution': spec,
                        'tests': spec, 'feature_doc': 'driver.py'}
                output = json.dumps(plan) + ('\nExtra note.' if len(planning) == 1 else '')
            elif not read_only:
                self.assertFalse(arena_candidate, 'the retained Arena commit is already implemented')
                (repo / 'driver.py').write_text('def run(data):\n    return data + 1\n')
                output = 'implemented'
            else:
                self.assertIn('cite an independent resolution report from R4', prompt)
                output = json.dumps({'status': 'ready', 'verdict': 'PASS', 'assessments': [
                    {'id': self.criterion['id'], 'verdict': 'PASS', 'reason': 'Observed from the driver.',
                     'evidence': ['R4']}]})
            return {'exit_code': 0, 'stdout': output}

        def check(*, kind, command, expect_exit, marker, actor, description, **unused):
            current = cli.read(task)
            head = cli.sha(workspace, 'HEAD')
            path = task.parent / f'{kind}-{actor}.json'
            cli.write(path, {'kind': kind, 'actor': actor, 'commit': head, 'container_created': True,
                             'cleanup_confirmed': True, 'exit_code': expect_exit, 'expected_exit': expect_exit,
                             'expected_marker': marker, 'stdout': marker, 'workspace': str(workspace),
                             'command': command, 'source_mode': 'workspace', 'source_verified_commit': head})
            cli.evidence_add(argparse.Namespace(file=path, kind=kind, commit=head, actor=actor,
                                                description=description, criterion=[]), current)
            cli.write(task, current)
            return {'report': str(path)}

        def review(**options):
            def audit(prompt, model, timeout):
                diff = json.loads(prompt.rsplit('\n', 1)[-1])['diff']
                line = next(int(row.split(':', 1)[0]) for row in diff.splitlines() if '+    return data + 1' in row)
                return {'summary': 'The changed return preserves the API.', 'checks': [
                    {'id': 'C1', 'status': 'supported', 'reason': 'The return changes.', 'diff_lines': [line]}]}
            return architect.review(**options, openai=audit)

        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            autopilot.run(task, issue, 'local-image', model=model, check=check, architecture_review=review)
        self.assertEqual(len(planning), 2)
        self.assertIn('invalid or ambiguous JSON', planning[1])
        data = cli.read(task)
        candidate = cli.sha(workspace, 'HEAD')
        delivery._independent_verification(data, candidate)
        if arena_candidate:
            adopted = [event for event in data['events'] if event['type'] == 'candidate-adopted']
            self.assertEqual(candidate, selected)
            self.assertEqual([(event['commit'], event['source']) for event in adopted], [(selected, 'feature-arena')])
            self.assertEqual(data['iterations'], 1)
            with self.assertRaisesRegex(delivery.DeliveryError, 'Interrogate evidence'):
                delivery._independent_verification(data, candidate, destination='user/fork')
            issue.write_text('Different request.\n')
            with self.assertRaisesRegex(delivery.DeliveryError, 'request differs'):
                delivery._independent_verification(data, candidate)
            return

        retained = next(item for item in data['evidence'] if item['kind'] == 'independent-review')
        path = Path(retained['path'])
        report = json.loads(path.read_text())
        for replacement in ({'architecture': None}, {'assessments': [{**report['assessments'][0], 'evidence': ['R5']}]}):
            with self.subTest(replacement=replacement):
                cli.write(path, {**report, **replacement})
                retained['sha256'] = cli.digest(path)
                with self.assertRaisesRegex(delivery.DeliveryError, 'independent PASS'):
                    delivery._independent_verification(data, candidate)

    def test_autopilot_and_delivery_require_candidate_architecture_and_runtime_evidence(self):
        self._run_autopilot_with_handoff()

    def test_arena_winner_moves_into_feature_verification_without_a_new_candidate(self):
        self._run_autopilot_with_handoff(arena_candidate=True)
