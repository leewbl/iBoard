import json
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from model import codex_review
from workbench import autopilot, cli, model_profiles, task_replay


GLM = {'provider': 'glm', 'model': 'glm-profile'}
OPENAI = {'provider': 'openai', 'model': 'gpt-review'}
RETIRED = {'provider': 'openai', 'model': 'gpt-retired'}


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

    def test_catalog_route_requires_inference_before_profile_approval(self):
        dest = self.root/'catalog.json'
        catalog = model_profiles.discover(dest,
            list_models=lambda *, timeout: [{'model': OPENAI['model'], 'efforts': ['low'], 'default': True}])
        self.assertEqual(cli.digest(dest), catalog['sha256'])
        self.assertEqual(cli.read(dest)['kind'], 'catalog')
        choices = self.root/'choices.json'
        cli.write(choices, self.choices)
        with self.assertRaisesRegex(ValueError, 'complete role inventory'):
            model_profiles.draft(dest, catalog['sha256'], choices, self.root/'draft.json')

    def test_rejected_model_does_not_stop_remaining_models(self):
        cli.write(self.probes, {'schema': 1, 'seconds': 75, 'engines': [
            {**RETIRED, 'efforts': ['low', 'high']}, {**OPENAI, 'efforts': ['low']},
            {**GLM, 'efforts': ['low', 'medium']}]})

        def judge(prompt, model, timeout, *, effort):
            reply = self.reviewer(prompt, model, timeout, effort=effort)
            if model == RETIRED['model']:
                raise codex_review.ModelUnavailable('model_not_found')
            return reply

        result = model_profiles.probe(self.probes, self.root/'inventory.json', editor=self.editor, reviewer=judge)
        self.assertEqual([row['status'] for row in result['probes']],
                         ['unavailable', 'unverified', 'confirmed', 'confirmed', 'confirmed'])
        self.assertEqual([entry[1] for entry in self.calls], [RETIRED['model'], OPENAI['model'], GLM['model'], GLM['model']])
        self.assertEqual(result['probes'][0]['reason'], 'model_not_found')
        choices = self.root/'choices.json'
        cli.write(choices, self.choices)
        profile = model_profiles.draft(result['path'], result['sha256'], choices, self.root/'draft.json')
        self.assertEqual(profile['routes']['arena-cross-judge-pool'][-1]['model'], OPENAI['model'])

    def test_model_retired_mid_probe_cannot_use_earlier_confirmed_effort(self):
        cli.write(self.probes, {'schema': 1, 'seconds': 75, 'engines': [
            {**OPENAI, 'efforts': ['low', 'medium']}, {**GLM, 'efforts': ['low', 'medium']}]})

        def judge(prompt, model, timeout, *, effort):
            if effort == 'medium':
                raise codex_review.ModelUnavailable('model_not_found')
            return self.reviewer(prompt, model, timeout, effort=effort)

        inventory = model_profiles.probe(self.probes, self.root/'inventory.json', editor=self.editor, reviewer=judge)
        self.assertEqual([item['status'] for item in inventory['probes'][:2]], ['confirmed', 'unavailable'])
        choices = self.root/'choices.json'
        cli.write(choices, self.choices)
        with self.assertRaisesRegex(ValueError, 'needs a confirmed openai/gpt-review'):
            model_profiles.draft(inventory['path'], inventory['sha256'], choices, self.root/'draft.json')

    def test_openai_quota_stops_other_openai_probes(self):
        cli.write(self.probes, {'schema': 1, 'seconds': 75, 'engines': [
            {**RETIRED, 'efforts': ['low']}, {**OPENAI, 'efforts': ['low']}, {**GLM, 'efforts': ['low']}]})

        def judge(prompt, model, timeout, *, effort):
            self.reviewer(prompt, model, timeout, effort=effort)
            raise codex_review.ReviewError('HTTP 429')

        result = model_profiles.probe(self.probes, self.root/'inventory.json', editor=self.editor, reviewer=judge)
        self.assertEqual([item['status'] for item in result['probes']], ['unverified', 'unverified', 'confirmed'])
        self.assertEqual([item[1] for item in self.calls], [RETIRED['model'], GLM['model']])

    def legacy_rule(self):
        path = self.root/'pstack-models.mdc'
        path.write_text(
            '---\ndescription: Legacy fixture model roles\nalwaysApply: true\n---\n'
            '# defaults: 2\n# budget: small (medium)\n'
            'feature, refactoring: auto\nbug-fix: glm-profile\nperf-issue: inherit-parent\n'
            'hillclimb: glm-profile\njudgment and prose: glm-profile\n'
            'arena runners: auto, gpt-review\n'
            'arena cross-judge pool: claude-fable-old, claude-opus-old, gemini-4-old\n'
            'swarm workers: inherit-parent\ninterrogate reviewers: gpt-review, auto\n'
            'architect runners: glm-profile\n')
        return path

    def test_rule_migration_uses_verified_mappings_and_keeps_its_audit_snapshot(self):
        path = self.legacy_rule()
        original = path.read_bytes()
        preview = model_profiles.inspect_rule(path, self.root/'rule-plan.json')
        plan = cli.read(Path(preview['path']))
        self.assertEqual(plan['rule_sha256'], sha256(original).hexdigest())
        self.assertEqual(preview['budget'], 'small')
        self.assertTrue(any(change['role'] == 'architect runners' and change['after'] is None
                            for change in preview['changes']))
        mapping = self.root/'mapping.json'
        cli.write(mapping, {'schema': 1, 'parent': GLM, 'models': {
            'claude-opus-5-5-high-fast': GLM, 'gemini-4-neon-xhigh': OPENAI}})
        inventory = self.inventory()
        draft = model_profiles.draft_rule(preview['path'], preview['sha256'], mapping, cli.digest(mapping),
                                          inventory['path'], inventory['sha256'], self.root/'draft.json')
        installed = model_profiles.install(draft['path'], draft['sha256'], self.root/'installed.json')
        path_approved = Path(installed['path'])
        Path(preview['path']).unlink()
        Path(inventory['path']).unlink()
        mapping.unlink()
        saved = model_profiles.load(path_approved, installed['sha256'])
        self.assertEqual(saved['choices']['roles']['arena-runners'], ['auto', OPENAI])
        self.assertEqual(saved['choices']['roles']['interrogate-reviewers'], [OPENAI, 'auto'])
        self.assertEqual(model_profiles.select(saved, 'arena-cross-judge-pool', other_than='glm')['model'],
                         OPENAI['model'])
        self.assertEqual(saved['migration']['plan_sha256'], preview['sha256'])
        self.assertEqual(path.read_bytes(), original)
        saved['migration']['plan']['roles']['feature, refactoring'] = 'inherit-parent'
        saved['migration']['plan_sha256'] = model_profiles._canonical_digest(saved['migration']['plan'])
        cli.write(path_approved, saved)
        with self.assertRaisesRegex(ValueError, 'approved rule migration'):
            model_profiles.load(path_approved, cli.digest(path_approved))

    def test_migration_requires_explicit_mapping_and_confirmed_model_for_each_role(self):
        rule = self.legacy_rule()
        preview = model_profiles.inspect_rule(rule, self.root/'rule-plan.json')
        inventory = self.inventory()
        mapping = self.root/'mapping.json'
        choices = {'schema': 1, 'parent': GLM, 'models': {}}
        cli.write(mapping, choices)
        out = self.root/'draft.json'

        def draft():
            return model_profiles.draft_rule(preview['path'], preview['sha256'], mapping, cli.digest(mapping),
                                             inventory['path'], inventory['sha256'], out)

        with self.assertRaisesRegex(ValueError, 'explicit mappings for: claude-opus.*gemini'):
            draft()
        choices['models'] = {'claude-opus-5-5-high-fast': GLM, 'gemini-4-neon-xhigh': RETIRED}
        cli.write(mapping, choices)
        with self.assertRaisesRegex(ValueError, 'needs a confirmed openai/gpt-retired'):
            draft()
        self.assertFalse(out.exists())

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
