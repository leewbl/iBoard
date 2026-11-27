import json
from pathlib import Path
import tempfile
import unittest

from workbench import arena, cli, model_profiles, swarm_investigate


GLM = {'provider': 'glm', 'model': 'glm-profile'}
OPENAI = {'provider': 'openai', 'model': 'gpt-profile'}


class ProfilePanelTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.repo = self.root/'source'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q', '-b', 'main')
        for key, value in [('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid'),
                           ('commit.gpgsign', 'false')]:
            cli.git(self.repo, 'config', key, value)
        (self.repo/'app.js').write_text("exports.value = 'old';\n")
        (self.repo/'check.js').write_text('// frozen test\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'Baseline with fixed observation')
        self.head = cli.sha(self.repo, 'HEAD')
        self.rubric = [{'id': key, 'text': key} for key in ('quality', 'behavior', 'scope')]
        self.calls = []

    def profile(self, parent, runners):
        def editor(work, prompt, timeout, **kwargs):
            return self.glm_reply(kwargs, {'available': True})

        spec = self.root/'probe.json'
        cli.write(spec, {'schema': 1, 'seconds': 30, 'engines': [
            {**GLM, 'efforts': ['low', 'medium']}, {**OPENAI, 'efforts': ['low', 'medium']}]})
        inventory = model_profiles.probe(spec, self.root/'inventory.json', editor=editor,
                                         reviewer=lambda *args, **kwargs: {'available': True})
        roles = {name: GLM for name in model_profiles.TASK_ROLES}
        other = OPENAI if parent == GLM else GLM
        roles.update({'arena-runners': runners, 'arena-cross-judge-pool': ['auto', parent, other],
                      'swarm-workers': GLM})
        choices = self.root/'choices.json'
        cli.write(choices, {'schema': 1, 'budget': 'small', 'parent': parent, 'roles': roles})
        selection = model_profiles.draft(inventory['path'], inventory['sha256'], choices, self.root/'profile.json')
        return {'path': selection['path'], 'sha256': selection['sha256']}

    @staticmethod
    def glm_reply(kwargs, answer):
        return {'exit_code': 0, 'timed_out': False, 'trace_completed': True,
                'model': kwargs['requested_model'], 'requested_effort': kwargs['requested_effort'],
                'stdout': json.dumps(answer)}

    def editor(self, work, prompt, timeout, **kwargs):
        self.calls.append(('glm', kwargs['requested_effort'], kwargs.get('read_only', False)))
        self.assertTrue(kwargs['private_workspace'])
        if kwargs.get('read_only'):
            answer = ({'verdict': 'PASS', 'reason': 'Fixed source and recorded sandbox agree.'}
                      if prompt.startswith('Independently verify') else self.score())
        else:
            name = work.parent.name
            (work/'app.js').write_text(f"exports.value = 'new'; // {name}\n")
            answer = {'status': 'ready', 'rationale': 'Apply the exact scoped value.'}
        return self.glm_reply(kwargs, answer)

    def score(self):
        return {'candidates': [{'id': label, 'scores': [
                    {'criterion': item['id'], 'score': score, 'why': 'Verified observed behavior.'}
                    for item in self.rubric]} for label, score in [('A1', 4), ('A2', 3)]],
                'recommendation': 'A1'}

    def reviewer(self, prompt, model, timeout, *, effort):
        self.calls.append(('openai', effort, True))
        self.assertEqual(model, OPENAI['model'])
        return self.score()

    @staticmethod
    def sandbox(work, image, command, timeout, **kwargs):
        passed = "'new'" in (work/'app.js').read_text()
        return {'stdout': 'NORMALIZE_PASS' if passed else '', 'container_created': True,
                'cleanup_confirmed': True, 'timed_out': False, 'exit_code': 0 if passed else 1}

    def test_arena_uses_cross_vendor_glm_when_the_parent_is_openai(self):
        reference = self.profile(OPENAI, [GLM, GLM])
        manifest = self.root/'arena.json'
        config = {'repo': str(self.repo), 'request': 'Set the exported value to new.', 'files': ['app.js'],
                  'model_profile': reference, 'image': 'fixture', 'revision': self.head,
                  'check': {'command': ['node', '/workspace/check.js'], 'marker': 'NORMALIZE_PASS', 'exit_code': 0},
                  'rubric': self.rubric,
                  'design_alternatives': {'A': {'modules': ['inline'], 'state': ['value'], 'interfaces': ['public']},
                                          'B': {'modules': ['helper'], 'state': ['value'], 'interfaces': ['public']}}}
        cli.write(manifest, config)
        dest = self.root/'arena-result'
        def editor(work, prompt, timeout, **kwargs):
            if prompt.startswith('Reformat the earlier report'):
                self.assertTrue(kwargs['read_only'])
                return self.glm_reply(kwargs, {'status': 'ready', 'rationale': 'Preserve the scoped edit.'})
            answer = self.editor(work, prompt, timeout, **kwargs)
            if work.parent.name == 'A1' and not kwargs.get('read_only'):
                answer['stdout'] = 'The scoped edit is complete.'
            return answer

        result = arena.run(manifest, dest, editor=editor, judge=self.reviewer, sandbox=self.sandbox)
        self.assertEqual(result['status'], 'verified', result)
        self.assertEqual(result['scorecards']['glm']['model'], GLM['model'])
        self.assertIn(('openai', 'medium', True), self.calls)
        self.assertEqual(result['model_profile_sha256'], reference['sha256'])
        settings = arena._settings(manifest)
        arena.verify_model_calls(dest, settings, result)
        calls = [cli.read(Path(row['path'])) for row in result['model_calls']]
        self.assertEqual([item['access'] for item in calls if item['purpose'] == 'edit-A1'], ['edit', 'read-only'])
        path = Path(result['model_calls'][0]['path'])
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(arena.ArenaError, 'receipt changed'):
            arena.verify_model_calls(dest, settings, result)

    def swarm_manifest(self, path, profile, *, first=None, second='profile'):
        lane = {'question': 'What value is exported?', 'files': ['app.js:1-1']}
        workers = [{'id': 'first', **lane}, {'id': 'second', **lane, 'engine': second}]
        if first is not None:
            workers[0]['engine'] = first
        cli.write(path, {'schema': 1, 'shape': 'race', 'race_rule': 'rank-all', 'revision': self.head,
                         'predicate': 'Quote the code exactly.', 'seconds': 45, 'parallelism': 2,
                         'model_profile': profile, 'lanes': workers, 'judge': 'profile',
                         'rubric': [{'id': 'scope', 'text': 'Cover the exact source.'}]})

    def test_swarm_freezes_profile_and_audits_effort_and_cross_vendor_ranking(self):
        reference = self.profile(GLM, [GLM, OPENAI])
        manifest = self.root/'swarm.json'
        self.swarm_manifest(manifest, reference)
        approval = swarm_investigate.freeze(manifest, self.repo, self.root/'swarm-plan')
        Path(reference['path']).unlink()
        def invoke(engine, prompt, seconds):
            self.assertEqual(engine['effort'], 'low')
            if engine['provider'] == 'openai':
                answer = {'status': 'ready', 'recommendation': 'arm-1', 'candidates': [
                    {'id': label, 'scores': [{'criterion': 'scope', 'score': score, 'why': 'Exact citation.'}]}
                    for label, score in [('arm-1', 4), ('arm-2', 3)]]}
            else:
                answer = {'status': 'ready', 'findings': [
                    {'text': 'The exported value is old.', 'basis': 'observed',
                     'citations': [{'id': 'S1', 'line': 1, 'quote': "exports.value = 'old';"}]}], 'unknowns': []}
            return {'answer': answer, 'observed_model': GLM['model'] if engine['provider'] == 'glm' else None,
                    'requested_effort': engine['effort']}

        result = swarm_investigate.run(**approval, out=self.root/'swarm-result', invoke=invoke)
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['model_profile_sha256'], reference['sha256'])
        self.assertTrue(all(row['requested_effort'] == 'low' for row in result['lanes']))
        audited = swarm_investigate.audit(**approval, report=result['report'], report_sha256=result['sha256'])
        self.assertEqual(audited['winner'], result['winner'])

    def test_swarm_rejects_an_arm_outside_the_budget_and_a_judge_within_the_race(self):
        reference = self.profile(GLM, [GLM, OPENAI])
        manifest = self.root/'swarm.json'
        self.swarm_manifest(manifest, reference, first={**GLM, 'effort': 'medium'})
        with self.assertRaisesRegex(ValueError, 'within the approved budget'):
            swarm_investigate.freeze(manifest, self.repo, self.root/'swarm-plan')
        self.swarm_manifest(manifest, reference, second=OPENAI)
        with self.assertRaisesRegex(ValueError, 'outside the race'):
            swarm_investigate.freeze(manifest, self.repo, self.root/'swarm-plan')


if __name__ == '__main__':
    unittest.main()
