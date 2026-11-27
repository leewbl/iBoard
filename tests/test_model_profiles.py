import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import autopilot, cli, model_profiles, task_replay


GLM = {'provider': 'glm', 'model': 'glm-profile'}
OPENAI = {'provider': 'openai', 'model': 'gpt-review'}


class ModelProfileTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.calls = []
        self.probes = self.root / 'probes.json'
        cli.write(self.probes, {'schema': 1, 'seconds': 75, 'engines': [
            {**GLM, 'efforts': ['low', 'medium']}, {**OPENAI, 'efforts': ['low']}]})
        self.choices = {'schema': 1, 'budget': 'small', 'parent': GLM, 'roles': {
            'planning': 'auto', 'arena-runners': ['inherit-parent', OPENAI],
            'arena-cross-judge-pool': [GLM, 'auto', OPENAI]}}

    def editor(self, work, prompt, timeout, **options):
        self.calls.append(('glm', options['requested_model'], options['requested_effort']))
        return {'exit_code': 0, 'timed_out': False, 'stdout': '{"available":true}', 'trace_completed': True,
                'model': options['requested_model']+'[1M]', 'requested_effort': options['requested_effort'],
                'provider': 'zhipu'}

    def reviewer(self, prompt, model, timeout, *, effort):
        self.calls.append(('openai', model, effort))
        return {'available': True}

    def inventory(self, editor=None):
        return model_profiles.probe(self.probes, self.root/'inventory.json',
                                    editor=editor or self.editor, reviewer=self.reviewer)

    def draft(self):
        value = self.inventory()
        cli.write(self.root/'choices.json', self.choices)
        result = model_profiles.draft(value['path'], value['sha256'], self.root/'choices.json', self.root/'draft.json')
        return value, result

    def bound_task(self, draft):
        task = self.root/'task'/'task.json'
        task.parent.mkdir()
        record = {'schema': 1, 'kind': 'bug', 'repo': str(self.root), 'status': 'running',
                  'limits': {'seconds': 120, 'iterations': 3}, 'iterations': 0, 'started_at': cli.now()}
        reference = model_profiles.snapshot(task, record, model_profiles.load(draft['path'], draft['sha256']))
        cli.write(task, record)
        cli.write(task.with_name('input-stage.json'), {'schema': 1, 'model_profile': reference})
        return task, reference

    def test_probes_and_budget_route_aliases_and_cross_vendor_judge(self):
        inventory, result = self.draft()
        self.assertEqual(len(inventory['probes']), 3)
        model = model_profiles.load(result['path'], result['sha256'])
        self.assertEqual(model['routes']['planning']['selection'], 'auto')
        self.assertEqual(model['routes']['planning']['effort'], 'medium')
        self.assertEqual(model['routes']['baseline-review']['effort'], 'low')
        self.assertEqual(model_profiles.select(model, 'arena-cross-judge-pool', other_than='glm')['provider'], 'openai')
        self.assertEqual(model_profiles.for_stage(model, 'ci-repair-2', 'bug')['role'], 'repair')
        self.assertEqual(model_profiles.for_stage(model, 'premise-2', 'bug')['role'], 'baseline-review')

    def test_provider_mismatch_closes_only_its_remaining_probes(self):
        def wrong(*args, **kwargs):
            return {**self.editor(*args, **kwargs), 'model': 'glm-wrong'}

        result = self.inventory(wrong)
        self.assertEqual([r['status'] for r in result['probes']], ['unverified', 'unverified', 'confirmed'])
        self.assertEqual([key for key, _, _ in self.calls], ['glm', 'openai'])

    def test_context_window_suffix_preserves_exact_model_identity(self):
        route = {**GLM, 'effort': 'low'}
        for suffix in ('', '[1M]', '[1m]'):
            record = self.editor(None, '', 30, requested_model=route['model'], requested_effort=route['effort'])
            record['model'] = route['model'] + suffix
            self.assertEqual(model_profiles.confirm_glm(route, record), record['model'])
        for model in ('glm-other[1m]', 'glm-profile[128k]', 'glm-profile[1m][1M]'):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, 'did not confirm'):
                model_profiles.confirm_glm(route, {**record, 'model': model})

    def test_update_preserves_choices_requires_current_digest_and_leaves_snapshot(self):
        inventory, first = self.draft()
        installed = model_profiles.install(first['path'], first['sha256'], self.root/'profile.json')
        task, reference = self.bound_task(first)
        rec = {**self.choices, 'budget': 'large', 'roles': {}}
        cli.write(self.root/'next.json', rec)
        draft = model_profiles.draft(inventory['path'], inventory['sha256'], self.root/'next.json',
                                     self.root/'new-draft.json', previous=installed['path'],
                                     previous_sha256=installed['sha256'])
        new = model_profiles.install(draft['path'], draft['sha256'], installed['path'],
                                     previous_sha256=installed['sha256'])
        with self.assertRaisesRegex(ValueError, 'digest changed'):
            model_profiles.install(draft['path'], draft['sha256'], installed['path'],
                                   previous_sha256=installed['sha256'])
        self.assertEqual(model_profiles.load(new['path'], new['sha256'])['choices']['roles'], self.choices['roles'])
        self.assertEqual(model_profiles.load(task.with_name(model_profiles.FILE), reference['sha256'])['choices']['budget'],
                         'small')

    def test_stage_records_exact_model_and_rejects_wrong_observation(self):
        _, draft = self.draft()
        task, reference = self.bound_task(draft)
        editor = self.editor
        with patch.object(autopilot, 'execute_model', editor):
            result = autopilot._model(task, 'implementation', 'make a change', read_only=False, model=editor)
        self.assertEqual(json.loads(result), {'available': True})
        record = cli.read(task)  # Bound profile remains in the original task.
        self.assertEqual(record['model_profile'], reference)
        reply = json.loads(task.with_name('implementation-model.json').read_text())
        self.assertEqual(reply['model_route']['profile_sha256'], reference['sha256'])
        self.assertTrue(reply['model_route']['cli_model_confirmed'])
        self.assertEqual(self.calls[-1], ('glm', 'glm-profile', 'medium'))

        def wrong(*args, **kwargs):
            return {**self.editor(*args, **kwargs), 'model': 'glm-wrong'}

        with patch.object(autopilot, 'execute_model', wrong):
            with self.assertRaisesRegex(autopilot.AutopilotError, 'output saved'):
                autopilot._model(task, 'repair-1', 'fix again', read_only=False, model=wrong)
        saved = json.loads(task.with_name('repair-1-model.json').read_text())
        self.assertFalse(saved['model_route']['cli_model_confirmed'])
        self.assertEqual(saved['model_route']['effort'], 'low')

    def test_replay_copies_snapshot_and_changed_profile_prevents_invocation(self):
        _, draft = self.draft()
        task, reference = self.bound_task(draft)
        for file in ('issue.txt', 'input-base.txt'):
            task.with_name(file).write_text('task input')
        child = self.root/'child'/'task.json'
        child.parent.mkdir()
        task_replay._copy_inputs(task, child)
        self.assertEqual(cli.digest(child.with_name(model_profiles.FILE)), reference['sha256'])
        route = task.with_name(model_profiles.FILE)
        route.write_bytes(route.read_bytes()+b' ')
        call_count = len(self.calls)
        with patch.object(autopilot, 'execute_model', self.editor):
            with self.assertRaisesRegex(ValueError, 'digest changed'):
                autopilot._model(task, 'implementation', 'make a change', read_only=False, model=self.editor)
        self.assertEqual(len(self.calls), call_count)


if __name__ == '__main__':
    unittest.main()
