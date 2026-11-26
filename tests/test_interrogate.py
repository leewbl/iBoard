import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from workbench import cli, interrogate


class InterrogateTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)
        self.repo = self.directory / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        cli.git(self.repo, 'config', 'user.name', 'Interrogate Fixture')
        self.entry = self.repo / 'check.mjs'
        self.entry.write_text("import state from './state.mjs';\n"
                              "if (state === 'active') console.log('ACTIVE_MARKER');\n", encoding='utf-8')
        (self.repo / 'state.mjs').write_text("export default 'pending';\n", encoding='utf-8')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'initial fixture')
        self.baseline = cli.sha(self.repo, 'HEAD')
        (self.repo / 'state.mjs').write_text("export default 'active';\n", encoding='utf-8')
        cli.git(self.repo, 'commit', '-qam', 'activate state')
        self.candidate = cli.sha(self.repo, 'HEAD')
        self.intent = 'Activate the state on initialization.\n'
        self.digest = hashlib.sha256(self.intent.encode()).hexdigest()
        self.image = 'sha256:' + 'a' * 64
        self.out = self.directory / 'interrogate'
        self.config = {'image': 'local-verified:node', 'review_model': 'gpt-fixture', 'probes': [
            {'id': 'init', 'command': ['node', '/workspace/check.mjs'], 'exit_code': 0,
             'marker': 'ACTIVE_MARKER', 'intent': 'Confirm initialization transitions to active.'}]}
        self.calls = []
        self.matchers = ['ACTIVE_MARKER', 'ACTIVE_MARKER']
        self.verdict_glm = self.verdict_openai = 'clear'

    def _sandbox(self, workspace, image, command, timeout, **settings):
        self.assertEqual(cli.sha(workspace, 'HEAD'), self.candidate)
        self.assertFalse(cli.git(workspace, 'status', '--porcelain'))
        stdout = self.matchers[len(self.calls)]
        self.calls.append(workspace)
        return {'image': image, 'command': command, 'workspace': str(workspace), 'exit_code': 0,
                'container_created': True, 'create_exit_code': 0, 'cleanup_confirmed': True,
                'timed_out': False, 'stdout_truncated': False, 'stderr_truncated': False,
                'stdout': stdout, 'stderr': '', **settings}

    def _answer(self, prompt, verdict):
        self.assertIn('ACTIVE_MARKER', prompt)
        self.assertIn('Activate the state', prompt)
        self.assertIn('init', prompt)
        return {'summary': 'I independently assessed both candidate observations.', 'verdict': verdict,
                'checks': [{'id': 'init', 'verdict': verdict, 'reason': 'The probe tracks the active state.',
                            'observations': ['a', 'b']}]}

    def _run(self, **options):
        params = {'repo': self.repo, 'baseline': self.baseline, 'candidate': self.candidate,
                  'request_sha256': self.digest, 'intent': self.intent, 'config': self.config, 'out': self.out,
                  'resolve_image': lambda name, remaining: self.image, 'sandbox': self._sandbox,
                  'glm': lambda prompt, timeout: json.dumps(self._answer(prompt, self.verdict_glm)),
                  'openai': lambda prompt, model, timeout: self._answer(prompt, self.verdict_openai)}
        return interrogate.run(**{**params, **options})

    def _validate(self, report=None, **overrides):
        path = self.out / 'result.json'
        fields = {'repo': self.repo, 'baseline': self.baseline, 'candidate': self.candidate,
                  'request_sha256': self.digest, 'config': self.config}
        receipt = json.loads(path.read_text(encoding='utf-8')) if report is None else report
        return interrogate.validate_receipt(path, receipt, **{**fields, **overrides})

    def test_checks_candidate_in_two_detached_worktrees_and_replays_receipt(self):
        root_ref = cli.git(self.repo, 'symbolic-ref', 'HEAD')
        result = self._run(seconds_limit=30)
        self.assertEqual(result['status'], 'clear', result.get('reason'))
        self.assertEqual(result['seconds_used_limit'], 30)
        self.assertEqual(len(self.calls), len(set(self.calls)))
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(any(workspace.exists() for workspace in self.calls))
        self.assertEqual(cli.git(self.repo, 'symbolic-ref', 'HEAD'), root_ref)
        self.assertFalse(cli.git(self.repo, 'status', '--porcelain'))
        self.assertEqual(self._validate()['status'], 'clear')
        with self.assertRaisesRegex(interrogate.InterrogateError, 'different candidate'):
            self._validate(candidate=self.baseline)
        with self.assertRaisesRegex(interrogate.InterrogateError, 'configuration changed'):
            self._validate(config={**self.config, 'memory_mb': 1024})
        raw = self.out / 'init-b.json'
        raw.write_bytes(raw.read_bytes() + b' ')
        with self.assertRaisesRegex(interrogate.InterrogateError, 'evidence digest'):
            self._validate()

    def test_repeatable_mismatch_blocks_even_with_favorable_review(self):
        self.matchers = ['pending state', 'pending state']
        result = self._run()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['findings'], ['init'])
        self.assertEqual([row['outcome'] for row in result['probes']['init'].values()], ['mismatch', 'mismatch'])
        self.assertEqual(result['worktree_cleanup'], {'a': True, 'b': True})
        with self.assertRaisesRegex(interrogate.InterrogateError, 'did not clear'):
            self._validate()

    def test_nonrepeatable_failure_is_not_attributed(self):
        self.matchers[1] = 'pending state'
        result = self._run()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['findings'], [])
        self.assertEqual([row['outcome'] for row in result['probes']['init'].values()], ['matched', 'mismatch'])

    def test_unresolved_review_prevents_publication(self):
        self.verdict_openai = 'blocked'
        result = self._run()
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('withheld approval', result['reason'])
        self.assertEqual(set(result['reviews']), {'openai', 'glm'})

    def test_glm_format_gets_one_corrective_retry_with_original_response_retained(self):
        prompts = []

        def glm(prompt, timeout):
            prompts.append(prompt)
            payload = json.dumps(self._answer(prompt, 'clear'))
            return payload + '\nTrailing note.' if len(prompts) == 1 else f'```json\n{payload}\n```'

        result = self._run(glm=glm)
        self.assertEqual(result['status'], 'clear', result.get('reason'))
        self.assertEqual(len(prompts), 2)
        self.assertIn('The previous reply was not one valid JSON object.', prompts[1])
        attempts = json.loads((self.out / 'review-glm.json').read_text())['attempts']
        self.assertIn('Trailing note.', attempts[0]['answer'])
        self.assertEqual(self._validate()['status'], 'clear')

    def test_glm_quota_error_does_not_retry(self):
        count = 0

        def glm(prompt, timeout):
            nonlocal count
            count += 1
            raise RuntimeError('GLM returned HTTP 429')

        result = self._run(glm=glm)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('GLM returned HTTP 429', result['reason'])
        self.assertEqual(count, 1)

    def test_changed_probe_or_request_cannot_execute(self):
        with self.assertRaisesRegex(interrogate.InterrogateError, 'request digest changed'):
            self._run(intent='a different requirement')
        self.entry.write_text("console.log('patently unrelated');\n", encoding='utf-8')
        cli.git(self.repo, 'commit', '-qam', 'change verification entrypoint')
        with self.assertRaisesRegex(interrogate.InterrogateError, 'entrypoint is missing or changed'):
            self._run(candidate=cli.sha(self.repo, 'HEAD'))
        self.assertFalse(self.calls)


if __name__ == '__main__':
    unittest.main()
