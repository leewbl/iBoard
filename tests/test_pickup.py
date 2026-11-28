from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from workbench import cli, pickup, start, workflow


class PickupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        repo = root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        (repo / 'app.js').write_text('console.log("ok")\n')
        cli.git(repo, 'add', 'app.js')
        cli.git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
        prepared = workflow.prepare(task_id='pickup', repo=repo, tasks_root=root / 'tasks',
                                    workspaces_root=root / 'workspaces', source='assigned task',
                                    kind='feature', surface='non-ui', iterations=2, seconds=600,
                                    implementer='engineer')
        self.task = Path(prepared['task'])
        self.data = cli.read(self.task)
        self.workspace = Path(prepared['workspace'])
        self.state = self.task.with_name('autopilot.json')
        self.input = self.task.with_name('input-stage.json')

    def test_old_running_record_reports_exact_completed_publication_and_current_pr(self):
        self.data['publish_only'] = True
        cli.write(self.task, self.data)
        head = self.data['base_commit']
        cli.write(self.state, {'schema':cli.SCHEMA, 'phase':'complete', 'candidate_commit':head,
                               'published_pr':{'repo':'alice/fork', 'pr':5, 'head':head}})

        class GitHub:
            def pull(_, repo, number):
                assert (repo, number) == ('alice/fork', 5)
                return {'number':5, 'headRefOid':head, 'state':'OPEN',
                        'statusCheckRollup':[{'status':'COMPLETED', 'conclusion':'SUCCESS'},
                                             {'status':'IN_PROGRESS'}]}

        before = [path.read_bytes() for path in (self.task, self.state)]
        result = pickup.inspect(self.task, check_pr=True, github=GitHub())
        self.assertEqual(result['recorded_status'], 'running')
        self.assertEqual(result['execution'], 'complete')
        self.assertEqual(result['outcome'], 'publication-recorded')
        self.assertEqual(result['pr']['state'], 'OPEN')
        self.assertEqual(result['pr']['checks'], {'success':1, 'pending':1})
        self.assertEqual(before, [path.read_bytes() for path in (self.task, self.state)])

        class StaleGitHub(GitHub):
            def pull(self, repo, number):
                return {**super().pull(repo, number), 'headRefOid':'0' * 40}
        stale = pickup.inspect(self.task, check_pr=True, github=StaleGitHub())
        self.assertEqual(stale['next'], 'reconcile-pr-with-recorded-candidate')
        self.data['status'] = 'paused'
        cli.write(self.task, self.data)
        with self.assertRaisesRegex(start.StartError, 'completed task cannot be resumed'):
            start.resume_task(self.task, run=lambda *a, **kw: self.fail('completed task executed'))
        self.assertEqual(cli.read(self.task)['status'], 'paused')

    def test_input_change_and_interrupted_edit_do_not_offer_a_resume(self):
        self.data['status'] = 'paused'
        cli.write(self.task, self.data)
        request = self.task.with_name('issue.txt')
        base = self.task.with_name('input-base.txt')
        for path in (request, base):
            path.write_text('original request')
        revision = cli.digest(request)
        cli.write(self.input, {'schema':cli.SCHEMA, 'phase':'ready', 'input_digest':None,
                               'base_digest':revision, 'request_digest':revision,
                               'run_settings':{'image':'test', 'pr_repo':None, 'source_mode':'workspace',
                                               'candidate_image':None, 'build_source_image':False, 'memory_mb':2048}})
        ready = pickup.inspect(self.task)
        self.assertEqual(ready['next'], 'resume-from-plan')
        cli.write(self.state, {'schema':cli.SCHEMA, 'phase':'repairing', 'issue_digest':revision,
                               'input_digest':None, 'candidate_commit':self.data['base_commit']})
        request.write_text('changed request')
        (self.workspace / 'app.js').write_text('partial edit')
        result = pickup.inspect(self.task)
        self.assertEqual(result['execution'], 'needs-inspection')
        self.assertTrue(result['worktree']['dirty'])
        self.assertTrue(any('prepared task input changed' in item for item in result['blockers']))
        self.assertTrue(any('repairing was interrupted' in item for item in result['blockers']))

    def test_external_process_holding_lock_is_reported_as_active(self):
        worker = ('from workbench.task_lock import task_lock\n'
                  'import sys\n'
                  'with task_lock(sys.argv[1]):\n'
                  ' print("acquired", flush=True)\n'
                  ' sys.stdin.readline()')
        with subprocess.Popen([sys.executable, '-c', worker, str(self.task)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True) as process:
            try:
                self.assertEqual(process.stdout.readline().strip(), 'acquired')
                report = pickup.inspect(self.task)
                self.assertEqual(report['execution'], 'active')
                self.assertEqual(report['next'], 'wait-for-current-operation')
            finally:
                process.communicate(input='release\n', timeout=10)
        self.assertEqual(pickup.inspect(self.task)['lock'], 'idle')


if __name__ == '__main__':
    unittest.main()
