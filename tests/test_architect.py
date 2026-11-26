import json
from pathlib import Path
import tempfile
import unittest

from workbench import architect, cli


class ArchitectTests(unittest.TestCase):
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
        cli.git(self.repo, 'add', 'driver.py')
        cli.git(self.repo, 'commit', '-qm', 'pin baseline')
        self.base = cli.sha(self.repo, 'HEAD')
        self.source.write_text('def run(data):\n    return data + 1\n')
        cli.git(self.repo, 'commit', '-qam', 'candidate')
        self.candidate = cli.sha(self.repo, 'HEAD')
        self.risks = []
        self.compare_calls = 0
        self.contested = False
        self.inspection_lines = None
        self.invalid_state = False
        self.claim_runtime = False
        self.model = 'gpt-fixture'

    def shape(self, label):
        states = [{'name': 'decision', 'owner': label}]
        if self.invalid_state and label == 'Coordinator':
            states.append({'name': 'decision', 'owner': 'Runtime'})
        return {'title': label, 'summary': 'Add one bounded operation.',
                'modules': [{'name': label, 'duty': 'Bind outcome to input.'},
                            {'name': 'Runtime', 'duty': 'Perform the operation.'}],
                'state': states,
                'interfaces': [{'signature': 'run(data: int) -> int', 'owner': label, 'caller': 'CLI'}],
                'tradeoffs': ['One tradeoff is visible.'], 'risks': ['Execution still needs evidence.'],
                'verification': [{'kind': 'static', 'expectation': 'Check that the new body changes the value.'},
                                 {'kind': 'runtime', 'expectation': 'Run the real CLI against the frozen input.'}],
                'findings': [{'text': 'The current wrapper returns the input.', 'basis': 'observed',
                              'citations': [{'id': 'S1', 'line': 2}]}]}

    def glm(self, prompt, timeout):
        self.assertIn('2:     return data', prompt)
        return '```json\n' + json.dumps(self.shape('Coordinator')) + '\n```'

    def openai(self, prompt, model, timeout):
        self.assertEqual(model, self.model)
        payload = prompt.split('\nThe prior response was rejected.', 1)[0].split(
            '\nThe previous answer was rejected.', 1)[0]
        packet = json.loads(payload.rsplit('\n', 1)[-1])
        if prompt.startswith('Design a possible'):
            self.assertIn('committed_source', packet)
            return self.shape('Repository')
        if prompt.startswith('Independently select'):
            self.compare_calls += 1
            self.assertEqual(set(packet['alternatives']), {'A', 'B'})
            self.assertNotIn('model_calls', packet)
            self.assertFalse(any('role' in proposal for proposal in packet['alternatives'].values()))
            winner = next(key for key, item in packet['alternatives'].items() if item['title'] == 'Coordinator')
            return {'decision': 'contested' if self.contested else winner,
                    'reasons': {'A': 'A scoped mechanism.', 'B': 'A different scoped mechanism.'},
                    'tradeoff': 'Select the interface with less coupling.', 'risks': []}
        index = next(int(line.split(':')[0]) for line in packet['diff'].splitlines() if ': +    return data + 1' in line)
        checks = [{'id': 'C1', 'status': 'supported', 'reason': 'The return changes.',
                   'diff_lines': self.inspection_lines if self.inspection_lines is not None else [index]}]
        if self.claim_runtime:
            checks.append({'id': 'C2', 'status': 'supported', 'reason': 'Unsupported runtime claim.',
                           'diff_lines': [index]})
        return {'summary': 'A static change is in place.', 'checks': checks}

    def design(self):
        return architect.plan(repo=self.repo, revision=self.base, question='Design this wrapper.',
                              files=['driver.py:1-2'], scope=['driver.py'], model=self.model,
                              out=self.root / 'plan', glm=self.glm, openai=self.openai)

    def review(self, revision=None, out='review', digest=None, reviewer=None):
        design = self.root / 'plan' / 'result.json'
        return architect.review(repo=self.repo, revision=revision or self.candidate, design=design,
                                design_sha256=digest or cli.digest(design), model=self.model,
                                out=self.root / out, openai=reviewer or self.openai)

    def test_plan_compares_grounded_options_and_review_preserves_unexecuted_obligations(self):
        result = self.design()
        self.assertEqual(result['status'], 'selected')
        self.assertEqual(result['selected']['title'], 'Coordinator')
        self.assertEqual(self.compare_calls, 1)
        self.assertEqual(set(result['alternatives']), {'A', 'B'})
        self.assertTrue((self.root / 'plan' / 'outline.md').is_file())
        checked = self.review()
        self.assertEqual(checked['status'], 'static-conforms')
        self.assertEqual(checked['runtime_pending'], ['C2'])
        self.assertEqual(checked['changed'], ['driver.py'])
        self.assertEqual(checked['candidate'], self.candidate)
        self.assertEqual(cli.git(self.repo, 'status', '--porcelain'), '')

    def test_invalid_state_ownership_blocks_design_selection(self):
        self.invalid_state = True
        result = self.design()
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('single definition', result['reason'])
        self.assertEqual(self.compare_calls, 0)

    def test_contested_choice_keeps_both_options_and_no_selected_outline(self):
        self.contested = True
        result = self.design()
        self.assertEqual(result['status'], 'contested')
        self.assertEqual(len(result['alternatives']), 2)
        self.assertFalse((self.root / 'plan' / 'outline.md').exists())

    def test_scope_drift_and_unapproved_digest_both_block_approval(self):
        self.design()
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.review(digest='a' * 64)
        (self.repo / 'unrelated.py').write_text('other = 1\n')
        cli.git(self.repo, 'add', 'unrelated.py')
        cli.git(self.repo, 'commit', '-qm', 'out of scope')
        checked = self.review(revision=cli.sha(self.repo, 'HEAD'),
                              reviewer=lambda *args: self.fail('reviewer should not see out-of-scope source'))
        self.assertEqual(checked['status'], 'revise')
        self.assertEqual(checked['outside_scope'], ['unrelated.py'])

    def test_diff_header_does_not_prove_implementation(self):
        self.design()
        self.inspection_lines = [1]
        checked = self.review()
        self.assertEqual(checked['status'], 'blocked')
        self.assertIn('added or removed content line', checked['reason'])

    def test_static_review_rejects_a_claim_of_runtime_success(self):
        self.design()
        self.claim_runtime = True
        checked = self.review()
        self.assertEqual(checked['status'], 'blocked')
        self.assertIn('unknown or duplicate identity', checked['reason'])
