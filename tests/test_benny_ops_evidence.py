"""Exercise the evidence views with native Benny receipts and no external requests."""

from pathlib import Path
import unittest
from unittest.mock import patch

import test_benny
import test_benny_correction
import test_benny_reproduce
from workbench import benny, benny_correction, benny_ops_evidence, cli


REPOSITORY = test_benny.RULES['repository']
TASK_ID = 'benny-' + 'a' * 32


def ref(path):
    return {'path': str(path), 'sha256': cli.digest(path)}


def operations(root, status, **fields):
    path = root/'operations.json'
    cli.write(path, {'schema': cli.SCHEMA, 'kind': 'benny-operations', 'status': status,
                     'root': test_benny.ROOT, 'scope': {'repository': REPOSITORY,
                         'workspace': test_benny.WORKSPACE, 'source_channel': test_benny.CHANNEL,
                         'source_root': test_benny.ROOT}, **fields})
    return ref(path)


class BennyOperationsEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_benny_reproduce.BennyReproductionTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.root = self.fixture.root

    def freeze(self, *, artifact=None):
        self.fixture.route = benny.freeze(rules=test_benny.RULES, slack=self.fixture.source.slack,
                                          channel=test_benny.CHANNEL, message_ts=test_benny.ROOT,
                                          out=self.root/'source', artifact=artifact)
        return self.fixture.route

    def reproduce(self, displayed='Reproduced'):
        self.route = self.freeze()
        baseline_path = self.root/'reproduction'
        report = self.fixture.reproduce(out=baseline_path)
        return {'status': report['status'], 'reproduction': ref(baseline_path/'result.json'),
                'operations': operations(self.root, displayed)}

    def assert_safe_view(self, facts):
        content = str(facts)
        for value in (str(self.root), self.fixture.source.root['text'], 'xoxb-private-review-contents'):
            self.assertNotIn(value, content)
        self.assertTrue(all(set(evidence) == {'kind', 'text'} for evidence in facts.values()))

    def test_reproduction_exports_outcomes_and_exact_baseline_without_raw_data(self):
        outcome = self.reproduce()
        facts = benny_ops_evidence.packet(outcome, repository=REPOSITORY)
        self.assertEqual(set(facts), {'S1', 'R1'})
        self.assertIn('observed the fault twice', facts['R1']['text'])
        self.assertIn(self.fixture.base, facts['R1']['text'])
        self.assert_safe_view(facts)
        raw = self.root/'reproduction/baseline/main.primary-1.json'
        raw.write_text(raw.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'frozen sandbox receipt'):
            benny_ops_evidence.packet(outcome, repository=REPOSITORY)

    def test_healthy_runtime_is_not_mislabeled_as_confirmed_reproduction(self):
        self.fixture.runtime.states.clear()
        outcome = self.reproduce('Could not reproduce')
        facts = benny_ops_evidence.packet(outcome, repository=REPOSITORY)
        self.assertIn('was not reproduced', facts['S1']['text'])
        self.assertIn('passed twice', facts['R1']['text'])
        self.assertNotIn('observed the fault twice', str(facts))

    def test_invalid_digest_and_cross_repository_scope_do_not_produce_facts(self):
        outcome = self.reproduce()
        with self.assertRaisesRegex(ValueError, 'settled receipt'):
            benny_ops_evidence.packet(outcome, repository='another/project')
        self.assert_safe_view(benny_ops_evidence.packet(outcome, repository=REPOSITORY))
        path = Path(outcome['reproduction']['path'])
        report = cli.read(path)
        cli.write(path, {**report, 'repository': 'another/project'})
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            benny_ops_evidence.packet(outcome, repository=REPOSITORY)
        with self.assertRaisesRegex(ValueError, 'does not match this event'):
            benny_ops_evidence.packet({**outcome, 'reproduction': ref(path)}, repository=REPOSITORY)

    def test_existing_fix_distinguishes_verified_comparison_from_stale_provider_result(self):
        artifact = f'https://github.com/{REPOSITORY}/pull/11'
        self.freeze(artifact=artifact)
        outer, inner = self.root/'existing/result.json', self.root/'existing/verification/result.json'
        base, head = self.fixture.base, self.fixture.runtime.revision
        pair = {'repository': REPOSITORY, 'url': artifact, 'base': base, 'head': head,
                'kind': 'pull', 'state': 'open'}
        def round_(sha, expected):
            return {'commit': sha, 'observations': {'doctor_healthy': True,
                                                   'primary': [expected] * 2, 'oracle': [expected] * 2}}

        report = {'schema': cli.SCHEMA, 'kind': 'benny-reproduction-result', 'artifact': artifact,
                  'source_route': {'receipt': self.fixture.route['receipt'], 'sha256': self.fixture.route['sha256']},
                  'verification': 'verification/result.json', 'source_recheck': 'unchanged',
                  'runtime_outcome': 'confirmed', 'outcome': 'confirmed'}
        cli.write(outer, report)
        cli.write(inner, {'schema': cli.SCHEMA, 'artifact': pair, 'artifact_recheck': pair,
                          'base_commit': base, 'candidate_commit': head,
                          'outcome': 'confirmed', 'runtime_outcome': 'confirmed',
                          'rounds': {'baseline': round_(base, 'observed-failure'), 'candidate': round_(head, 'pass')}})
        outcome = {'status': 'confirmed', 'existing': ref(outer), 'existing_verification': ref(inner),
                   'operations': operations(self.root, 'Existing fix verified')}
        facts = benny_ops_evidence.packet(outcome, repository=REPOSITORY)
        self.assertIn(f'passed twice each on candidate {head}', facts['X1']['text'])
        self.assert_safe_view(facts)
        cli.write(outer, {**report, 'outcome': 'stale'})
        stale = benny_ops_evidence.packet({**outcome, 'status': 'stale', 'existing': ref(outer),
                                          'operations': operations(self.root, 'Blocked')}, repository=REPOSITORY)
        self.assertEqual(set(stale), {'S1'})
        self.assertIn('moved during verification', stale['S1']['text'])

    def test_draft_link_and_candidate_require_frozen_journal_and_independent_review(self):
        outcome = self.reproduce()
        task = self.root/'tasks'/TASK_ID/'task.json'
        candidate_dir = task.parent/('benny-candidate-' + 'b' * 32)
        original = outcome['reproduction']
        with patch.object(self.fixture, 'receipt', return_value={'receipt': original['path'], 'sha256': original['sha256']}):
            report = self.fixture.after(out=candidate_dir)
        candidate = ref(candidate_dir/'result.json')
        review = task.with_name('independent-review.json')
        cli.write(review, {'verdict': 'PASS', 'candidate_commit': report['head'], 'baseline_commit': report['baseline'],
                           'benny_candidate': candidate, 'private_analysis': 'xoxb-private-review-contents'})
        cli.write(task, {'schema': cli.SCHEMA, 'benny_candidate': candidate,
                         'evidence': [{**ref(review), 'kind': 'independent-review', 'commit': report['head']}]})
        branch, url = 'benny/report-xyz', f'https://github.com/{REPOSITORY}/pull/27'
        journal = task.with_name('benny-draft.json')
        cli.write(journal, {'schema': cli.SCHEMA, 'phase': 'draft', 'pr': 27,
                            'plan': {'repository': REPOSITORY, 'head': report['head'], 'base': report['baseline'],
                                     'branch': branch, 'before_sha256': outcome['reproduction']['sha256'],
                                     'route_sha256': self.route['sha256'], 'after_sha256': candidate['sha256'],
                                     'review_sha256': cli.digest(review)}})
        outcome.update(status='draft', operations=operations(self.root, 'Draft pull request opened', draft={'url': url}),
                       task=str(task), draft_journal=ref(journal), repair={'status': 'draft', 'repo': REPOSITORY,
                           'receipt': str(journal), 'url': url, 'pr': 27, 'branch': branch, 'head': report['head'],
                           'base': report['baseline']})
        facts = benny_ops_evidence.packet(outcome, repository=REPOSITORY)
        self.assertIn(report['head'], facts['V1']['text'])
        self.assertIn(url, facts['D1']['text'])
        self.assert_safe_view(facts)
        del outcome['draft_journal']
        facts = benny_ops_evidence.packet(outcome, repository=REPOSITORY)
        self.assertEqual(set(facts), {'R1'})
        outcome['draft_journal'] = ref(journal)
        outcome['repair']['url'] = 'https://github.com/other/repository/pull/27'
        with self.assertRaisesRegex(ValueError, 'native draft journal'):
            benny_ops_evidence.packet(outcome, repository=REPOSITORY)


class BennyCorrectedEvidenceTests(unittest.TestCase):
    def test_original_and_corrected_are_separate_audited_evidence_entries(self):
        correction = test_benny_correction.BennyCorrectionTests()
        self.addCleanup(correction.doCleanups)
        correction.setUp()
        fixture, root = correction.fixture, correction.root
        fixture.route = benny.freeze(rules=test_benny.RULES, slack=fixture.source.slack,
                                     channel=test_benny.CHANNEL, message_ts=test_benny.ROOT, out=root/'source')
        args = correction.ready()
        correction.add_correction(args)
        route = benny_correction.select(**args)
        runtime = benny_correction.options(args['archive'], 'clean-setup', correction.runtime)
        out = root/'correction/reproduction'
        fixture.reproduce(route=route['path'], sha256=route['sha256'], out=out, **runtime)
        outcome = {'status': 'reproduced', 'operations': operations(root, 'Reproduced'),
                   'corrected_route': route, 'original_reproduction': args['initial'], 'reproduction': ref(out/'result.json')}
        facts = benny_ops_evidence.packet(outcome, repository=REPOSITORY)
        self.assertTrue(facts['B1']['text'].startswith('Original setup.'))
        self.assertTrue(facts['R1']['text'].startswith('Corrected setup.'))
