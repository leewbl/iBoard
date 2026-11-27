import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from workbench import cli, interrogate, interrogate_discovery, model_profiles


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
        identity = hashlib.sha256(f'{len(self.calls)}'.encode()).hexdigest()
        return {'image': image, 'command': command, 'workspace': str(workspace), 'exit_code': 0,
                'container_created': True, 'create_exit_code': 0, 'cleanup_confirmed': True,
                'timed_out': False, 'stdout_truncated': False, 'stderr_truncated': False,
                'stdout': stdout, 'stderr': '', 'create_stdout': identity,
                'stdout_sha256': hashlib.sha256(stdout.encode()).hexdigest(),
                'stderr_sha256': hashlib.sha256(b'').hexdigest(), **settings}

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

    def _profile(self):
        provider = {'provider': 'glm', 'model': 'glm-interrogate'}
        external = {'provider': 'openai', 'model': 'gpt-fixture'}
        path = self.directory / 'probe.json'
        cli.write(path, {'schema': 1, 'seconds': 30, 'engines': [{**provider, 'efforts': ['low']},
                                                               {**external, 'efforts': ['low']}]})
        inventory = model_profiles.probe(path, self.directory/'inventory.json',
            editor=lambda work, prompt, seconds, **kwargs: {
                'exit_code': 0, 'trace_completed': True, 'model': provider['model'],
                'requested_effort': kwargs['requested_effort'], 'stdout': '{"available":true}'},
            reviewer=lambda *args, **kwargs: {'available': True})
        path = self.directory / 'choices.json'
        cli.write(path, {'schema': 1, 'budget': 'small', 'parent': provider,
                        'roles': {'interrogate-reviewers': ['inherit-parent', external]}})
        installed = model_profiles.draft(inventory['path'], inventory['sha256'], path, self.directory/'profile.json')
        self.config.pop('review_model')
        self.config['model_profile'] = {'path': installed['path'], 'sha256': installed['sha256']}

    @staticmethod
    def _profile_reply(route, answer):
        return {'answer': answer, 'requested_effort': route['effort'],
                'observed_model': route['model'] if route['provider'] == 'glm' else None}

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

    def test_profiled_panel_observations_and_receipts_are_independent(self):
        self._profile()
        result = self._run(profile_invoke=lambda route, prompt, _: self._profile_reply(
            route, self._answer(prompt, 'clear')))
        self.assertEqual(result['status'], 'clear', result.get('reason'))
        self.assertEqual(set(result['reviews']), {'reviewer-1', 'reviewer-2'})
        Path(self.config['model_profile']['path']).unlink()
        self.assertEqual(self._validate()['status'], 'clear')
        path = self.out/'review-reviewer-1.json'
        proof = cli.read(path)
        proof['attempts'][0]['requested_effort'] = 'xhigh'
        cli.write(path, proof)
        result['reviews']['reviewer-1']['sha256'] = cli.digest(path)
        with self.assertRaisesRegex(interrogate.InterrogateError, 'approved model or prompt'):
            self._validate(result)

    def test_profile_rejects_reused_docker_container_and_unstable_mismatches(self):
        self._profile()
        def invoke(route, prompt, seconds):
            return self._profile_reply(route, self._answer(prompt, 'clear'))

        def reused(*args, **kwargs):
            return {**self._sandbox(*args, **kwargs), 'create_stdout': 'a' * 64}

        result = self._run(profile_invoke=invoke, sandbox=reused)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['probes']['init']['b']['outcome'], 'inconclusive')
        self.matchers = ['First mismatch.', 'Different mismatch.']
        self.calls.clear()
        result = self._run(profile_invoke=invoke, out=self.directory/'unstable')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['findings'], [])

    def test_discovery_keeps_required_probes_and_freezes_the_selected_catalog(self):
        self._profile()
        extra = {**self.config['probes'][0], 'id': 'compatibility'}
        self.config['probes'].append(extra)
        path = self.directory/'catalog.json'
        cli.write(path, {'schema': 1, **self.config})

        def invoke(route, prompt, seconds):
            self.assertNotIn('/workspace/check.mjs', prompt)
            answer={'cases': [{'id': 'compatibility', 'line': 'L2',
                               'reason': 'Initialization should be observable by existing callers.'}]}
            return self._profile_reply(route, answer)

        result = interrogate_discovery.propose(repo=self.repo, baseline=self.baseline, candidate=self.candidate,
            intent=self.intent, catalog=path, scope=['state.mjs'], out=self.directory/'discover',
            required=['init'], invoke=invoke)
        self.assertEqual(result['status'], 'ready', result.get('reason'))
        self.assertEqual(result['selected'], ['init', 'compatibility'])
        self.assertEqual(self.calls, [])
        Path(self.config['model_profile']['path']).unlink()
        pinned, selected = interrogate_discovery.load(result['plan'], result['sha256'])
        self.assertEqual([item['id'] for item in selected['probes']], ['init', 'compatibility'])
        self.assertEqual(pinned['drivers'].keys(), {'init', 'compatibility'})
        file = self.directory/'discover/proposal-reviewer-1.json'
        file.write_bytes(file.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError, 'evidence digest'):
            interrogate_discovery.load(result['plan'], result['sha256'])

    def test_discovery_does_not_accept_model_authored_commands(self):
        self._profile()
        catalog = self.directory/'catalog.json'
        cli.write(catalog, {'schema': 1, **self.config})
        result = interrogate_discovery.propose(repo=self.repo, baseline=self.baseline, candidate=self.candidate,
            intent=self.intent, catalog=catalog, scope=['state.mjs'], out=self.directory/'discover',
            invoke=lambda route, prompt, seconds: self._profile_reply(route,
                {'cases': [{'id': 'init', 'line': 'L2', 'reason': 'Existing behavior.',
                            'command': ['node', 'unapproved.js']}]}))
        self.assertEqual(result['status'], 'inconclusive')
        with self.assertRaisesRegex(ValueError, 'completed functional discovery'):
            interrogate_discovery.execute(result['plan'], result['sha256'], self.out)
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
