import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_autopilot
from test_provider_limits import ACTUAL_QUOTA
from workbench import autopilot, cli


class PartialImplementationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_autopilot.AutopilotTests('test_provider_limit_preserves_partial_implementation_and_baseline_evidence')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        f.issue = f.task.parent / 'issue.txt'
        f.issue.write_text('real task description')
        (f.task.parent / 'input-base.txt').write_text('real task description')
        cli.write(f.task.parent / 'input-stage.json', {
            'schema': 1, 'phase': 'ready', 'descriptions': [], 'input_digest': None,
            'base_digest': cli.digest(f.issue), 'request_digest': cli.digest(f.issue),
            'run_settings': {'image': 'image', 'pr_repo': None, 'source_mode': 'workspace',
                             'candidate_image': None, 'build_source_image': False, 'memory_mb': 2048}})
        def quota(workspace, prompt, timeout, *, read_only):
            if 'Implement this explicitly' in prompt:
                (workspace / 'code.txt').write_text('unverified partial')
                (workspace / 'new.txt').write_text('untracked partial')
                return {'exit_code': 1, 'timed_out': False, 'stdout': ACTUAL_QUOTA, 'stderr': ''}
            return f.model(workspace, prompt, timeout, read_only=read_only)
        with self.assertRaises(Exception):
            autopilot.run(f.task, f.issue, 'image', model=quota, check=f.check)

    def register(self):
        from workbench.partial_implementation import register
        f = self.fixture
        return register(f.task, f.task.parent / 'implementation-model.json')

    def test_register_and_continue_runs_native_verification_without_double_iteration(self):
        f = self.fixture
        before = cli.read(f.task)
        checkpoint = self.register()
        self.assertEqual(checkpoint['status'], 'ready')
        self.assertEqual(cli.read(f.task)['status'], 'paused')
        self.assertEqual((f.workspace / 'new.txt').read_text(), 'untracked partial')
        data = cli.read(f.task); data['status'] = 'running'; cli.write(f.task, data)
        def continuation(workspace, prompt, timeout, *, read_only):
            if 'Continue the interrupted implementation' in prompt:
                self.assertIn('real task description', prompt)
                self.assertIn('correct behavior', prompt)
                self.assertEqual((workspace / 'new.txt').read_text(), 'untracked partial')
                (workspace / 'code.txt').write_text('fixed')
                return {'exit_code': 0, 'timed_out': False, 'stdout': 'continued', 'stderr': ''}
            return f.model(workspace, prompt, timeout, read_only=read_only)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            autopilot.run(f.task, f.issue, 'image', model=continuation, check=f.check)
        self.assertEqual(cli.read(f.task)['iterations'], before['iterations'])
        self.assertIn('resolution:independent-verifier', f.calls)
        self.assertIn('final-review', f.calls)
        self.assertEqual(cli.read(f.task.parent / 'autopilot.json')['phase'], 'delivery')

    def test_changed_partial_after_registration_refuses_without_model(self):
        f = self.fixture
        self.register()
        (f.workspace / 'new.txt').write_text('tampered')
        data = cli.read(f.task); data['status'] = 'running'; cli.write(f.task, data)
        calls = []
        def forbidden(*args, **kwargs):
            calls.append(True); raise AssertionError('must not call model')
        with self.assertRaises(Exception):
            autopilot.run(f.task, f.issue, 'image', model=forbidden, check=f.check)
        self.assertEqual(calls, [])
        self.assertEqual((f.workspace / 'new.txt').read_text(), 'tampered')

    def test_second_quota_stops_without_commit_or_automatic_replay(self):
        f = self.fixture
        self.register()
        base = cli.sha(f.workspace, 'HEAD')
        data = cli.read(f.task); data['status'] = 'running'; cli.write(f.task, data)
        def quota(workspace, prompt, timeout, *, read_only):
            (workspace / 'code.txt').write_text('new partial')
            return {'exit_code': 1, 'timed_out': False, 'stdout': ACTUAL_QUOTA, 'stderr': ''}
        with self.assertRaises(Exception):
            autopilot.run(f.task, f.issue, 'image', model=quota, check=f.check)
        self.assertEqual(cli.sha(f.workspace, 'HEAD'), base)
        self.assertEqual((f.workspace / 'code.txt').read_text(), 'new partial')
        self.assertEqual(cli.read(f.task)['status'], 'paused')
        data = cli.read(f.task); data['status'] = 'running'; cli.write(f.task, data)
        with self.assertRaises(Exception):
            autopilot.run(f.task, f.issue, 'image', model=lambda *a, **k: self.fail('replayed'), check=f.check)

    def test_no_checkpoint_preserves_original_interruption_refusal(self):
        f = self.fixture
        data = cli.read(f.task); data['status'] = 'running'; cli.write(f.task, data)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'implementation was interrupted'):
            autopilot.run(f.task, f.issue, 'image', model=lambda *a, **k: self.fail('model called'), check=f.check)

    def test_registration_rejects_staged_index_without_changing_worktree(self):
        f = self.fixture
        subprocess.run(['git', '-C', str(f.workspace), 'add', 'new.txt'], check=True)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'staged'):
            self.register()
        self.assertEqual((f.workspace / 'new.txt').read_text(), 'untracked partial')
        self.assertFalse((f.task.parent / 'partial-implementation-checkpoint.json').exists())

    def test_registration_rejects_nonquota_or_missing_semantic_pass(self):
        f = self.fixture
        report = f.task.parent / 'implementation-model.json'; original = report.read_text()
        cli.write(report, {'exit_code': 1, 'timed_out': False, 'stdout': 'ordinary error', 'stderr': ''})
        with self.assertRaisesRegex(autopilot.AutopilotError, 'provider limit'):
            self.register()
        report.write_text(original)
        review = f.task.parent / 'baseline-review-model.json'; value = json.loads(review.read_text())
        value['stdout'] = '{"status":"ready","verdict":"FAIL"}'; cli.write(review, value)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'semantic PASS'):
            self.register()
        self.assertFalse((f.task.parent / 'partial-implementation-checkpoint.json').exists())

    def test_index_flags_cannot_hide_changes_before_or_after_registration(self):
        from workbench import partial_implementation
        f = self.fixture
        subprocess.run(['git', '-C', str(f.workspace), 'update-index', '--assume-unchanged', 'code.txt'], check=True)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'assume-unchanged'):
            self.register()
        subprocess.run(['git', '-C', str(f.workspace), 'update-index', '--no-assume-unchanged', 'code.txt'], check=True)
        self.register()
        subprocess.run(['git', '-C', str(f.workspace), 'update-index', '--skip-worktree', 'code.txt'], check=True)
        data = cli.read(f.task); state = cli.read(f.task.parent / 'autopilot.json')
        settings = cli.read(f.task.parent / 'input-stage.json')['run_settings']
        with self.assertRaisesRegex(autopilot.AutopilotError, 'skip-worktree'):
            partial_implementation.claim(f.task, data, state, settings)
        self.assertEqual(cli.read(f.task.parent / 'partial-implementation-checkpoint.json')['status'], 'ready')

    def test_registration_respects_original_budget(self):
        f = self.fixture
        data = cli.read(f.task); data['limits']['iterations'] = 0; cli.write(f.task, data)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'original limits'):
            self.register()
        self.assertEqual(cli.read(f.task)['limits']['iterations'], 0)

    def test_checkpoint_rejects_file_mode_and_new_paths(self):
        from workbench import partial_implementation
        f = self.fixture
        self.register()
        data = cli.read(f.task); state = cli.read(f.task.parent / 'autopilot.json')
        settings = cli.read(f.task.parent / 'input-stage.json')['run_settings']
        path = f.workspace / 'new.txt'
        old_mode = path.stat().st_mode
        path.chmod(0o755)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'checkpoint'):
            partial_implementation.claim(f.task, data, state, settings)
        path.chmod(old_mode)
        extra = f.workspace / 'additional.txt'; extra.write_text('unexpected')
        with self.assertRaisesRegex(autopilot.AutopilotError, 'checkpoint'):
            partial_implementation.claim(f.task, data, state, settings)
        self.assertEqual(cli.read(f.task.parent / 'partial-implementation-checkpoint.json')['status'], 'ready')

    def test_checkpoint_rejects_criteria_history_and_report_changes(self):
        from workbench import partial_implementation
        f = self.fixture
        self.register()
        data = cli.read(f.task); state = cli.read(f.task.parent / 'autopilot.json')
        settings = cli.read(f.task.parent / 'input-stage.json')['run_settings']
        data['criterion_changes'].append({'reason': 'unexpected'})
        with self.assertRaisesRegex(autopilot.AutopilotError, 'checkpoint'):
            partial_implementation.claim(f.task, data, state, settings)
        data = cli.read(f.task)
        report = Path(state['baseline_reports'][0]); report.write_text('{}')
        with self.assertRaises(autopilot.AutopilotError):
            partial_implementation.claim(f.task, data, state, settings)
        self.assertEqual(cli.read(f.task.parent / 'partial-implementation-checkpoint.json')['status'], 'ready')

    def test_changed_settings_provider_or_input_refuse_claim(self):
        from workbench import partial_implementation
        f = self.fixture
        self.register()
        data = cli.read(f.task); state = cli.read(f.task.parent / 'autopilot.json')
        settings = cli.read(f.task.parent / 'input-stage.json')['run_settings']
        with self.assertRaisesRegex(autopilot.AutopilotError, 'checkpoint'):
            partial_implementation.claim(f.task, data, state, {**settings, 'memory_mb': 4096})
        issue = f.task.parent / 'issue.txt'; original = issue.read_text(); issue.write_text('changed')
        with self.assertRaises(autopilot.AutopilotError):
            partial_implementation.claim(f.task, data, state, settings)
        issue.write_text(original)
        provider = f.task.parent / 'implementation-model.json'; provider.write_text('{}')
        with self.assertRaises(autopilot.AutopilotError):
            partial_implementation.claim(f.task, data, state, settings)

    def test_input_changed_during_model_refuses_commit_and_preserves_partial(self):
        f = self.fixture
        self.register(); base = cli.sha(f.workspace, 'HEAD')
        data = cli.read(f.task); data['status'] = 'running'; cli.write(f.task, data)
        def changed_issue(workspace, prompt, timeout, *, read_only):
            (workspace / 'code.txt').write_text('continued partial')
            f.issue.write_text('changed during model')
            return {'exit_code': 0, 'timed_out': False, 'stdout': 'done', 'stderr': ''}
        with self.assertRaises(autopilot.AutopilotError):
            autopilot.run(f.task, f.issue, 'image', model=changed_issue, check=f.check)
        self.assertEqual(cli.sha(f.workspace, 'HEAD'), base)
        self.assertEqual((f.workspace / 'code.txt').read_text(), 'continued partial')
        self.assertEqual(cli.read(f.task)['status'], 'paused')

    def test_interrupt_pauses_and_checkpoint_cannot_be_replayed(self):
        f = self.fixture
        self.register(); base = cli.sha(f.workspace, 'HEAD')
        data = cli.read(f.task); data['status'] = 'running'; cli.write(f.task, data)
        def interrupted(workspace, prompt, timeout, *, read_only):
            (workspace / 'code.txt').write_text('interrupted again')
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            autopilot.run(f.task, f.issue, 'image', model=interrupted, check=f.check)
        self.assertEqual(cli.sha(f.workspace, 'HEAD'), base)
        self.assertEqual(cli.read(f.task)['status'], 'paused')
        self.assertEqual(cli.read(f.task.parent / 'partial-implementation-checkpoint.json')['status'], 'attempted')

    def test_commit_failure_preserves_model_changes_and_consumes_checkpoint(self):
        f = self.fixture
        self.register(); base = cli.sha(f.workspace, 'HEAD')
        data = cli.read(f.task); data['status'] = 'running'; cli.write(f.task, data)
        def success(workspace, prompt, timeout, *, read_only):
            (workspace / 'code.txt').write_text('model continuation')
            return {'exit_code': 0, 'timed_out': False, 'stdout': 'done', 'stderr': ''}
        with patch.object(autopilot, '_commit_changes', side_effect=OSError('commit unavailable')):
            with self.assertRaises(autopilot.AutopilotError):
                autopilot.run(f.task, f.issue, 'image', model=success, check=f.check)
        self.assertEqual(cli.sha(f.workspace, 'HEAD'), base)
        self.assertEqual((f.workspace / 'code.txt').read_text(), 'model continuation')
        self.assertEqual(cli.read(f.task)['status'], 'paused')
        self.assertEqual(cli.read(f.task.parent / 'partial-implementation-checkpoint.json')['status'], 'attempted')

    def test_state_write_failure_after_commit_requires_manual_inspection(self):
        f = self.fixture
        self.register(); base = cli.sha(f.workspace, 'HEAD')
        data = cli.read(f.task); data['status'] = 'running'; cli.write(f.task, data)
        def success(workspace, prompt, timeout, *, read_only):
            (workspace / 'code.txt').write_text('model continuation')
            return {'exit_code': 0, 'timed_out': False, 'stdout': 'done', 'stderr': ''}
        original_write = cli.write
        def fail_publication(path, value):
            if Path(path).name == 'autopilot.json' and value.get('phase') == 'verify':
                raise OSError('state publication failed')
            return original_write(path, value)
        with patch.object(cli, 'write', side_effect=fail_publication):
            with self.assertRaises(autopilot.AutopilotError):
                autopilot.run(f.task, f.issue, 'image', model=success, check=f.check)
        committed = cli.sha(f.workspace, 'HEAD')
        self.assertNotEqual(committed, base)
        self.assertEqual(cli.sha(f.workspace, 'HEAD^'), base)
        self.assertEqual(cli.read(f.task.parent / 'autopilot.json')['phase'], 'implementing')
        self.assertEqual(cli.read(f.task)['status'], 'paused')
        data = cli.read(f.task); data['status'] = 'running'; cli.write(f.task, data)
        with self.assertRaises(autopilot.AutopilotError):
            autopilot.run(f.task, f.issue, 'image', model=lambda *a, **k: self.fail('replayed'), check=f.check)


class CleanInterruptedRepairTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_autopilot.AutopilotTests('test_reverify_preserves_previous_review_and_creates_a_new_one')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        f.issue = f.task.parent / 'issue.txt'; f.issue.write_text('real task description')
        (f.task.parent / 'input-base.txt').write_text('real task description')
        cli.write(f.task.parent / 'input-stage.json', {
            'schema': 1, 'phase': 'ready', 'descriptions': [], 'input_digest': None,
            'base_digest': cli.digest(f.issue), 'request_digest': cli.digest(f.issue),
            'run_settings': {'image': 'image', 'pr_repo': None, 'source_mode': 'workspace',
                             'candidate_image': None, 'build_source_image': False, 'memory_mb': 2048}})
        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            autopilot.run(f.task, f.issue, 'image', model=f.model, check=f.check)
        state = cli.read(f.task.parent / 'autopilot.json')
        state['phase'] = 'repairing'; cli.write(f.task.parent / 'autopilot.json', state)
        data = cli.read(f.task); data['status'] = 'running'; cli.write(f.task, data)
        f.calls.clear()

    def test_explicit_reverify_reruns_full_verification_on_same_candidate(self):
        f = self.fixture
        candidate = cli.sha(f.workspace, 'HEAD')
        old = len(list(f.task.parent.glob('final-review-model-previous-*')))
        iterations = cli.read(f.task)['iterations']
        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            autopilot.run(f.task, f.issue, 'image', model=f.model, check=f.check, reverify=True)
        self.assertEqual(cli.sha(f.workspace, 'HEAD'), candidate)
        self.assertEqual(cli.read(f.task)['iterations'], iterations)
        self.assertEqual(f.calls, ['resolution:implementer', 'test-result:implementer',
            'runtime-log:implementer', 'resolution:independent-verifier',
            'test-result:independent-verifier', 'final-review'])
        self.assertEqual(len(list(f.task.parent.glob('final-review-model-previous-*'))), old + 1)

    def test_dirty_repair_is_refused_before_check_or_model(self):
        f = self.fixture
        (f.workspace / 'code.txt').write_text('unverified repair')
        with self.assertRaisesRegex(autopilot.AutopilotError, 'unchanged candidate'):
            autopilot.run(f.task, f.issue, 'image', model=lambda *a, **k: self.fail('model called'),
                          check=lambda **k: self.fail('check called'), reverify=True)
        self.assertEqual((f.workspace / 'code.txt').read_text(), 'unverified repair')

    def test_implementing_cannot_use_reverify(self):
        f = self.fixture
        state = cli.read(f.task.parent / 'autopilot.json'); state['phase'] = 'implementing'
        cli.write(f.task.parent / 'autopilot.json', state)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'unchanged candidate'):
            autopilot.run(f.task, f.issue, 'image', model=lambda *a, **k: self.fail('model called'),
                          check=lambda **k: self.fail('check called'), reverify=True)

    def test_changed_stage_or_evidence_blocks_reverify(self):
        f = self.fixture
        stage_path = f.task.parent / 'input-stage.json'
        stage = cli.read(stage_path); stage['run_settings']['memory_mb'] = 4096; cli.write(stage_path, stage)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'settings'):
            autopilot.run(f.task, f.issue, 'image', model=lambda *a, **k: self.fail('model called'),
                          check=lambda **k: self.fail('check called'), reverify=True)

    def test_changed_head_blocks_reverify(self):
        f = self.fixture
        (f.workspace / 'code.txt').write_text('other commit')
        subprocess.run(['git', '-C', str(f.workspace), 'add', 'code.txt'], check=True)
        subprocess.run(['git', '-C', str(f.workspace), 'commit', '-qm', 'other'], check=True)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'unchanged candidate'):
            autopilot.run(f.task, f.issue, 'image', model=lambda *a, **k: self.fail('model called'),
                          check=lambda **k: self.fail('check called'), reverify=True)

    def test_hidden_dirty_index_flag_blocks_reverify(self):
        f = self.fixture
        subprocess.run(['git', '-C', str(f.workspace), 'update-index', '--assume-unchanged', 'code.txt'], check=True)
        (f.workspace / 'code.txt').write_text('hidden unverified repair')
        with self.assertRaisesRegex(autopilot.AutopilotError, 'assume-unchanged'):
            autopilot.run(f.task, f.issue, 'image', model=lambda *a, **k: self.fail('model called'),
                          check=lambda **k: self.fail('check called'), reverify=True)
        self.assertEqual((f.workspace / 'code.txt').read_text(), 'hidden unverified repair')

    def test_clean_receipt_rejects_different_checkout_at_same_commit(self):
        from workbench import partial_implementation
        f = self.fixture
        state_path = f.task.parent / 'autopilot.json'
        state = cli.read(state_path); data = cli.read(f.task)
        settings = cli.read(f.task.parent / 'input-stage.json')['run_settings']
        state['clean_reverify_binding'] = partial_implementation.clean_reverify_binding(
            f.task, data, state, f.issue, settings)
        state['phase'] = 'verify'; cli.write(state_path, state)
        other = f.root / 'different-worktree'
        subprocess.run(['git', '-C', str(f.workspace), 'worktree', 'add', '-qb', 'other-checkout',
                        str(other), 'HEAD'], check=True)
        data['repo'] = str(other); cli.write(f.task, data)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'binding changed'):
            autopilot.run(f.task, f.issue, 'image', model=lambda *a, **k: self.fail('model called'),
                          check=lambda **k: self.fail('check called'))

    def test_clean_receipt_rejects_removed_evidence_but_allows_appended(self):
        from workbench import partial_implementation
        f = self.fixture
        state = cli.read(f.task.parent / 'autopilot.json'); data = cli.read(f.task)
        settings = cli.read(f.task.parent / 'input-stage.json')['run_settings']
        binding = partial_implementation.clean_reverify_binding(f.task, data, state, f.issue, settings)
        state['clean_reverify_binding'] = binding
        extra = dict(data['evidence'][-1]); extra['id'] = max(item['id'] for item in data['evidence']) + 1
        grown = {**data, 'evidence': [*data['evidence'], extra]}
        self.assertEqual(partial_implementation.clean_reverify_binding(f.task, grown, state, f.issue, settings), binding)
        reduced = {**data, 'evidence': data['evidence'][1:]}
        with self.assertRaisesRegex(autopilot.AutopilotError, 'removed or changed'):
            partial_implementation.clean_reverify_binding(f.task, reduced, state, f.issue, settings)
        state['phase'] = 'verify'; cli.write(f.task.parent / 'autopilot.json', state)
        cli.write(f.task, reduced)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'removed or changed'):
            autopilot.run(f.task, f.issue, 'image', model=lambda *a, **k: self.fail('model called'),
                          check=lambda **k: self.fail('check called'))


if __name__ == '__main__':
    unittest.main()
