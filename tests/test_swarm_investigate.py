import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest

from workbench import cli, swarm, swarm_investigate as model_swarm


GLM = {'provider': 'glm', 'model': 'glm-test'}
OPENAI = {'provider': 'openai', 'model': 'gpt-test'}


class ModelSwarmTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'source'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        for key, value in (('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid'),
                           ('commit.gpgsign', 'false')):
            cli.git(self.repo, 'config', key, value)
        (self.repo / 'left.ts').write_text('export const left = 1;\n')
        (self.repo / 'right.ts').write_text('export const right = 2;\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'Synthetic source')
        self.head = cli.sha(self.repo, 'HEAD')

    def lane(self, ident, engine=GLM, file='left.ts'):
        return {'id': ident, 'question': 'Describe the selected constant.', 'files': [file], 'engine': engine}

    def freeze(self, name, lanes, *, shape='coverage', rule=None, judge=None, parallelism=2):
        manifest = self.root / f'{name}.json'
        cli.write(manifest, {'schema': cli.SCHEMA, 'predicate': 'Cite committed source.',
                             'shape': shape, 'race_rule': rule, 'parallelism': parallelism,
                             'revision': self.head, 'seconds': 30, 'lanes': lanes, 'judge': judge,
                             'rubric': [{'id': 'scope', 'text': 'Covers the exact source without guessing.'}] if judge else []})
        return model_swarm.freeze(manifest, self.repo, self.root / f'approval-{name}')

    def answer(self, engine, prompt, *, quote=None, text='The exposed constant is declared in the file.'):
        evidence = json.loads(prompt.split('\n')[-1])['evidence']
        exact = evidence['S1']['text'].partition(': ')[2]
        return {'answer': {'status': 'ready', 'findings': [
            {'text': text, 'basis': 'observed', 'citations': [{'id': 'S1', 'line': 1, 'quote': quote or exact}]}],
            'unknowns': []}, 'observed_model': engine['model'] if engine['provider'] == 'glm' else None}

    def verify(self, approved, result):
        return model_swarm.audit(**approved, report=result['report'], report_sha256=result['sha256'])

    def test_parallel_readers_receive_only_their_approved_ranges(self):
        selected = [self.lane('left'), self.lane('right', OPENAI, 'right.ts')]
        approved = self.freeze('coverage', selected)
        active = Barrier(2, timeout=5)

        def invoke(engine, prompt, seconds):
            files = [item['path'] for item in json.loads(prompt.split('\n')[-1])['evidence'].values()]
            self.assertEqual(files, ['left.ts'] if engine == GLM else ['right.ts'])
            self.assertNotIn(str(self.repo), prompt)
            active.wait()
            return self.answer(engine, prompt)

        report = model_swarm.run(**approved, out=self.root / 'coverage', invoke=invoke)
        self.assertEqual(self.verify(approved, report)['status'], 'PASS')
        self.assertEqual({lane['revision'] for lane in report['lanes']}, {self.head})
        self.assertIsNone(report['lanes'][1]['observed_model'])

    def test_retries_only_invalid_model_evidence_and_audits_original_receipt(self):
        approved = self.freeze('retry', [self.lane('left')])
        responses = iter(('not a source line', None))

        def invoke(engine, prompt, seconds):
            return self.answer(engine, prompt, quote=next(responses))

        report = model_swarm.run(**approved, out=self.root / 'retry', invoke=invoke)
        lane = report['lanes'][0]
        self.assertEqual(len(lane['attempts']), 2)
        self.assertEqual(self.verify(approved, report)['status'], 'PASS')
        Path(lane['attempts'][1]['path']).write_text('{}')
        with self.assertRaisesRegex(swarm.SwarmError, 'digest changed'):
            self.verify(approved, report)

    def test_stops_same_provider_after_error_but_reports_the_other_lane(self):
        lanes = [self.lane('first'), self.lane('parked'), self.lane('third', OPENAI)]
        approved = self.freeze('outage', lanes, parallelism=1)
        calls = []

        def invoke(engine, prompt, seconds):
            calls.append(engine)
            if engine == GLM:
                raise RuntimeError('GLM returned HTTP 429 token [fixture]')
            return self.answer(engine, prompt)

        report = model_swarm.run(**approved, out=self.root / 'outage', invoke=invoke)
        self.assertEqual(calls, [GLM, OPENAI])
        self.assertEqual(report['gaps'], ['first', 'parked'])
        self.assertEqual(self.verify(approved, report)['status'], 'BLOCKED')
        receipts = [Path(row['attempts'][0]['path']).read_text() for row in report['lanes'][:2]]
        self.assertNotIn('[fixture]', receipts[0])

    def test_ranked_judge_receives_neutral_labels_and_audit_recreates_the_winner(self):
        lanes = [self.lane('alpha'), self.lane('beta', {'provider': 'glm', 'model': 'glm-other'})]
        approved = self.freeze('rank', lanes, shape='race', rule='rank-all', judge=OPENAI)

        def invoke(engine, prompt, seconds):
            if engine['provider'] == 'glm':
                return self.answer(engine, prompt, text='Precise answer.' if engine == GLM else 'Broad answer.')
            self.assertNotIn('glm-', prompt)
            self.assertNotIn('alpha', prompt)
            self.assertNotIn('beta', prompt)
            arms = json.loads(prompt.split('\n')[-1])['arms']
            scores = {row['id']: 4 if row['findings'][0]['text'] == 'Precise answer.' else 2 for row in arms}
            return {'answer': {'status': 'ready', 'candidates': [
                {'id': label, 'scores': [{'criterion': 'scope', 'score': score, 'why': 'Verified source.'}]}
                for label, score in scores.items()], 'recommendation': max(scores, key=scores.get)},
                'observed_model': None}

        report = model_swarm.run(**approved, out=self.root / 'ranked', invoke=invoke)
        self.assertEqual(report['winner'], 'alpha')
        self.assertEqual(self.verify(approved, report)['winner'], 'alpha')
        path = Path(report['report'])
        changed = cli.read(path)
        changed['winner'] = 'beta'
        cli.write(path, changed)
        report['sha256'] = cli.digest(path)
        with self.assertRaisesRegex(model_swarm.ModelSwarmError, 'combined verdict'):
            self.verify(approved, report)

    def test_race_rejects_different_inputs_and_same_model_judge(self):
        with self.assertRaisesRegex(model_swarm.ModelSwarmError, 'same question'):
            self.freeze('different', [self.lane('left'), self.lane('right', file='right.ts')],
                        shape='race', rule='first-pass')
        with self.assertRaisesRegex(model_swarm.ModelSwarmError, 'outside the race'):
            self.freeze('judge', [self.lane('first'), self.lane('second')], shape='race', rule='best-of', judge=GLM)


if __name__ == '__main__':
    unittest.main()
