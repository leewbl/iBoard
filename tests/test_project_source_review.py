import json
from pathlib import Path
import tempfile
import unittest
from threading import Barrier, Event

from workbench import cli, project_source_review, project_verify, swarm_investigate


IMAGE = 'sha256:' + 'a' * 64


class ProjectSourceReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Verification fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        helpers = self.root / 'helpers'
        helpers.mkdir()
        checks = []
        for name in ('doctor', 'first', 'second'):
            marker = f'JOURNEY_{name.upper()}_PASS'
            helper = name + '.sh'
            (self.repo / f'{name}.ts').write_text(
                f'// committed {name} behavior\nexport const {name}Result = "{name} ready";\n',
                encoding='utf-8',
            )
            (helpers / helper).write_text(f"printf '%s\\n' '{marker}'\n", encoding='utf-8')
            checks.append({
                'id': name, 'helper': helper, 'marker': marker, 'reach': f'Open {name}.',
                'drive': f'Run {name}.', 'observe': f'Expect {name} ready.', 'gotchas': 'Use an isolated instance.',
                'source': [{'path': f'{name}.ts', 'contains': f'export const {name}Result'}],
            })
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'Record source fixtures for review')
        self.revision = cli.sha(self.repo, 'HEAD')
        contract = {
            'project': 'fixture', 'surface': 'Fixture CLI', 'launch': 'Use an isolated workdir.',
            'cleanup': 'Remove test data.', 'doctor': checks[0], 'features': checks[1:],
        }
        self.skill = self.root / 'verify-fixture'
        project_verify._write_bundle(self.skill, project_verify._artifacts(contract, helpers))
        self.drives = []

    def review(self, invoke, name='review', parallelism=2, **kwargs):
        return project_source_review.review(
            repo=self.repo, revision=self.revision, skill=self.skill, provider='glm', model='glm-fixture',
            out=self.root / name, parallelism=parallelism, timeout=30, invoke=invoke, **kwargs,
        )

    def audit(self, receipt):
        return project_source_review.audit(self.repo, self.revision, self.skill, receipt['path'], receipt['sha256'])

    @staticmethod
    def context(prompt):
        return json.loads(prompt.splitlines()[-1])

    @staticmethod
    def citation(context, label, fragment):
        for raw in context['evidence'][label]['text'].splitlines():
            number, _, quote = raw.partition(': ')
            if fragment in quote:
                return {'id': label, 'line': int(number), 'quote': quote}
        raise AssertionError(f'{fragment!r} absent from evidence {label}')

    def reply(self, context, *, status='aligned'):
        name = Path(context['evidence']['S1']['path']).stem
        source = self.citation(context, 'S1', f'{name}Result')
        note = None
        if status == 'drift':
            note = {'text': 'Clarify the documented result.',
                    'citations': [source, self.citation(context, 'M1', f'Expect {name} ready.')]}
        elif status == 'uncertain':
            note = {'text': 'The supplied code does not establish the startup requirement.', 'citations': [source]}
        return {'answer': {'status': status, 'summary': f'Check the {name} result.', 'citations': [source],
                           'note': note, 'recipe': {'entry': f'Open {name}.', 'action': f'Run {name}.',
                                                    'expect': f'Expect {name} ready.'}},
                'observed_model': 'glm-fixture'}

    def maintain(self, receipt, name='maintenance'):
        def attest(repo, image, timeout, *, commit):
            self.assertEqual((repo, image, commit), (self.repo, 'local-image', self.revision))
            return IMAGE

        def drive(scratch, image, command, timeout, **opts):
            check = Path(command[-1]).stem
            self.drives.append(check)
            return {'image': image, 'command': command, 'workspace_read_only': True, 'container_created': True,
                    'cleanup_confirmed': True, 'exit_code': 0, 'stdout': f'JOURNEY_{check.upper()}_PASS\n'}

        return project_verify.run(
            'maintain', repo=self.repo, revision=self.revision, image='local-image', skill=self.skill,
            out=self.root / name, sandbox=drive, attester=attest,
            source_review=receipt['path'], source_review_sha256=receipt['sha256'],
        )

    def test_runs_one_independent_source_call_per_feature_concurrently(self):
        (self.repo / 'first.ts').write_text('uncommitted replacement\n', encoding='utf-8')
        both_started = Barrier(2)
        second_finished = Event()
        finished = []

        def invoke(engine, prompt, seconds):
            context = self.context(prompt)
            source = context['evidence']['S1']
            name = Path(source['path']).stem
            other = 'second' if name == 'first' else 'first'
            self.assertEqual(engine, {'provider': 'glm', 'model': 'glm-fixture'})
            self.assertEqual(context['revision'], self.revision)
            self.assertGreater(seconds, 0)
            self.assertEqual(set(context['evidence']), {'M1', 'H1', 'S1'})
            self.assertNotIn(f'committed {other} behavior', prompt)
            self.assertNotIn('uncommitted replacement', prompt)
            self.assertIn(f'committed {name} behavior', source['text'])
            both_started.wait(10)
            if name == 'first':
                self.assertTrue(second_finished.wait(10))
            finished.append(name)
            if name == 'second':
                second_finished.set()
            reply = self.reply(context)
            if name == 'first':
                citation = reply['answer']['citations'][0]
                quote = citation['quote'].strip()
                reply['answer']['citations'] = [{**citation, 'quote': quote[:length]} for length in range(5, 13)]
            return reply

        receipt = self.review(invoke)
        self.assertEqual(finished, ['second', 'first'])
        self.assertEqual(receipt['status'], 'reviewed')
        reviewed = self.audit(receipt)
        self.assertEqual(reviewed['status'], 'reviewed')
        self.assertEqual(list(reviewed['rows']), ['first', 'second'])
        self.assertEqual(len(reviewed['rows']['first']['answer']['citations']), 8)
        self.assertEqual(cli.digest(receipt['path']), receipt['sha256'])
        maintained = self.maintain(receipt)
        self.assertEqual(maintained['outcome'], 'clean')
        self.assertEqual(self.drives, ['doctor', 'first', 'second'])

    def test_drift_and_uncertainty_block_clean_even_when_all_live_checks_pass(self):
        for status in ('drift', 'uncertain'):
            with self.subTest(status=status):
                self.drives.clear()

                def invoke(engine, prompt, seconds):
                    context = self.context(prompt)
                    name = Path(context['evidence']['S1']['path']).stem
                    return self.reply(context, status=status if name == 'second' else 'aligned')

                receipt = self.review(invoke, f'{status}-review')
                self.assertEqual(receipt['status'], 'review-required')
                self.assertEqual(self.audit(receipt)['status'], 'review-required')
                maintained = self.maintain(receipt, f'{status}-maintenance')
                self.assertEqual(self.drives, ['doctor', 'first', 'second'])
                self.assertEqual(maintained['features'], {'first': 'passed', 'second': 'passed'})
                self.assertEqual(maintained['outcome'], 'blocked')

    def test_invalid_citations_and_provider_unavailability_block_the_source_wave(self):
        def hallucinated(engine, prompt, seconds):
            value = self.reply(self.context(prompt))
            value['answer']['citations'][0]['quote'] = 'invented code'
            return value

        def unavailable(engine, prompt, seconds):
            raise swarm_investigate.ProviderUnavailable('fixture provider unavailable')

        for name, invoke in (('bad-citation', hallucinated), ('unavailable', unavailable)):
            with self.subTest(name=name):
                receipt = self.review(invoke, name)
                self.assertEqual(receipt['status'], 'blocked')
                report = self.audit(receipt)
                self.assertEqual([row['status'] for row in report['rows'].values()], ['blocked', 'blocked'])

    def test_reconciles_duplicate_recipes_without_dropping_feature_reviews(self):
        def invoke(engine, prompt, seconds):
            reply = self.reply(self.context(prompt))
            reply['answer']['recipe'] = {'entry': 'Open the CLI.', 'action': 'Run both fixtures.',
                                         'expect': 'Both results are visible.'}
            return reply

        reviewed = self.audit(self.review(invoke))
        self.assertEqual(list(reviewed['rows']), ['first', 'second'])
        self.assertEqual(len(reviewed['recipe_groups']), 1)
        self.assertEqual(reviewed['recipe_groups'][0]['features'], ['first', 'second'])

    def test_retry_only_dispatches_blocked_workers_and_binds_original_report(self):
        def incomplete(engine, prompt, seconds):
            context = self.context(prompt)
            value = self.reply(context)
            if context['feature'] == 'second':
                value['answer']['citations'][0]['quote'] = 'wrong quote'
            return value

        original = self.review(incomplete)
        old = self.audit(original)
        self.assertEqual(old['rows']['second']['status'], 'blocked')
        dispatched = []

        def complete(engine, prompt, seconds):
            context = self.context(prompt)
            dispatched.append(context['feature'])
            return self.reply(context)

        receipt = self.review(complete, 'retry', retry_report=original['path'], retry_sha256=original['sha256'])
        retried = self.audit(receipt)
        self.assertEqual(dispatched, ['second'])
        self.assertEqual(retried['status'], 'reviewed')
        self.assertEqual(retried['retry_of']['reused'], ['first'])
        self.assertEqual(retried['rows']['first'], old['rows']['first'])
        original_path = Path(original['path'])
        original_path.write_bytes(original_path.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'evidence digest changed'):
            self.audit(receipt)

    def test_refuses_symlinked_feature_documents_before_sending_source(self):
        features = self.skill / 'features'
        detached = self.root / 'outside-feature-documents'
        features.rename(detached)
        features.symlink_to(detached, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'feature documents must be inside'):
            self.review(lambda *_: self.fail('called provider with external feature documents'))

    def test_subdirectory_invocation_cannot_write_evidence_into_worktree(self):
        subdirectory = self.repo / 'subdirectory'
        subdirectory.mkdir()
        output = self.repo / 'review'
        with self.assertRaisesRegex(ValueError, 'outside the target and skill'):
            project_source_review.review(
                repo=subdirectory, revision=self.revision, skill=self.skill, provider='glm', model='glm-fixture',
                out=output, invoke=lambda *_: self.fail('called provider before checking evidence location'),
            )
        self.assertFalse(output.exists())

    def test_rejects_changed_report_and_changed_feature_map(self):
        def invoke(engine, prompt, seconds):
            return self.reply(self.context(prompt))

        receipt = self.review(invoke)
        report = Path(receipt['path'])
        original = report.read_bytes()
        report.write_bytes(original + b'\n')
        altered = self.maintain(receipt, 'altered-report')
        self.assertEqual(altered['outcome'], 'blocked')
        self.assertEqual(self.drives, [])
        report.write_bytes(original)
        feature = self.skill / 'features' / 'first.md'
        feature.write_text(feature.read_text(encoding='utf-8') + '\nChanged map text.\n', encoding='utf-8')
        changed = self.maintain(receipt, 'changed-map')
        self.assertEqual(changed['outcome'], 'blocked')
        self.assertEqual(self.drives, [])

    def test_rehashing_a_report_cannot_hide_removed_rows_or_invented_source(self):
        def invoke(engine, prompt, seconds):
            return self.reply(self.context(prompt))

        original = self.review(invoke)
        path = Path(original['path'])
        record = json.loads(path.read_text(encoding='utf-8'))
        del record['rows']['first']
        path.write_text(json.dumps(record), encoding='utf-8')
        receipt = {**original, 'sha256': cli.digest(path)}
        with self.assertRaisesRegex(ValueError, 'feature coverage changed'):
            self.audit(receipt)
        receipt = self.review(invoke, 'invented-source-review')
        path = Path(receipt['path'])
        record = json.loads(path.read_text(encoding='utf-8'))
        record['rows']['first']['answer']['citations'][0]['quote'] = 'invented source'
        path.write_text(json.dumps(record), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'citation does not match'):
            self.audit({**receipt, 'sha256': cli.digest(path)})


if __name__ == '__main__':
    unittest.main()
