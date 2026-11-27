import json
from pathlib import Path
import unittest

import test_interrogate
from workbench import blast_radius, cli, interrogate


class BlastRadiusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_interrogate.InterrogateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.directory
        self.repo = self.fixture.repo
        self.baseline = self.fixture.baseline
        self.candidate = self.fixture.candidate
        self.intent = self.root / 'issue.txt'
        self.intent.write_text(self.fixture.intent, encoding='utf-8')
        self.source = ['check.mjs:2', 'state.mjs:1']
        self.probes = self.root / 'probes.json'
        self.assessments = {
            'summary': 'Activation changes the observed client state.',
            'facts': [
                {'surface': 'Client read', 'invariant': 'Client still recognizes active.',
                 'risk': 'Changing the literal would leave the client pending.', 'likelihood': 'medium',
                 'impact': 'high', 'source': 'S1', 'line': 2},
                {'surface': 'Stored representation', 'invariant': 'Old data can be interpreted.',
                 'risk': 'A different persisted literal might no longer deserialize.', 'likelihood': 'unknown',
                 'impact': 'medium', 'source': 'S2', 'line': 1},
            ],
        }

    def analyze(self):
        return blast_radius.analyze(repo=self.repo, baseline=self.baseline, candidate=self.candidate,
                                    intent_file=self.intent, source=self.source, model='gpt-fixture',
                                    out=self.root / 'analysis.json', reviewer=lambda *_: self.assessments)

    def run_check(self, *, check=True, sandbox=None):
        analysis = self.analyze()
        config = {'checks': {'fact-1': 'init'} if check else {}, 'required': ['fact-1'],
                  'interrogate': self.fixture.config if check else None}
        self.probes.write_text(json.dumps(config), encoding='utf-8')

        def review(prompt):
            self.assertIn('Verified candidate diff excerpt', prompt)
            self.assertIn('"source_file_changed": false', prompt.replace('\\"', '"'))
            return self.fixture._answer(prompt, 'clear')

        def engine(**kwargs):
            return interrogate.run(**kwargs, resolve_image=lambda *_: self.fixture.image,
                                   sandbox=sandbox or self.fixture._sandbox,
                                   glm=lambda prompt, remaining: json.dumps(review(prompt)),
                                   openai=lambda prompt, model, remaining: review(prompt))

        return blast_radius.run(repo=self.repo, analysis=analysis['path'], sha256=analysis['sha256'],
                               probes=self.probes, out=self.root / 'blast', runner=engine)

    def attach(self, report):
        task = self.root / 'task.json'
        cli.write(task, {'schema': cli.SCHEMA, 'id': 'impact', 'base_commit': self.baseline, 'repo': str(self.repo),
                         'kind': 'feature', 'criteria': [], 'criterion_changes': [], 'evidence': [],
                         'implementer': 'editor', 'surface': 'non-ui'})
        blast_radius.attach(task, report['path'], report['sha256'])
        return cli.read(task)

    def test_independent_runs_clear_one_unchanged_downstream_caller(self):
        result = self.run_check()
        self.assertEqual(result['status'], 'clear', result)
        self.assertEqual(result['facts']['fact-1']['status'], 'proven')
        self.assertTrue(result['facts']['fact-1']['outside_diff'])
        self.assertEqual(result['facts']['fact-2']['status'], 'unproven')
        data = self.attach(result)
        self.assertEqual(blast_radius.require(data, self.candidate)['status'], 'clear')
        self.assertFalse(any('blast-radius evidence:' in term for term in cli.evidence_gate(data, self.candidate)[1]))
        raw = self.root / 'blast' / 'interrogate' / 'init-b.json'
        raw.write_bytes(raw.read_bytes() + b' ')
        with self.assertRaisesRegex(cli.WorkbenchError, 'facts are unproven or the evidence changed'):
            blast_radius.require(data, self.candidate)
        self.assertTrue(any('blast-radius evidence:' in term for term in cli.evidence_gate(data, self.candidate)[1]))

    def test_repeatable_real_mismatch_is_reported_and_cannot_be_attached(self):
        self.fixture.matchers = ['pending', 'pending']
        result = self.run_check()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['facts']['fact-1']['status'], 'regression-observed')
        with self.assertRaisesRegex(cli.WorkbenchError, 'unproven or the evidence changed'):
            self.attach(result)

    def test_required_but_unchecked_risk_is_unproven(self):
        result = self.run_check(check=False)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['facts']['fact-1']['proof_level'], 'source-anchor')
        self.assertEqual(self.fixture.calls, [])

    def test_model_cannot_cite_a_line_outside_selected_source(self):
        self.assessments['facts'][0]['line'] = 1
        with self.assertRaisesRegex(ValueError, 'captured downstream source line'):
            self.analyze()

    def test_two_receipts_from_one_container_leave_the_risk_unproven(self):
        def duplicate(*args, **kwargs):
            return {**self.fixture._sandbox(*args, **kwargs), 'create_stdout': 'f' * 64}

        result = self.run_check(sandbox=duplicate)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['facts']['fact-1']['status'], 'unproven')


if __name__ == '__main__':
    unittest.main()
