import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, workflow
from test_runner import tiny_png


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        (self.repo / 'app.txt').write_text('before\n')
        for name in ('test-command', 'test', 'before', 'after'):
            (self.repo / name).write_text('#!/bin/sh\nexit 0\n')
        self.git('add', 'app.txt', 'test-command', 'test', 'before', 'after')
        self.git('commit', '-qm', 'base')

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_overlay_archives_changes_and_preserves_full_verification(self):
        import json
        import tarfile
        (self.repo / 'unchanged.txt').write_text('keep\n')
        (self.repo / 'literal-other').write_text('keep wildcard match\n')
        (self.repo / 'deleted.txt').write_text('remove\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'baseline payload')
        base = cli.sha(self.repo, 'HEAD')
        (self.repo / 'app.txt').write_text('after\n')
        (self.repo / 'new.txt').write_text('new\n')
        (self.repo / 'literal-*').write_text('literal name\n')
        (self.repo / 'deleted.txt').unlink()
        self.git('add', '-A')
        self.git('commit', '-qm', 'candidate')
        head = cli.sha(self.repo, 'HEAD')
        real_run = subprocess.run
        captured = {}
        def run(command, **kwargs):
            if command[:3] == ['docker', 'image', 'inspect']:
                return subprocess.CompletedProcess(command, 0, json.dumps([{'Config': {}}]), '')
            if command[:2] == ['docker', 'build']:
                with tarfile.open(fileobj=kwargs['stdin'], mode='r:') as archive:
                    captured['files'] = archive.getnames()
                    captured['recipe'] = archive.extractfile('Dockerfile').read().decode()
                    captured['app'] = archive.extractfile('source/app.txt').read()
                return subprocess.CompletedProcess(command, 0)
            return real_run(command, **kwargs)
        with patch.object(workflow, '_verified_image', return_value='sha256:' + 'a' * 64) as verify, \
                patch.object(workflow.subprocess, 'run', side_effect=run):
            workflow.build_source_image(self.repo, base, 'baseline', 30)
        self.assertIn('source/new.txt', captured['files'])
        self.assertIn('source/literal-*', captured['files'])
        self.assertNotIn('source/literal-other', captured['files'])
        self.assertNotIn('source/unchanged.txt', captured['files'])
        self.assertNotIn('source/deleted.txt', captured['files'])
        self.assertEqual(captured['app'], b'after\n')
        self.assertIn('/app/deleted.txt', captured['recipe'])
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(verify.call_args_list[0].kwargs['commit'], base)
        self.assertEqual(cli.sha(self.repo, 'HEAD'), head)

    def test_source_build_failure_retains_owner_only_tool_output(self):
        import json
        import stat
        real_run = subprocess.run
        def run(command, **kwargs):
            if command[:3] == ['docker', 'image', 'inspect']:
                return subprocess.CompletedProcess(command, 0, json.dumps([{'Config': {}}]), '')
            if command[:2] == ['docker', 'build']:
                kwargs['stdout'].write(b'diagnostic: local build failed\n')
                kwargs['stdout'].flush()
                raise subprocess.CalledProcessError(42, command)
            return real_run(command, **kwargs)
        base = cli.sha(self.repo, 'HEAD')
        with patch.object(workflow, '_verified_image', return_value='sha256:' + 'a' * 64), \
                patch.object(workflow.subprocess, 'run', side_effect=run):
            with self.assertRaises(workflow.WorkflowError) as caught:
                workflow.build_source_image(self.repo, base, 'baseline', 30)
        self.assertIn('exit 42', str(caught.exception))
        log = Path(str(caught.exception).split('output saved at ', 1)[1])
        try:
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
            self.assertEqual(log.read_text(), 'diagnostic: local build failed\n')
        finally:
            log.unlink()

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def prepare(self):
        return workflow.prepare(task_id='issue-1', repo=self.repo,
                                tasks_root=self.root / 'tasks', workspaces_root=self.root / 'workspaces',
                                source='test issue', kind='bug', surface='non-ui',
                                iterations=3, seconds=120, implementer='implementer')

    def criterion(self, task):
        record = cli.read(task)
        cli.criterion_add(argparse.Namespace(id='a', text='repair behavior', reason='issue'), record)
        cli.write(Path(task), record)

    def test_prepare_creates_independent_worktree_without_touching_original(self):
        result = self.prepare()
        self.assertEqual(Path(result['workspace'], 'app.txt').read_text(), 'before\n')
        self.assertEqual(cli.read(result['task'])['base_commit'], result['base_commit'])
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.repo), 'status', '--porcelain']), b'')
        with self.assertRaises(workflow.WorkflowError):
            self.prepare()

    def test_prepare_records_explicit_skip_policy_and_reason(self):
        result = workflow.prepare(task_id='policy', repo=self.repo,
            tasks_root=self.root / 'tasks', workspaces_root=self.root / 'workspaces',
            source='test issue', kind='bug', surface='non-ui', iterations=3, seconds=120,
            implementer='implementer', required_ci_workflows=['CI'],
            ci_skip_policy='openclaw-selected-v1', ci_skip_reason='prove unselected CI lanes')
        data = cli.read(result['task'])
        self.assertEqual(data['ci_skip_policy'], 'openclaw-selected-v1')
        event = data['events'][-1]
        self.assertEqual(event['reason'], 'prove unselected CI lanes')
        self.assertEqual(event['required_ci_workflows'], ['CI'])
        self.assertEqual(event['mode'], 'creation')

    def test_prepare_rejects_skip_policy_without_required_ci_and_reason(self):
        with self.assertRaises(workflow.WorkflowError):
            workflow.prepare(task_id='policy', repo=self.repo,
                tasks_root=self.root / 'tasks', workspaces_root=self.root / 'workspaces',
                source='test issue', kind='bug', surface='non-ui', iterations=3, seconds=120,
                implementer='implementer', ci_skip_policy='openclaw-selected-v1')
        self.assertFalse((self.root / 'tasks' / 'policy').exists())

    def test_prepare_persists_required_workflows_and_rejects_invalid_names(self):
        options = dict(task_id='ci', repo=self.repo, tasks_root=self.root / 'tasks',
                       workspaces_root=self.root / 'workspaces', source='test', kind='bug',
                       surface='non-ui', iterations=1, seconds=60, implementer='tester')
        with self.assertRaisesRegex(workflow.WorkflowError, 'required_ci_workflows'):
            workflow.prepare(**options, required_ci_workflows=['CI', 'CI'])
        self.assertFalse((self.root / 'workspaces/ci').exists())
        result = workflow.prepare(**options, required_ci_workflows=['CI', 'Security Review'])
        self.assertEqual(cli.read(result['task'])['required_ci_workflows'], ['CI', 'Security Review'])

    def test_prepare_cli_collects_repeated_required_workflows(self):
        args = ['prepare', '--id', 'ci', '--repo', str(self.repo), '--tasks-root', str(self.root / 'tasks'),
                '--workspaces-root', str(self.root / 'workspaces'), '--source', 'test', '--implementer', 'tester',
                '--kind', 'bug', '--surface', 'non-ui', '--iterations', '1', '--seconds', '60',
                '--required-ci-workflow', 'CI', '--required-ci-workflow', 'Security Review']
        with patch.object(workflow, 'prepare', return_value={'task': 'task.json'}) as prepare:
            workflow.main(args)
        self.assertEqual(prepare.call_args.kwargs['required_ci_workflows'], ['CI', 'Security Review'])

    def test_input_preparation_time_is_included_in_task_budget(self):
        began = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        result = workflow.prepare(task_id='budget', repo=self.repo, tasks_root=self.root / 'tasks',
                                  workspaces_root=self.root / 'workspaces', source='test', kind='bug',
                                  surface='non-ui', iterations=1, seconds=60, implementer='tester',
                                  started_at=began)
        self.assertEqual(cli.read(result['task'])['started_at'], began)
        with self.assertRaisesRegex(workflow.WorkflowError, 'time limit'):
            workflow.prepare(task_id='expired', repo=self.repo, tasks_root=self.root / 'tasks',
                             workspaces_root=self.root / 'workspaces', source='test', kind='bug',
                             surface='non-ui', iterations=1, seconds=1, implementer='tester',
                             started_at=began)
        self.assertFalse((self.root / 'workspaces/expired').exists())

    def test_refuses_dirty_source_and_invalid_id(self):
        (self.repo / 'app.txt').write_text('user change\n')
        with self.assertRaises(workflow.WorkflowError):
            self.prepare()
        self.assertEqual((self.repo / 'app.txt').read_text(), 'user change\n')
        with self.assertRaises(workflow.WorkflowError):
            workflow.prepare(task_id='../bad', repo=self.repo, tasks_root=self.root,
                             workspaces_root=self.root, source='issue', kind='bug',
                             surface='ui', iterations=1, seconds=1, implementer='a')

    def test_reproduction_requires_real_failure_marker_and_captures_report(self):
        task = self.prepare()['task']
        self.criterion(task)
        def failing_runner(workspace, image, command, timeout):
            return {'exit_code': 1, 'stdout': '', 'stderr': 'database is locked',
                    'cleanup_confirmed': True, 'timed_out': False}
        result = workflow.run_check(task=task, kind='reproduction', image='local-image',
                                    command=['/workspace/test-command'], timeout=10, expect_exit=1,
                                    marker='database is locked', actor='runner',
                                    description='reproduce database lock', runner=failing_runner)
        self.assertTrue(Path(result['report']).is_file())
        self.assertEqual(cli.read(task)['evidence'][0]['kind'], 'reproduction')
        with self.assertRaises(workflow.WorkflowError):
            workflow.run_check(task=task, kind='reproduction', image='local-image',
                               command=['/workspace/test-command'], timeout=10, expect_exit=1,
                               marker='unexpected result', actor='runner',
                               description='bad reproduction', runner=failing_runner)
        self.assertEqual(len(cli.read(task)['evidence']), 1)
        self.assertEqual(len(list(Path(task).parent.glob('reproduction-*.json'))), 2)

    def test_nonisolated_or_unexpected_results_cannot_be_recorded(self):
        task = self.prepare()['task']
        self.criterion(task)
        def bad_runner(workspace, image, command, timeout):
            return {'exit_code': 0, 'stdout': 'looks fine', 'cleanup_confirmed': False}
        with self.assertRaises(workflow.WorkflowError):
            workflow.run_check(task=task, kind='test-result', image='image', command=['/workspace/test'],
                               timeout=10, expect_exit=0, marker='', actor='runner',
                               description='test', runner=bad_runner)
        self.assertEqual(cli.read(task)['evidence'], [])

    def test_cannot_treat_unobserved_failure_as_bug_reproduction(self):
        task = self.prepare()['task']
        self.criterion(task)
        with self.assertRaises(workflow.WorkflowError):
            workflow.run_check(task=task, kind='reproduction', image='image', command=['/workspace/test'],
                               timeout=10, expect_exit=0, marker='', actor='runner',
                               description='test', runner=lambda *a: {})

    def test_rejects_unbound_image_script_by_default(self):
        task = self.prepare()['task']
        self.criterion(task)
        for command in (['node', '-e', 'import("/app/index.js")'], ['node', '/app/index.js']):
            with self.assertRaisesRegex(workflow.WorkflowError, 'workspace source'):
                workflow.run_check(task=task, kind='test-result', image='image', command=command,
                                   timeout=10, expect_exit=0, marker='', actor='runner',
                                   description='test', runner=lambda *a: {})

    def test_rejects_ignored_untracked_workspace_entrypoint(self):
        result = self.prepare()
        task = result['task']
        self.criterion(task)
        workspace = Path(result['workspace'])
        exclude = Path(subprocess.check_output(['git', '-C', str(workspace), 'rev-parse', '--git-path', 'info/exclude'], text=True).strip())
        if not exclude.is_absolute():
            exclude = workspace / exclude
        with exclude.open('a') as f:
            f.write('\nignored-entry\n')
        (workspace / 'ignored-entry').write_text('#!/bin/sh\necho fake\n')
        with self.assertRaisesRegex(workflow.WorkflowError, 'tracked'):
            workflow.run_check(task=task, kind='test-result', image='image', command=['/workspace/ignored-entry'],
                               timeout=10, expect_exit=0, marker='', actor='runner', description='test', runner=lambda *a: {})

    def test_image_verification_pins_execution_to_image_id(self):
        task = self.prepare()['task']
        self.criterion(task)
        image_id = 'sha256:' + 'a' * 64
        executions = []
        def runner(workspace, image, command, timeout, **options):
            self.assertTrue(verify.called)
            executions.append((image, options))
            return {'exit_code': 0, 'stdout': 'ok', 'stderr': '', 'cleanup_confirmed': True}
        with patch.object(workflow, '_verified_image', return_value=image_id) as verify:
            for memory in (2048, 7168):
                with self.subTest(memory=memory):
                    result = workflow.run_check(task=task, kind='test-result', image='mutable:tag',
                        command=['node', '/app/test.js'], timeout=10, expect_exit=0,
                        marker='ok', actor='runner', description='test', runner=runner,
                        memory_mb=memory, source_mode='image')
        self.assertEqual(executions, [(image_id, {'source_root': '/app'}),
                                     (image_id, {'source_root': '/app', 'memory_mb': 7168})])
        report = __import__('json').loads(Path(result['report']).read_text())
        self.assertEqual(report['verified_image_id'], image_id)

    def test_dirty_source_never_attested(self):
        result = self.prepare()
        task = result['task']
        self.criterion(task)
        Path(result['workspace'], 'app.txt').write_text('modified but uncommitted')
        with self.assertRaisesRegex(workflow.WorkflowError, 'clean'):
            workflow.run_check(task=task, kind='test-result', image='image', command=['/workspace/test'],
                               timeout=10, expect_exit=0, marker='', actor='runner',
                               description='test', runner=lambda *a: {})

    def test_screenshot_evidence_binds_image_and_execution_report(self):
        result = self.prepare()
        task = result['task']
        self.criterion(task)
        image = tiny_png()
        def runner(workspace, image_name, command, timeout, *, memory_mb, screenshot_output):
            Path(screenshot_output).write_bytes(image)
            return {'exit_code': 0, 'stdout': 'captured', 'stderr': '', 'cleanup_confirmed': True,
                    'screenshot_path': str(Path(screenshot_output).resolve()),
                    'screenshot_sha256': hashlib.sha256(image).hexdigest()}
        check = workflow.run_check(task=task, kind='screenshot', image='image', command=['/workspace/test'],
                                   timeout=10, expect_exit=0, marker='captured', actor='verifier',
                                   description='sandbox capture', runner=runner)
        evidence = cli.read(task)['evidence'][0]
        self.assertEqual(Path(evidence['path']).read_bytes(), image)
        self.assertEqual(evidence['execution_report'], check['report'])
        Path(check['report']).write_text('changed')
        _, issues = cli.gate(cli.read(task), 'HEAD')
        self.assertTrue(any('execution report changed' in issue for issue in issues))

    def test_composed_bug_evidence_gate_uses_baseline_and_candidate(self):
        result = self.prepare()
        task, workspace = result['task'], Path(result['workspace'])
        self.criterion(task)
        def runner(path, image, command, timeout):
            return {'exit_code': 1 if command[0] == '/workspace/before' else 0,
                    'stdout': 'observed-bug' if command[0] == '/workspace/before' else 'fixed',
                    'stderr': '', 'cleanup_confirmed': True, 'timed_out': False}
        workflow.run_check(task=task, kind='reproduction', image='image', command=['/workspace/before'],
                           timeout=10, expect_exit=1, marker='observed-bug', actor='runner',
                           description='reproduce issue', runner=runner)
        (workspace / 'app.txt').write_text('after\n')
        subprocess.run(['git', '-C', str(workspace), 'add', 'app.txt'], check=True)
        subprocess.run(['git', '-C', str(workspace), 'commit', '-qm', 'fix'], check=True)
        for kind in ('resolution', 'test-result', 'runtime-log'):
            workflow.run_check(task=task, kind=kind, image='image', command=['/workspace/after'],
                               timeout=10, expect_exit=0, marker='fixed', actor='runner',
                               description='candidate test', runner=runner)
        review = Path(task).parent / 'review.txt'
        review.write_text('Independent verifier report; tested using fake runner.\n')
        record = cli.read(task)
        cli.evidence_add(argparse.Namespace(file=str(review), kind='independent-review',
                                           commit='HEAD', actor='verifier', description='review',
                                           criterion=['a']), record)
        cli.write(Path(task), record)
        self.assertEqual(cli.gate(cli.read(task), 'HEAD')[1], [])


if __name__ == '__main__':
    unittest.main()
