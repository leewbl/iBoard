import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import test_adopt as fixtures
from workbench import autopilot, cli, replan, start


class ReplanTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdoptionTests(methodName='test_rebinds_input_and_preserves_baseline_candidate_and_old_task')
        self.fixture.setUp()
        info = self.fixture.create()
        self.task = Path(info['task'])
        self.workspace = Path(info['workspace'])
        self.state = self.task.parent / 'autopilot.json'
        self.plan = {'status': 'ready', **{name: {'command': ['node', '-e', 'console.log("OBSERVED")'],
                     'marker': 'OBSERVED', 'exit_code': 0} for name in ('reproduction', 'resolution', 'tests')}}

    def tearDown(self):
        self.fixture.tearDown()

    def seed(self, phase='reproduce'):
        state = {'schema': cli.SCHEMA, 'phase': phase, 'plan': copy.deepcopy(self.plan),
                 'issue_digest': cli.digest(self.task.parent / 'issue.txt'), 'input_digest': None}
        cli.write(self.state, state)
        return state

    def test_unplanned_adoption_accepts_explicit_plan_without_creating_model_or_runtime_history(self):
        before = self.task.read_bytes()
        result = replan.replace(self.task, self.plan, 'Reviewed real runtime harness')
        state = cli.read(self.state)
        self.assertTrue(result['changed'])
        self.assertEqual(self.task.read_bytes(), before)
        self.assertEqual(state['phase'], 'reproduce')
        self.assertEqual(state['plan_revisions'][0]['previous_phase'], 'unplanned')
        self.assertIsNone(state['plan_revisions'][0]['previous_plan'])
        self.assertEqual(state['plan_revisions'][0]['source_kind'], 'explicit-supplied-plan')
        self.assertNotIn('baseline_reports', state)
        self.assertFalse((self.task.parent / 'plan-model.json').exists())
        replan.validate_binding(self.task, cli.read(self.task), state)

    def test_replacement_preserves_original_plan_and_is_idempotent(self):
        old = self.seed()
        new = copy.deepcopy(self.plan)
        new['reproduction']['command'][-1] = 'console.log("REAL_LOCK")'
        new['reproduction']['marker'] = 'REAL_LOCK'
        before = self.task.read_bytes()
        replan.replace(self.task, new, 'Old harness did not reproduce')
        state = cli.read(self.state)
        self.assertEqual(state['plan_revisions'][0]['previous_plan'], old['plan'])
        self.assertEqual(state['phase'], 'reproduce')
        self.assertEqual(self.task.read_bytes(), before)
        snapshot = self.state.read_bytes()
        self.assertFalse(replan.replace(self.task, new, 'Same plan again')['changed'])
        self.assertEqual(snapshot, self.state.read_bytes())

    def test_accepts_bound_planning_state(self):
        self.seed('plan')
        replan.replace(self.task, self.plan, 'Explicit plan after failed model planning')
        self.assertEqual(cli.read(self.state)['plan_revisions'][0]['previous_phase'], 'plan')

    def test_rejects_stage_input_acceptance_and_adoption_changes(self):
        mutations = [
            lambda d: d['criteria'][0].update(text='weaker'),
            lambda d: d['adoption'].update(memory_mb=7168),
            lambda d: d.update(base_commit='b' * 40),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                original = cli.read(self.task)
                changed = copy.deepcopy(original)
                mutate(changed)
                cli.write(self.task, changed)
                try:
                    with self.assertRaises(replan.ReplanError):
                        replan.replace(self.task, self.plan, 'Replacement')
                    self.assertFalse(self.state.exists())
                finally:
                    cli.write(self.task, original)
        request = self.task.parent / 'issue.txt'
        request.write_text('changed input')
        with self.assertRaises(replan.ReplanError):
            replan.replace(self.task, self.plan, 'Replacement')

    def test_rejects_dirty_baseline_and_existing_evidence(self):
        (self.workspace / 'code').write_text('dirty')
        with self.assertRaisesRegex(replan.ReplanError, 'clean baseline'):
            replan.replace(self.task, self.plan, 'Replacement')
        self.fixture.git('checkout', '--', 'code', repo=self.workspace)
        data = cli.read(self.task)
        data['evidence'] = [{'kind': 'reproduction', 'id': 1}]
        cli.write(self.task, data)
        with self.assertRaisesRegex(replan.ReplanError, 'without successful evidence'):
            replan.replace(self.task, self.plan, 'Replacement')

    def test_rejects_running_later_phases_and_prior_baseline_reports(self):
        for phase in ('verify', 'delivery', 'adopting', 'implement'):
            self.seed(phase)
            with self.assertRaises(replan.ReplanError):
                replan.replace(self.task, self.plan, 'Replacement')
        state = self.seed()
        state['baseline_reports'] = ['old-report']
        cli.write(self.state, state)
        with self.assertRaises(replan.ReplanError):
            replan.replace(self.task, self.plan, 'Replacement')
        self.seed()
        data = cli.read(self.task)
        data['status'] = 'running'
        cli.write(self.task, data)
        with self.assertRaises(replan.ReplanError):
            replan.replace(self.task, self.plan, 'Replacement')

    def test_rejects_changed_criteria_invalid_command_and_evidence_in_plan(self):
        variants = []
        changed = copy.deepcopy(self.plan)
        changed['criteria'] = [{'id': 'fake', 'text': 'weaker'}]
        variants.append(changed)
        changed = copy.deepcopy(self.plan)
        changed['reproduction']['command'] = ['node', 'x' * 8193]
        variants.append(changed)
        changed = copy.deepcopy(self.plan)
        changed['baseline_reports'] = ['manual-pass']
        variants.append(changed)
        for plan in variants:
            with self.subTest(plan_keys=list(plan)):
                with self.assertRaises(replan.ReplanError):
                    replan.replace(self.task, plan, 'Replacement')
                self.assertFalse(self.state.exists())

    def test_atomic_publication_failure_keeps_original_state_and_task(self):
        self.seed()
        previous = self.state.read_bytes()
        task_bytes = self.task.read_bytes()
        new = copy.deepcopy(self.plan)
        new['tests']['marker'] = 'DIFFERENT'
        with patch('workbench.cli.os.replace', side_effect=OSError('injected before replace')):
            with self.assertRaises(OSError):
                replan.replace(self.task, new, 'Atomic failure')
        self.assertEqual(self.state.read_bytes(), previous)
        self.assertEqual(self.task.read_bytes(), task_bytes)

    def test_later_execution_refuses_modified_plan_or_contract(self):
        replan.replace(self.task, self.plan, 'Reviewed harness')
        original = cli.read(self.state)
        for phase in ('reproduce', 'verify', 'delivery'):
            state = copy.deepcopy(original)
            state['phase'] = phase
            state['plan']['tests']['marker'] = 'SILENT_CHANGE'
            cli.write(self.state, state)
            with self.assertRaisesRegex(start.StartError, 'replacement plan'):
                start.resume_task(self.task)

    def test_replacement_reexecutes_both_baseline_checks_and_independent_review_before_adoption(self):
        replan.replace(self.task, self.plan, 'Reviewed harness')
        calls = []
        report = self.task.parent / 'fresh-baseline-report.json'
        report.write_text('{"actual": "baseline observation"}')
        def check(**kwargs):
            calls.append((kwargs['kind'], kwargs['actor'], kwargs['command']))
            return {'report': str(report)}
        def model(workspace, prompt, timeout, read_only):
            self.assertIn('actual baseline run', prompt)
            return {'exit_code': 0, 'timed_out': False,
                    'stdout': json.dumps({'status': 'blocked', 'reason': 'Independent review rejects run'})}
        def run(task, issue, image, **kwargs):
            return autopilot.run(task, issue, image, check=check, model=model, **kwargs)
        with self.assertRaisesRegex(start.StartError, 'Independent review rejects run'):
            start.resume_task(self.task, run=run)
        self.assertEqual([(kind, actor) for kind, actor, _ in calls],
                         [('reproduction', 'author'), ('reproduction', 'independent-verifier')])
        self.assertTrue(all(command == self.plan['reproduction']['command'] for _, _, command in calls))
        self.assertEqual(cli.sha(self.workspace, 'HEAD'), cli.read(self.task)['base_commit'])
        self.assertEqual(cli.read(self.state)['phase'], 'reproduce')
        self.assertEqual(cli.read(self.task)['evidence'], [])

    def test_replacement_history_and_late_acceptance_are_revalidated(self):
        replan.replace(self.task, self.plan, 'Reviewed harness')
        data = cli.read(self.task)
        state = cli.read(self.state)
        state['plan_revisions'][0]['previous_plan'] = {'forged': True}
        with self.assertRaisesRegex(replan.ReplanError, 'history'):
            replan.validate_binding(self.task, data, state)
        state = cli.read(self.state)
        state['phase'] = 'delivery'
        data['criteria'][0]['text'] = 'weaker after reproduction'
        with self.assertRaises(replan.ReplanError):
            replan.validate_binding(self.task, data, state)

    def test_multiple_replacements_retain_a_verified_digest_chain(self):
        replan.replace(self.task, self.plan, 'First explicit harness')
        next_plan = copy.deepcopy(self.plan)
        next_plan['tests']['marker'] = 'NEW_MARKER'
        replan.replace(self.task, next_plan, 'Corrected observation marker')
        state = cli.read(self.state)
        self.assertEqual(len(state['plan_revisions']), 2)
        self.assertEqual(state['plan_revisions'][1]['previous_plan_digest'],
                         state['plan_revisions'][0]['new_plan_digest'])
        replan.validate_binding(self.task, cli.read(self.task), state)

    def test_real_cli_accepts_plan_but_keeps_task_paused_without_runtime_evidence(self):
        plan_path = self.task.parent / 'explicit-plan.json'
        plan_path.write_text(json.dumps(self.plan))
        process = subprocess.run([sys.executable, '-m', 'workbench.replan', '--task', str(self.task),
            '--plan-file', str(plan_path), '--reason', 'Reviewed supplied runtime harness'],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(json.loads(process.stdout)['changed'])
        data = cli.read(self.task)
        self.assertEqual(data['status'], 'paused')
        self.assertEqual(data['evidence'], [])
        self.assertEqual(cli.read(self.state)['phase'], 'reproduce')


class OrdinaryReplanTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdoptionTests(methodName='test_rebinds_input_and_preserves_baseline_candidate_and_old_task')
        self.fixture.setUp()
        issue = self.fixture.root / 'request.txt'
        issue.write_text('reported ordinary bug')
        captured = {}
        def paused(task, request, image, **kwargs):
            captured['task'] = task
            data = cli.read(task)
            data['status'] = 'paused'
            data['criteria'] = [{'id': 'fix', 'text': 'fix the failure', 'revision': 1}]
            cli.write(task, data)
        with patch('workbench.start.subprocess.run', side_effect=self.fixture.fake_run):
            start.start(task_id='ordinary', repo=self.fixture.repo, issue_file=issue,
                        source='reported ordinary bug', kind='bug', surface='non-ui', iterations=10, seconds=300,
                        image='baseline', tasks_root=self.fixture.root / 'tasks',
                        workspaces_root=self.fixture.root / 'workspaces', run=paused)
        self.task = Path(captured['task'])
        self.state = self.task.parent / 'autopilot.json'
        self.plan = {'status': 'ready', **{name: {'command': ['node', '-e', 'console.log("OBSERVED")'],
                     'marker': 'OBSERVED', 'exit_code': 0} for name in ('reproduction', 'resolution', 'tests')}}

    def tearDown(self):
        self.fixture.tearDown()

    def test_ordinary_replacement_preserves_task_and_binds_later_runs(self):
        before = self.task.read_bytes()
        replan.replace(self.task, self.plan, 'Correct the real reproduction fixture')
        self.assertEqual(self.task.read_bytes(), before)
        data, state = cli.read(self.task), cli.read(self.state)
        self.assertNotIn('adoption', data)
        self.assertEqual(state['plan_revisions'][0]['contract']['kind'], 'ordinary')
        for phase in ('reproduce', 'verify', 'delivery'):
            state['phase'] = phase
            replan.validate_binding(self.task, data, state)
            changed = copy.deepcopy(data)
            changed['criteria'][0]['revision'] += 1
            with self.assertRaises(replan.ReplanError):
                replan.validate_binding(self.task, changed, state)

    def test_ordinary_contract_detects_input_settings_criteria_and_history_changes(self):
        replan.replace(self.task, self.plan, 'Correct fixture')
        for filename in ('issue.txt', 'input-base.txt', 'input-stage.json', 'autopilot.json', 'task.json'):
            path = self.task if filename == 'task.json' else self.task.parent / filename
            original = path.read_bytes()
            mutations = [None]
            if filename == 'task.json':
                mutations = ['id', 'text', 'revision']
            for field in mutations:
                with self.subTest(file=filename, field=field):
                    if filename.endswith('.txt'):
                        path.write_text('tampered')
                    else:
                        data = json.loads(original)
                        if filename == 'input-stage.json':
                            data['run_settings']['memory_mb'] = 4096
                        elif filename == 'autopilot.json':
                            data['plan_revisions'][0]['previous_plan'] = {'forged': True}
                        else:
                            data['criteria'][0][field] = 'tampered'
                        cli.write(path, data)
                    try:
                        with self.assertRaises(replan.ReplanError):
                            replan.validate_binding(self.task, cli.read(self.task), cli.read(self.state))
                    finally:
                        path.write_bytes(original)

    def test_invalid_adoption_metadata_cannot_select_ordinary_contract(self):
        original = cli.read(self.task)
        for adoption in (None, False, [], 'ordinary', {}, {'image': 'baseline'}):
            with self.subTest(adoption=adoption):
                data = copy.deepcopy(original)
                data['adoption'] = adoption
                cli.write(self.task, data)
                with self.assertRaises(replan.ReplanError):
                    replan.replace(self.task, self.plan, 'Correct fixture')
                self.assertFalse(self.state.exists())
        cli.write(self.task, original)

    def test_ordinary_rejects_dirty_advanced_baseline_and_successful_evidence(self):
        data = cli.read(self.task)
        workspace = Path(data['repo'])
        (workspace / 'code').write_text('dirty')
        with self.assertRaises(replan.ReplanError):
            replan.replace(self.task, self.plan, 'Correct fixture')
        self.fixture.git('add', 'code', repo=workspace)
        self.fixture.git('commit', '-qm', 'advanced', repo=workspace)
        with self.assertRaises(replan.ReplanError):
            replan.replace(self.task, self.plan, 'Correct fixture')
        self.fixture.git('reset', '--hard', data['base_commit'], repo=workspace)
        data['evidence'] = [{'kind': 'reproduction', 'id': 1}]
        cli.write(self.task, data)
        with self.assertRaises(replan.ReplanError):
            replan.replace(self.task, self.plan, 'Correct fixture')

    def test_later_binding_rejects_changed_execution_arguments(self):
        replan.replace(self.task, self.plan, 'Correct fixture')
        settings = cli.read(self.task.parent / 'input-stage.json')['run_settings']
        replan.validate_binding(self.task, cli.read(self.task), cli.read(self.state), run_settings=settings)
        for key, value in {'image': 'other', 'pr_repo': 'other/repo', 'source_mode': 'image',
                           'candidate_image': 'other', 'build_source_image': True, 'memory_mb': 4096}.items():
            with self.subTest(setting=key):
                changed = dict(settings, **{key: value})
                with self.assertRaises(replan.ReplanError):
                    replan.validate_binding(self.task, cli.read(self.task), cli.read(self.state), run_settings=changed)


    def test_ordinary_early_workflow_guards_and_atomic_audit(self):
        base_state = {'schema': cli.SCHEMA, 'phase': 'reproduce', 'plan': self.plan,
                      'issue_digest': cli.digest(self.task.parent / 'issue.txt'),
                      'input_digest': autopilot._input_digest(self.task)}
        for change in ({'phase': 'verify'}, {'phase': 'implement'}, {'phase': 'delivery'},
                       {'baseline_reports': ['successful']}, {'candidate_commit': 'a' * 40},
                       {'input_digest': 'tampered'}, {'issue_digest': 'tampered'}):
            with self.subTest(change=change):
                cli.write(self.state, dict(base_state, **change))
                before = self.state.read_bytes()
                with self.assertRaises(replan.ReplanError):
                    replan.replace(self.task, self.plan, 'Correct fixture')
                self.assertEqual(self.state.read_bytes(), before)
        base_state['plan'] = None
        cli.write(self.state, base_state)
        before = self.state.read_bytes()
        with patch('workbench.cli.os.replace', side_effect=OSError('atomic failure')):
            with self.assertRaises(OSError):
                replan.replace(self.task, self.plan, 'Correct fixture')
        self.assertEqual(self.state.read_bytes(), before)
        replan.replace(self.task, self.plan, 'Correct fixture')
        new = copy.deepcopy(self.plan)
        new['tests']['marker'] = 'NEW_MARKER'
        replan.replace(self.task, new, 'Second reviewed correction')
        state = cli.read(self.state)
        self.assertEqual(len(state['plan_revisions']), 2)
        replan.validate_binding(self.task, cli.read(self.task), state)
        state['plan_revisions'][1]['previous_plan_digest'] = 'forged'
        with self.assertRaises(replan.ReplanError):
            replan.validate_binding(self.task, cli.read(self.task), state)

    def test_ordinary_resume_rejects_changed_contract_before_runtime_or_model(self):
        replan.replace(self.task, self.plan, 'Correct fixture')
        original = cli.read(self.state)
        for phase in ('reproduce', 'verify', 'delivery'):
            state = copy.deepcopy(original)
            state['phase'] = phase
            state['plan']['tests']['marker'] = 'TAMPERED'
            cli.write(self.state, state)
            with patch('workbench.autopilot._check') as runtime, patch('workbench.autopilot._model') as model:
                with self.assertRaisesRegex(start.StartError, 'replacement plan'):
                    start.resume_task(self.task)
                runtime.assert_not_called()
                model.assert_not_called()


    def test_direct_run_rejects_changed_settings_before_any_execution(self):
        replan.replace(self.task, self.plan, 'Correct fixture')
        settings = cli.read(self.task.parent / 'input-stage.json')['run_settings']
        for phase in ('reproduce', 'verify', 'delivery'):
            for key, value in {'image': 'other', 'pr_repo': 'other/repo', 'source_mode': 'image',
                               'candidate_image': 'other', 'build_source_image': True,
                               'memory_mb': 4096}.items():
                with self.subTest(phase=phase, setting=key):
                    data = cli.read(self.task)
                    data['status'] = 'running'
                    cli.write(self.task, data)
                    state = cli.read(self.state)
                    state['phase'] = phase
                    cli.write(self.state, state)
                    changed = dict(settings, **{key: value})
                    with patch('workbench.autopilot._check') as check, patch('workbench.autopilot._model') as model:
                        with self.assertRaises(autopilot.AutopilotError):
                            autopilot.run(self.task, self.task.parent / 'issue.txt', **changed)
                        check.assert_not_called()
                        model.assert_not_called()
