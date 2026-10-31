import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workbench import autopilot, cli, start, delivery, publish, workflow


class StartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.issue = self.root / 'issue.txt'
        self.issue.write_text('Explicit failure and acceptance details.\n')
        self.kwargs = dict(task_id='case', repo=self.root, issue_file=self.issue, source='user report',
                           kind='bug', surface='non-ui', iterations=2, seconds=120, image='local-image',
                           tasks_root=self.root / 'tasks', workspaces_root=self.root / 'workspaces')
        self.called = []

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, **kwargs):
        self.called.append(('prepare', kwargs))
        task = self.root / 'tasks' / 'case' / 'task.json'
        task.parent.mkdir(parents=True)
        cli.write(task, {'schema': cli.SCHEMA, 'status': 'running', 'started_at': cli.now(),
                         'limits': {'seconds': 120, 'iterations': 2}, 'iterations': 0, 'events': []})
        return {'task': str(task), 'workspace': str(self.root / 'workspace'), 'base_commit': 'abc'}

    def drive(self, task, request, image, **kwargs):
        self.called.append(('run', request.read_text(), image, kwargs))
        return {'phase': 'delivery'}

    def test_invalid_memory_is_rejected_before_task_preparation(self):
        for memory in (True, False, 127, 8193, 7168.0, '7168', None):
            with self.subTest(memory=memory):
                with self.assertRaises(start.StartError):
                    start.start(**self.kwargs, memory_mb=memory, prepare=self.prepare, run=self.drive)
                self.assertEqual(self.called, [])

    @patch('workbench.start.shutil.which', return_value='/usr/bin/tool')
    @patch('workbench.start.subprocess.run', return_value=subprocess.CompletedProcess([], 0))
    def test_explicit_seven_gib_is_retained_for_resume(self, process, which):
        def fail(*args, **kwargs):
            raise autopilot.AutopilotError('temporary failure')
        with self.assertRaises(start.StartError):
            start.start(**self.kwargs, memory_mb=7168, prepare=self.prepare, run=fail)
        task = self.root / 'tasks/case/task.json'
        self.assertEqual(cli.read(task.parent / 'input-stage.json')['run_settings']['memory_mb'], 7168)
        start.resume_task(task, run=self.drive)
        self.assertEqual(self.called[-1][3]['memory_mb'], 7168)

    @patch('workbench.start.shutil.which', return_value='/usr/bin/tool')
    @patch('workbench.start.subprocess.run', return_value=subprocess.CompletedProcess([], 0))
    def test_snapshots_request_and_runs_on_prepared_task(self, process, which):
        result = start.start(**self.kwargs, pr_repo='alice/fork', required_ci_workflows=['CI', 'Security Review'],
                             prepare=self.prepare, run=self.drive)
        self.assertEqual(result['phase'], 'delivery')
        self.assertEqual(self.called[0][1]['source'], 'user report')
        self.assertEqual(self.called[0][1]['required_ci_workflows'], ['CI', 'Security Review'])
        self.assertEqual(self.called[1][1], self.issue.read_text())
        self.assertEqual(self.called[1][3]['pr_repo'], 'alice/fork')
        self.assertEqual((self.root / 'tasks/case/issue.txt').stat().st_mode & 0o777, 0o600)

    @patch('workbench.start.shutil.which', return_value='/usr/bin/tool')
    @patch('workbench.start.subprocess.run', return_value=subprocess.CompletedProcess([], 0))
    def test_failure_preserves_task_path(self, process, which):
        def fail(*args, **kwargs):
            raise autopilot.AutopilotError('verification failed')
        with self.assertRaisesRegex(start.StartError, 'task retained at .*task.json'):
            start.start(**self.kwargs, prepare=self.prepare, run=fail)
        self.assertTrue((self.root / 'tasks/case/issue.txt').exists())

    @patch('workbench.start.shutil.which', return_value='/usr/bin/tool')
    @patch('workbench.start.subprocess.run', return_value=subprocess.CompletedProcess([], 0))
    def test_plain_task_resumes_with_saved_execution_settings(self, process, which):
        def fail(*args, **kwargs):
            raise autopilot.AutopilotError('temporary failure')
        with self.assertRaises(start.StartError):
            start.start(**self.kwargs, pr_repo='alice/fork', required_ci_workflows=['CI'],
                        memory_mb=3072, prepare=self.prepare, run=fail)
        task = self.root / 'tasks/case/task.json'
        result = start.resume_task(task, run=self.drive)
        self.assertEqual(result['phase'], 'delivery')
        self.assertEqual(self.called[-1][2], 'local-image')
        self.assertEqual(self.called[-1][3]['pr_repo'], 'alice/fork')
        self.assertEqual(self.called[-1][3]['memory_mb'], 3072)

    @patch('workbench.start.shutil.which', return_value='/usr/bin/tool')
    @patch('workbench.start.subprocess.run', return_value=subprocess.CompletedProcess([], 0))
    def test_plain_task_resume_rejects_changed_input(self, process, which):
        def fail(*args, **kwargs):
            raise autopilot.AutopilotError('temporary failure')
        with self.assertRaises(start.StartError):
            start.start(**self.kwargs, prepare=self.prepare, run=fail)
        task = self.root / 'tasks/case/task.json'
        (task.parent / 'issue.txt').write_text('altered request')
        with self.assertRaisesRegex(start.StartError, 'prepared task input changed'):
            start.resume_task(task, run=self.drive)
        self.assertEqual(cli.read(task)['status'], 'paused')

    @patch('workbench.start.shutil.which', return_value='/usr/bin/tool')
    @patch('workbench.start.subprocess.run', return_value=subprocess.CompletedProcess([], 0))
    def test_supplied_log_is_snapshotted_and_included_as_untrusted_data(self, process, which):
        log = self.root / 'failure.log'
        log.write_text('request failed: 503\n')
        start.start(**self.kwargs, log_files=[log], prepare=self.prepare, run=self.drive)
        self.assertIn('request failed: 503', self.called[1][1])
        self.assertIn('UNTRUSTED DATA', self.called[1][1])
        inputs = self.root / 'tasks/case/inputs'
        self.assertEqual((inputs / 'log-1.txt').read_bytes(), log.read_bytes())
        item = json.loads((inputs / 'manifest.json').read_text())['logs'][0]
        self.assertEqual(item['sha256'], hashlib.sha256(log.read_bytes()).hexdigest())
        self.assertEqual((inputs / 'log-1.txt').stat().st_mode & 0o777, 0o600)

    def test_invalid_log_is_rejected_before_workspace_creation(self):
        log = self.root / 'binary.log'
        log.write_bytes(b'\xff')
        with self.assertRaisesRegex(start.StartError, 'UTF-8'):
            start.start(**self.kwargs, log_files=[log], prepare=self.prepare, run=self.drive)
        self.assertEqual(self.called, [])

    def test_delivery_requires_workflows_before_preparing_workspace(self):
        with self.assertRaisesRegex(start.StartError, 'required_ci_workflows'):
            start.start(**self.kwargs, pr_repo='alice/fork', prepare=self.prepare, run=self.drive)
        self.assertEqual(self.called, [])

    def test_cli_collects_repeated_required_workflow_names(self):
        args = ['--id', 'case', '--repo', str(self.root), '--issue-file', str(self.issue),
                '--source', 'test', '--kind', 'bug', '--surface', 'non-ui', '--iterations', '1',
                '--seconds', '120', '--image', 'local-image', '--pr-repo', 'alice/fork',
                '--required-ci-workflow', 'CI', '--required-ci-workflow', 'Security Review']
        with patch.object(start, 'start', return_value={'phase': 'complete'}) as run:
            start.main(args)
        self.assertEqual(run.call_args.kwargs['required_ci_workflows'], ['CI', 'Security Review'])

    def test_cli_collects_explicit_skip_policy_and_reason(self):
        args = ['--id', 'case', '--repo', str(self.root), '--issue-file', str(self.issue),
                '--source', 'test', '--kind', 'bug', '--surface', 'non-ui', '--iterations', '1',
                '--seconds', '120', '--image', 'local-image', '--pr-repo', 'alice/fork',
                '--required-ci-workflow', 'CI', '--ci-skip-policy', 'openclaw-selected-v1',
                '--ci-skip-reason', 'prove unselected lanes']
        with patch.object(start, 'start', return_value={'phase': 'complete'}) as run:
            start.main(args)
        self.assertEqual(run.call_args.kwargs['ci_skip_policy'], 'openclaw-selected-v1')
        self.assertEqual(run.call_args.kwargs['ci_skip_reason'], 'prove unselected lanes')

    @patch('workbench.start.shutil.which', return_value='/usr/bin/tool')
    def test_input_retrieval_consumes_task_budget(self, which):
        initial = datetime(2026, 1, 1, tzinfo=timezone.utc)
        class Clock:
            count = 0
            @classmethod
            def now(cls, tz):
                cls.count += 1
                return initial if cls.count <= 2 else initial + timedelta(seconds=3)
        options = {**self.kwargs, 'issue_file': None, 'issue_url': 'https://github.com/alice/repo/issues/1',
                   'source': None, 'seconds': 2}
        with patch('workbench.start.datetime', Clock):
            with self.assertRaisesRegex(start.StartError, 'time limit'):
                start.start(**options, fetch=lambda url, timeout: 'issue', prepare=self.prepare, run=self.drive)
        self.assertEqual(self.called, [])

    @patch('workbench.start.shutil.which', return_value='/usr/bin/tool')
    @patch('workbench.start.subprocess.run', return_value=subprocess.CompletedProcess([], 0))
    def test_screenshot_is_snapshotted_and_described_as_untrusted(self, process, which):
        screenshot = self.root / 'error.png'
        data = b'\x89PNG\r\n\x1a\nexample'
        screenshot.write_bytes(data)
        seen = []
        def describe(content, timeout):
            seen.append(content)
            return 'Visible error: connection refused'
        start.start(**self.kwargs, screenshot_files=[screenshot], prepare=self.prepare, run=self.drive, vision=describe)
        self.assertEqual(seen, [data])
        self.assertIn('connection refused', self.called[1][1])
        self.assertIn('NOT VERIFICATION EVIDENCE', self.called[1][1])
        inputs = self.root / 'tasks/case/inputs'
        self.assertEqual((inputs / 'screenshot-1.png').read_bytes(), data)
        manifest = json.loads((inputs / 'manifest.json').read_text())
        self.assertEqual(manifest['screenshots'][0]['sha256'], hashlib.sha256(data).hexdigest())

    @patch('workbench.start.shutil.which', return_value='/usr/bin/tool')
    @patch('workbench.start.subprocess.run', return_value=subprocess.CompletedProcess([], 0))
    def test_resume_after_vision_failure_reuses_prior_description(self, process, which):
        first = self.root / 'first.png'; first.write_bytes(b'\x89PNG\r\n\x1a\nfirst')
        second = self.root / 'second.png'; second.write_bytes(b'\x89PNG\r\n\x1a\nsecond')
        calls = []
        def failing(content, timeout):
            calls.append(content)
            if len(calls) == 2:
                raise RuntimeError('vision unavailable')
            return 'first observation'
        with self.assertRaisesRegex(start.StartError, 'vision unavailable'):
            start.start(**self.kwargs, screenshot_files=[first, second], prepare=self.prepare,
                        run=self.drive, vision=failing)
        task = self.root / 'tasks/case/task.json'
        self.assertEqual(cli.read(task)['status'], 'paused')
        self.assertEqual(cli.read(task.parent / 'input-stage.json')['descriptions'], ['first observation'])
        retry = []
        result = start.resume_input(task, run=self.drive,
                                    vision=lambda data, timeout: retry.append(data) or 'second observation')
        self.assertEqual(result['phase'], 'delivery')
        self.assertEqual(retry, [second.read_bytes()])
        self.assertIn('first observation', self.called[-1][1])
        self.assertIn('second observation', self.called[-1][1])

    @patch('workbench.start.shutil.which', return_value='/usr/bin/tool')
    @patch('workbench.start.subprocess.run', return_value=subprocess.CompletedProcess([], 0))
    def test_resume_rejects_altered_screenshot(self, process, which):
        screenshot = self.root / 'image.png'; screenshot.write_bytes(b'\x89PNG\r\n\x1a\noriginal')
        def fail(*args):
            raise RuntimeError('vision unavailable')
        with self.assertRaises(start.StartError):
            start.start(**self.kwargs, screenshot_files=[screenshot], prepare=self.prepare, run=self.drive, vision=fail)
        task = self.root / 'tasks/case/task.json'
        (task.parent / 'inputs/screenshot-1.png').write_bytes(b'changed')
        with self.assertRaisesRegex(start.StartError, 'snapshot changed'):
            start.resume_input(task, run=self.drive, vision=lambda *args: 'description')
        self.assertEqual(cli.read(task)['status'], 'paused')

    def test_legacy_resume_missing_stage_preserves_paused_task(self):
        task = self.root / 'tasks' / 'case' / 'task.json'
        task.parent.mkdir(parents=True)
        original = {'schema': cli.SCHEMA, 'status': 'paused', 'started_at': cli.now(),
                    'limits': {'seconds': 120, 'iterations': 2}, 'iterations': 0, 'events': []}
        cli.write(task, original)
        with self.assertRaisesRegex(start.StartError, 'input-stage.json is missing or unsafe'):
            start.resume_task(task, run=self.drive)
        self.assertEqual(cli.read(task), original)
        self.assertFalse(self.called)

    def test_resume_does_not_override_expired_budget(self):
        task = self.root / 'expired.json'
        cli.write(task, {'schema': cli.SCHEMA, 'status': 'paused', 'started_at': '2000-01-01T00:00:00+00:00',
                         'limits': {'seconds': 60, 'iterations': 1}, 'iterations': 0, 'events': []})
        with self.assertRaisesRegex(start.StartError, 'budget exceeded'):
            start.resume_input(task, run=self.drive, vision=lambda *args: 'description')
        self.assertEqual(cli.read(task)['status'], 'paused')

    def test_resume_does_not_pause_running_task(self):
        task = self.root / 'running.json'
        cli.write(task, {'schema': cli.SCHEMA, 'status': 'running', 'events': []})
        with self.assertRaisesRegex(start.StartError, 'only paused tasks'):
            start.resume_task(task, run=self.drive)
        self.assertEqual(cli.read(task)['status'], 'running')
        self.assertFalse(self.called)

    def test_concurrent_process_cannot_resume_or_run_same_task(self):
        task = self.root / 'locked.json'
        cli.write(task, {'schema': cli.SCHEMA, 'status': 'paused', 'events': []})
        code = ('import sys; from workbench.task_lock import task_lock; '
                'lock = task_lock(sys.argv[1]); lock.__enter__(); '
                'print("locked", flush=True); sys.stdin.readline(); lock.__exit__(None, None, None)')
        process = subprocess.Popen([sys.executable, '-c', code, str(task)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'locked')
            with self.assertRaisesRegex(start.StartError, 'already being operated on'):
                start.resume_task(task, run=self.drive)
            with self.assertRaisesRegex(OSError, 'already being operated on'):
                autopilot.run(task, self.issue, 'image')
            with self.assertRaisesRegex(OSError, 'already being operated on'):
                publish.publish(task, 'alice/project')
            with self.assertRaisesRegex(OSError, 'already being operated on'):
                delivery.guarded_merge(task, 'alice/project', 1)
            with self.assertRaisesRegex(OSError, 'already being operated on'):
                workflow.run_check(task=task, kind='test-result', image='image', command=['true'],
                                   timeout=1, expect_exit=0, marker='', actor='test', description='test')
            result = subprocess.run([sys.executable, str(Path(cli.__file__)), 'pause', '--task', str(task),
                                     '--reason', 'test'], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertIn('already being operated on', result.stderr)
            self.assertEqual(cli.read(task)['status'], 'paused')
            self.assertFalse(self.called)
        finally:
            process.communicate('\n', timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_invalid_input_is_rejected_before_creating_workspace(self):
        self.kwargs['surface'] = 'unknown'
        with self.assertRaisesRegex(start.StartError, 'surface'):
            start.start(**self.kwargs, prepare=self.prepare, run=self.drive)
        self.assertEqual(self.called, [])

    @patch('workbench.start.subprocess.run')
    def test_github_issue_fetch_is_explicit_and_rejects_pull_requests(self, process):
        process.return_value = subprocess.CompletedProcess([], 0, stdout=json.dumps(
            {'number': 4, 'title': 'Issue', 'body': 'Description'}), stderr='')
        result = start.read_issue_url('https://github.com/alice/project/issues/4')
        self.assertIn('Description', result)
        process.return_value.stdout = json.dumps({'number': 4, 'pull_request': {}, 'title': 'PR'})
        with self.assertRaisesRegex(start.StartError, 'does not identify an issue'):
            start.read_issue_url('https://github.com/alice/project/issues/4')
        with self.assertRaises(start.StartError):
            start.read_issue_url('https://github.com.evil.test/alice/project/issues/4')
