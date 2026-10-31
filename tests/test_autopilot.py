import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from test_runner import tiny_png

from workbench import autopilot, cli, delivery, workflow


class CompletedCI:
    def _call(self, *args):
        if args[0] == 'api':
            return {'pull_requests': [{'number': 1, 'head': {'sha': 'a' * 40},
                                      'base': {'ref': 'main', 'sha': self.base}}]}
        return [{'databaseId': 123, 'workflowName': 'CI', 'headSha': 'a' * 40, 'headBranch': 'workbench/case',
                 'status': 'completed', 'conclusion': 'success', 'event': 'pull_request'}]


class AutopilotTests(unittest.TestCase):
    def test_model_json_can_be_a_single_fenced_block_after_explanation(self):
        result = autopilot._result('Evidence follows.\n```json\n{"status":"ready","verdict":"PASS"}\n```')
        self.assertEqual(result['verdict'], 'PASS')
        result = autopilot._result('Inspected the evidence.\n\n{"status":"ready","verdict":"PASS"}\n')
        self.assertEqual(result['verdict'], 'PASS')
        with self.assertRaisesRegex(autopilot.AutopilotError, 'ambiguous'):
            autopilot._result('```json\n{"status":"ready"}\n```\n```json\n{"status":"ready"}\n```')
        with self.assertRaisesRegex(autopilot.AutopilotError, 'valid JSON'):
            autopilot._result('```json\nnot-json\n```')
        with self.assertRaisesRegex(autopilot.AutopilotError, 'valid JSON'):
            autopilot._result('Prefix\n{"status":"ready"}\ntrailing explanation')

    def test_review_requires_each_criterion_and_valid_evidence_references(self):
        criteria = [{'id': 'a'}, {'id': 'b'}]
        valid = {'assessments': [
            {'id': 'a', 'verdict': 'PASS', 'reason': 'Seen in run', 'evidence': ['R1']},
            {'id': 'b', 'verdict': 'PASS', 'reason': 'Seen in screenshot', 'evidence': ['R2']}]}
        self.assertEqual(autopilot._validate_assessments(valid, criteria, {'R1', 'R2'}, screenshot_id='R2'),
                         valid['assessments'])
        invalid = {'assessments': [valid['assessments'][0], {**valid['assessments'][1], 'evidence': ['R99']}]}
        with self.assertRaises(autopilot.AutopilotError):
            autopilot._validate_assessments(invalid, criteria, {'R1', 'R2'}, screenshot_id='R2')
        with self.assertRaises(autopilot.AutopilotError):
            autopilot._validate_assessments({'assessments': valid['assessments'][:1]}, criteria, {'R1', 'R2'})
        diff_only = {'assessments': [valid['assessments'][0], {**valid['assessments'][1], 'evidence': ['DIFF']}]}
        self.assertEqual(autopilot._validate_assessments(diff_only, criteria, {'R1', 'R2', 'DIFF'}), diff_only['assessments'])
        all_diff = {'assessments': [{**item, 'evidence': ['DIFF']} for item in valid['assessments']]}
        with self.assertRaises(autopilot.AutopilotError):
            autopilot._validate_assessments(all_diff, criteria, {'R1', 'R2', 'DIFF'})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'test@example.invalid'], check=True)
        subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Test'], check=True)
        (repo / 'code.txt').write_text('broken')
        (repo / 'capture.js').write_text('// screenshot fixture\n')
        (repo / 'repro.js').write_text('// reproduction fixture\n')
        (repo / 'test.js').write_text('// verification fixture\n')
        subprocess.run(['git', '-C', str(repo), 'add', 'code.txt', 'capture.js', 'repro.js', 'test.js'], check=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'baseline'], check=True)
        info = workflow.prepare(task_id='case', repo=repo, tasks_root=self.root / 'tasks',
                                workspaces_root=self.root / 'workspaces', source='reported bug',
                                kind='bug', surface='non-ui', iterations=3, seconds=600,
                                implementer='implementer')
        self.task = Path(info['task'])
        self.workspace = Path(info['workspace'])
        self.issue = self.root / 'issue.txt'
        self.issue.write_text('real task description')
        self.calls = []
        self.plan = {'status':'ready', 'criteria':[{'id':'one', 'text':'correct behavior'}],
                     'reproduction':{'command':['node','/workspace/repro.js'], 'marker':'BROKEN', 'exit_code':0},
                     'resolution':{'command':['node','/workspace/repro.js'], 'marker':'FIXED', 'exit_code':0},
                     'tests':{'command':['node','/workspace/test.js'], 'marker':'PASS', 'exit_code':0}}

    def tearDown(self):
        self.tmp.cleanup()

    def ci_pull(self, checks):
        data = cli.read(self.task)
        data['required_ci_workflows'] = ['CI']
        cli.write(self.task, data)
        CompletedCI.base = data['base_commit']
        return {'state': 'OPEN', 'isDraft': False, 'isCrossRepository': False,
                'headRefOid': 'a' * 40, 'headRefName': 'workbench/case',
                'baseRefName': 'main', 'baseRefOid': data['base_commit'],
                'statusCheckRollup': checks}

    def test_ci_wait_retries_pending_and_transient_errors_until_checks_complete(self):
        pending = self.ci_pull([{'status': 'QUEUED', 'conclusion': ''}])
        done = self.ci_pull([{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}])
        class GitHub(CompletedCI):
            def __init__(self): self.results = iter([delivery.DeliveryError('GitHub request failed: EOF'),
                *[delivery.DeliveryError(f'GitHub request failed: HTTP {status}') for status in (500, 502, 503, 504)], pending, done])
            def pull(self, repo, number):
                result = next(self.results)
                if isinstance(result, Exception): raise result
                return result
        delays = []
        autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=GitHub(), sleep=delays.append)
        self.assertEqual(len(delays), 6)
        self.assertTrue(all(0 < delay <= 30 for delay in delays))

    def test_ci_wait_fails_after_other_checks_finish(self):
        pending = self.ci_pull([{'status': 'QUEUED'}, {'status': 'COMPLETED', 'conclusion': 'FAILURE', 'name': 'test'}])
        finished = self.ci_pull([{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}, {'status': 'COMPLETED', 'conclusion': 'FAILURE', 'name': 'test'}])
        class GitHub(CompletedCI):
            def __init__(self): self.pulls = iter([pending, finished])
            def pull(self, repo, number): return next(self.pulls)
        delays = []
        with self.assertRaisesRegex(autopilot.AutopilotError, 'CI check failed'):
            autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=GitHub(), sleep=delays.append)
        self.assertEqual(len(delays), 1)

    def test_ci_failure_evidence_requires_exact_run_and_all_failure_annotations(self):
        details = {'id': 123, 'run_attempt': 2, 'head_sha': 'a' * 40, 'status': 'completed', 'conclusion': 'failure'}
        class GitHub:
            def _call(self, *args):
                if '/jobs?' in args[-1]:
                    return {'total_count': 1, 'jobs': [{'id': 5, 'name': 'test', 'status': 'completed', 'conclusion': 'failure'}]}
                return [{'message': 'assertion failed', 'path': 'test.ts', 'annotation_level': 'failure'}]
        evidence = autopilot._ci_failure_evidence(GitHub(), 'user/fork', 'CI', 123, details, 'a' * 40)
        self.assertEqual(evidence['attempt'], 2)
        self.assertEqual(evidence['jobs'][0]['annotations'][0]['message'], 'assertion failed')
        with self.assertRaises(autopilot.AutopilotError):
            autopilot._ci_failure_evidence(GitHub(), 'user/fork', 'CI', 123, {**details, 'head_sha': 'b' * 40}, 'a' * 40)
        class Missing(GitHub):
            def _call(self, *args): return {'total_count': 1, 'jobs': []}
        with self.assertRaises(autopilot.AutopilotError):
            autopilot._ci_failure_evidence(Missing(), 'user/fork', 'CI', 123, details, 'a' * 40)

    def test_ci_wait_collects_failure_only_after_required_workflow_finishes(self):
        pull = self.ci_pull([{'status': 'COMPLETED', 'conclusion': 'FAILURE', 'name': 'failed job'}])
        class GitHub(CompletedCI):
            def __init__(self): self.count = 0
            def pull(self, repo, number):
                self.count += 1
                return pull
            def _call(self, *args):
                if args[0] != 'api':
                    run = super()._call(*args)[0]
                    run.update(status='in_progress' if self.count == 1 else 'completed',
                               conclusion='' if self.count == 1 else 'failure')
                    return [run]
                if '/jobs?' in args[-1]:
                    return {'total_count': 1, 'jobs': [{'id': 5, 'name': 'test', 'status': 'completed', 'conclusion': 'failure'}]}
                if '/annotations?' in args[-1]:
                    return [{'message': 'assertion failed', 'annotation_level': 'failure'}]
                details = super()._call(*args)
                details.update(id=123, run_attempt=1, head_sha='a' * 40, status='completed', conclusion='failure')
                return details
        github = GitHub()
        sleeps = []
        with self.assertRaises(autopilot.CIFailure) as captured:
            autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=github, sleep=sleeps.append)
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(captured.exception.evidence['jobs'][0]['id'], 5)

    def test_ci_wait_retries_transient_failure_while_collecting_annotations(self):
        pull = self.ci_pull([{'status': 'COMPLETED', 'conclusion': 'FAILURE', 'name': 'failed job'}])
        class GitHub(CompletedCI):
            def __init__(self): self.count = 0
            def pull(self, repo, number): return pull
            def _call(self, *args):
                if args[0] != 'api':
                    run = super()._call(*args)[0]
                    run.update(status='completed', conclusion='failure')
                    return [run]
                if '/jobs?' in args[-1]:
                    return {'total_count': 1, 'jobs': [{'id': 5, 'name': 'test', 'status': 'completed', 'conclusion': 'failure'}]}
                if '/annotations?' in args[-1]:
                    self.count += 1
                    if self.count == 1: raise delivery.DeliveryError('GitHub request failed: HTTP 503')
                    return [{'message': 'assertion failed', 'annotation_level': 'failure'}]
                details = super()._call(*args)
                details.update(id=123, run_attempt=1, head_sha='a' * 40, status='completed', conclusion='failure')
                return details
        github = GitHub()
        sleeps = []
        with self.assertRaises(autopilot.CIFailure):
            autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=github, sleep=sleeps.append)
        self.assertEqual(github.count, 2)
        self.assertEqual(len(sleeps), 1)

    def test_ci_wait_never_retries_annotation_permission_error(self):
        pull = self.ci_pull([{'status': 'COMPLETED', 'conclusion': 'FAILURE', 'name': 'failed job'}])
        class GitHub(CompletedCI):
            def pull(self, repo, number): return pull
            def _call(self, *args):
                if args[0] != 'api':
                    run = super()._call(*args)[0]
                    run.update(status='completed', conclusion='failure')
                    return [run]
                if '/jobs?' in args[-1]:
                    return {'total_count': 1, 'jobs': [{'id': 5, 'name': 'test', 'status': 'completed', 'conclusion': 'failure'}]}
                if '/annotations?' in args[-1]:
                    raise delivery.DeliveryError('GitHub request failed: HTTP 403')
                details = super()._call(*args)
                details.update(id=123, run_attempt=1, head_sha='a' * 40, status='completed', conclusion='failure')
                return details
        with self.assertRaisesRegex(delivery.DeliveryError, '403'):
            autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=GitHub(),
                                   sleep=lambda _: self.fail('permission error must not retry'))

    def test_ci_wait_waits_for_all_required_workflows_before_collecting_failure(self):
        pull = self.ci_pull([{'status': 'COMPLETED', 'conclusion': 'FAILURE'}])
        data = cli.read(self.task)
        data['required_ci_workflows'] = ['CI', 'CI2']
        cli.write(self.task, data)
        class GitHub(CompletedCI):
            def __init__(self): self.count = 0
            def pull(self, repo, number):
                self.count += 1
                return pull
            def _call(self, *args):
                if args[0] != 'api':
                    name = args[args.index('--workflow') + 1]
                    run = super()._call(*args)[0]
                    run.update(workflowName=name, databaseId=123 if name == 'CI' else 124,
                               status='in_progress' if name == 'CI2' and self.count == 1 else 'completed',
                               conclusion='failure' if name == 'CI' else ('success' if self.count > 1 else ''))
                    return [run]
                if '/jobs?' in args[-1]:
                    return {'total_count': 1, 'jobs': [{'id': 5, 'name': 'test', 'status': 'completed', 'conclusion': 'failure'}]}
                if '/annotations?' in args[-1]:
                    return [{'message': 'assertion failed', 'annotation_level': 'failure'}]
                details = super()._call(*args)
                details.update(id=int(args[-1].split('/')[-1]), run_attempt=1, head_sha='a' * 40,
                               status='completed', conclusion='failure')
                return details
        github, sleeps = GitHub(), []
        with self.assertRaises(autopilot.CIFailure):
            autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=github, sleep=sleeps.append)
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(github.count, 2)

    def test_ci_wait_does_not_deliver_on_empty_rollup(self):
        empty = self.ci_pull([])
        complete = self.ci_pull([{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}])
        class GitHub(CompletedCI):
            def __init__(self): self.results = iter([empty, complete])
            def pull(self, repo, number): return next(self.results)
        delays = []
        autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=GitHub(), sleep=delays.append)
        self.assertEqual(len(delays), 1)

    def test_ci_wait_stops_at_deadline_when_checks_never_appear(self):
        pull = self.ci_pull([])
        github = type('GitHub', (CompletedCI,), {'pull': lambda self, repo, number: pull})()
        def expire(_):
            data = cli.read(self.task)
            data['started_at'] = '2000-01-01T00:00:00+00:00'
            cli.write(self.task, data)
        with self.assertRaises(autopilot.AutopilotError):
            autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=github, sleep=expire)

    def test_ci_wait_rejects_changed_head_and_permanent_github_errors(self):
        pull = self.ci_pull([{'status': 'IN_PROGRESS'}]); pull['headRefOid'] = 'b' * 40
        github = type('GitHub', (CompletedCI,), {'pull': lambda self, repo, number: pull})()
        with self.assertRaisesRegex(autopilot.AutopilotError, 'identity changed'):
            autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=github)
        class Denied(CompletedCI):
            def pull(self, repo, number): raise delivery.DeliveryError('GitHub request failed: HTTP 403')
        with self.assertRaisesRegex(delivery.DeliveryError, '403'):
            autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=Denied())

    def test_ci_wait_stops_when_time_budget_expires(self):
        pull = self.ci_pull([{'state': 'PENDING'}])
        github = type('GitHub', (CompletedCI,), {'pull': lambda self, repo, number: pull})()
        def expire(_):
            data = cli.read(self.task)
            data['started_at'] = '2000-01-01T00:00:00+00:00'
            cli.write(self.task, data)
        with self.assertRaises(autopilot.AutopilotError):
            autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=github, sleep=expire)

    def test_ci_wait_rechecks_budget_after_github_query_before_sleep(self):
        pull = self.ci_pull([{'state': 'PENDING'}])
        task = self.task
        class GitHub(CompletedCI):
            def pull(self, repo, number):
                data = cli.read(task)
                data['started_at'] = '2000-01-01T00:00:00+00:00'
                cli.write(task, data)
                return pull
        with self.assertRaises(autopilot.AutopilotError):
            autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=GitHub(),
                                   sleep=lambda _: self.fail('must not sleep after deadline'))

    def test_ci_requires_explicit_workflows(self):
        for value in (None, [], ['CI', 'CI'], [''], '-invalid'):
            data = cli.read(self.task)
            data['required_ci_workflows'] = value
            cli.write(self.task, data)
            with self.assertRaisesRegex(autopilot.AutopilotError, 'required_ci_workflows'):
                autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=CompletedCI())

    def test_ci_wait_wont_treat_unrelated_success_as_required_workflow(self):
        pull = self.ci_pull([{'status': 'COMPLETED', 'conclusion': 'SUCCESS', 'name': 'Labeler'}])
        class GitHub(CompletedCI):
            def __init__(self): self.count = 0
            def pull(self, repo, number): return pull
            def _call(self, *args):
                if args[0] == 'api': return super()._call(*args)
                self.count += 1
                return [] if self.count == 1 else super()._call(*args)
        delays = []
        autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=GitHub(), sleep=delays.append)
        self.assertEqual(len(delays), 1)

    def test_ci_wait_rejects_wrong_or_failed_workflow(self):
        pull = self.ci_pull([{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}])
        for change in ({'headSha': 'b' * 40}, {'headBranch': 'other'}, {'event': 'push'},
                       {'status': 'completed', 'conclusion': 'failure'}):
            class GitHub(CompletedCI):
                def pull(self, repo, number): return pull
                def _call(self, *args):
                    if args[0] == 'api': return super()._call(*args)
                    run = super()._call(*args)[0]
                    run.update(change)
                    return [run]
            with self.assertRaises(autopilot.AutopilotError):
                autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=GitHub())

    def test_ci_wait_rejects_run_for_other_pr_or_baseline(self):
        pull = self.ci_pull([{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}])
        for change in ('number', 'base'):
            class GitHub(CompletedCI):
                def pull(self, repo, number): return pull
                def _call(self, *args):
                    result = super()._call(*args)
                    if args[0] == 'api':
                        if change == 'number': result['pull_requests'][0]['number'] = 2
                        else: result['pull_requests'][0]['base']['sha'] = 'b' * 40
                    return result
            with self.assertRaisesRegex(autopilot.AutopilotError, 'not bound'):
                autopilot._wait_for_ci(self.task, 'user/fork', 1, 'a' * 40, github=GitHub())

    def test_direct_review_receives_both_versions_without_tools(self):
        (self.workspace / 'code.txt').write_text('fixed')
        subprocess.run(['git', '-C', str(self.workspace), 'add', 'code.txt'], check=True)
        subprocess.run(['git', '-C', str(self.workspace), 'commit', '-qm', 'fix'], check=True)
        captured = []
        def review(prompt, timeout):
            captured.append(prompt)
            return '{"status":"blocked","reason":"need more evidence"}'
        with patch.object(autopilot, 'direct_review', review):
            output = autopilot._model(self.task, 'final-review', 'inspect evidence', read_only=True,
                                      model=autopilot.execute_model)
            autopilot._model(self.task, 'final-review', 'inspect again', read_only=True,
                             model=autopilot.execute_model)
        self.assertIn('broken', captured[0])
        self.assertIn('fixed', captured[0])
        self.assertIn('no tools', captured[0])
        self.assertIn('blocked', output)
        history = list(self.task.parent.glob('final-review-model-previous-*.json'))
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text())['stdout'], output)

    def model(self, workspace, prompt, timeout, *, read_only):
        if 'Return only JSON:' in prompt:
            self.calls.append('plan')
            output = self.plan
        elif 'actual baseline run' in prompt:
            self.calls.append('baseline-review')
            output = {'status':'ready', 'verdict':'PASS'}
        elif 'Implement this explicitly' in prompt:
            self.calls.append('implement')
            self.assertFalse(read_only)
            (workspace / 'code.txt').write_text('fixed')
            return {'exit_code':0, 'timed_out':False, 'stdout':'implemented', 'stderr':''}
        else:
            self.calls.append('final-review')
            ids = [item['id'] for item in cli.read(self.task)['criteria']]
            refs = ['R1', 'R6'] if 'independent screenshot report R6' in prompt else ['R1']
            output = {'status':'ready', 'verdict':'PASS', 'assessments':[
                {'id':item, 'verdict':'PASS', 'reason':'The observed report supports this criterion.', 'evidence':refs}
                for item in ids]}
        return {'exit_code':0, 'timed_out':False, 'stdout':json.dumps(output), 'stderr':''}

    def check(self, *, task, kind, image, command, timeout, expect_exit, marker, actor, description):
        self.calls.append(f'{kind}:{actor}')
        data = cli.read(task)
        commit = cli.sha(data['repo'], 'HEAD')
        report = task.parent / f'{len(self.calls)}-{kind}.json'
        cli.write(report, {'kind':kind, 'actor':actor, 'commit':commit, 'container_created':True,
                           'cleanup_confirmed':True, 'timed_out':False, 'exit_code':expect_exit,
                           'expected_exit':expect_exit, 'stdout':marker, 'expected_marker':marker,
                           'workspace':str(self.workspace), 'command':command, 'source_mode':'workspace',
                           'source_verified_commit':commit})
        cli.evidence_add(argparse.Namespace(file=str(report), kind=kind, commit=commit,
                                            actor=actor, description=description, criterion=[]), data)
        cli.write(task, data)
        return {'report':str(report), 'commit':commit, 'kind':kind}

    def test_failed_reproduction_stops_before_modification(self):
        def fail(**kwargs):
            raise workflow.WorkflowError('no real reproduction')
        with self.assertRaisesRegex(autopilot.AutopilotError, 'no real reproduction'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=fail)
        self.assertNotIn('implement', self.calls)
        self.assertEqual((self.workspace / 'code.txt').read_text(), 'broken')
        self.assertEqual(cli.read(self.task)['status'], 'paused')

    def prepare_adoption(self):
        base = cli.sha(self.workspace, 'HEAD')
        (self.workspace / 'code.txt').write_text('adopted fix')
        subprocess.run(['git', '-C', str(self.workspace), 'add', 'code.txt'], check=True)
        subprocess.run(['git', '-C', str(self.workspace), 'commit', '-qm', 'candidate'], check=True)
        candidate = cli.sha(self.workspace, 'HEAD')
        subprocess.run(['git', '-C', str(self.workspace), 'reset', '--hard', base],
                       check=True, stdout=subprocess.DEVNULL)
        data = cli.read(self.task)
        data['criteria'] = [{'id': 'one', 'text': 'correct behavior', 'revision': 1}]
        rebound_issue = self.task.parent / 'issue.txt'
        rebound_issue.write_bytes(self.issue.read_bytes())
        self.issue = rebound_issue
        snapshot = self.task.parent / 'legacy-task.json'
        cli.write(snapshot, {'schema': cli.SCHEMA, 'origin': 'test'})
        data['adoption'] = {'base_commit': base, 'candidate_commit': candidate,
            'request_digest': hashlib.sha256(self.issue.read_bytes()).hexdigest(),
            'criteria_digest': cli.criteria_digest(data), 'image': 'image', 'pr_repo': None, 'memory_mb': 2048,
            'legacy_snapshot_sha256': cli.digest(snapshot)}
        cli.write(self.task, data)
        return base, candidate

    def run_adoption(self, check=None):
        def checked(**kwargs):
            kwargs.pop('source_mode')
            return (check or self.check)(**kwargs)
        with patch.object(workflow, 'build_source_image', return_value='candidate-image'):
            return autopilot.run(self.task, self.issue, 'image', source_mode='image',
                build_source_image=True, model=self.model, check=checked)

    def test_adopted_candidate_is_verified_only_after_real_baseline_phases(self):
        base, candidate = self.prepare_adoption()
        commits = []
        def checked(**kwargs):
            commits.append((kwargs['kind'], cli.sha(self.workspace, 'HEAD')))
            return self.check(**kwargs)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            self.run_adoption(check=checked)
        self.assertEqual(commits[:2], [('reproduction', base), ('reproduction', base)])
        self.assertTrue(all(commit == candidate for _, commit in commits[2:]))
        self.assertIn('baseline-review', self.calls)
        self.assertIn('final-review', self.calls)
        self.assertNotIn('implement', self.calls)
        self.assertEqual(cli.sha(self.workspace, 'HEAD'), candidate)

    def test_adoption_preserves_persisted_baseline_evidence_through_delivery(self):
        base, candidate = self.prepare_adoption()
        baseline_evidence = []
        def checked(**kwargs):
            if kwargs['kind'] != 'reproduction':
                retained = [entry for entry in cli.read(self.task)['evidence']
                            if entry['kind'] == 'reproduction']
                self.assertEqual(retained, baseline_evidence)
            result = self.check(**kwargs)
            if kwargs['kind'] == 'reproduction':
                baseline_evidence[:] = [entry for entry in cli.read(self.task)['evidence']
                                        if entry['kind'] == 'reproduction']
            return result
        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            self.run_adoption(check=checked)
        state = cli.read(self.task.parent / 'autopilot.json')
        self.assertEqual(state['phase'], 'delivery')
        self.assertEqual(len(baseline_evidence), 2)
        self.assertEqual({entry['actor'] for entry in baseline_evidence},
                         {'implementer', 'independent-verifier'})
        self.assertTrue(all(entry['commit'] == base for entry in baseline_evidence))
        retained = [entry for entry in cli.read(self.task)['evidence']
                    if entry['kind'] == 'reproduction']
        self.assertEqual(retained, baseline_evidence)
        self.assertEqual([entry['path'] for entry in retained], state['baseline_reports'])
        self.assertTrue(all(cli.digest(Path(entry['path'])) == entry['sha256'] for entry in retained))
        self.assertEqual(state['candidate_commit'], candidate)

    def test_adoption_rechecks_authoritative_acceptance_before_switching_candidate(self):
        base, _ = self.prepare_adoption()
        def checked(**kwargs):
            result = self.check(**kwargs)
            if kwargs['kind'] == 'reproduction' and kwargs['actor'] == 'independent-verifier':
                data = cli.read(self.task)
                data['criteria'][0]['text'] = 'changed during reproduction'
                cli.write(self.task, data)
            return result
        with self.assertRaisesRegex(autopilot.AutopilotError, 'acceptance changed'):
            self.run_adoption(check=checked)
        self.assertEqual(cli.sha(self.workspace, 'HEAD'), base)
        self.assertFalse(any(event['type'] == 'candidate-adopted'
                             for event in cli.read(self.task)['events']))

    def test_adoption_does_not_switch_candidate_when_reproduction_fails(self):
        base, _ = self.prepare_adoption()
        def fail(**kwargs):
            raise workflow.WorkflowError('no real reproduction')
        with self.assertRaisesRegex(autopilot.AutopilotError, 'no real reproduction'):
            self.run_adoption(check=fail)
        self.assertEqual(cli.sha(self.workspace, 'HEAD'), base)
        self.assertNotIn('implement', self.calls)

    def test_adoption_rejects_changed_acceptance(self):
        self.prepare_adoption()
        data = cli.read(self.task)
        data['adoption']['criteria_digest'] = 'changed'
        cli.write(self.task, data)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'acceptance changed'):
            self.run_adoption()
        self.assertEqual(cli.sha(self.workspace, 'HEAD'), data['base_commit'])

    def test_adoption_rejects_changed_isolation_settings(self):
        self.prepare_adoption()
        with self.assertRaisesRegex(autopilot.AutopilotError, 'execution settings changed'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check)
        self.assertNotIn('plan', self.calls)

    def test_adoption_rejects_missing_baseline_evidence(self):
        base, _ = self.prepare_adoption()
        cli.write(self.task.parent / 'autopilot.json', {'schema': cli.SCHEMA, 'phase': 'implement',
            'issue_digest': hashlib.sha256(self.issue.read_bytes()).hexdigest(), 'input_digest': None,
            'plan': self.plan, 'baseline_reports': []})
        with self.assertRaisesRegex(autopilot.AutopilotError, 'both independently reviewed baseline runs'):
            self.run_adoption()
        self.assertEqual(cli.sha(self.workspace, 'HEAD'), base)

    def test_adoption_rejects_changed_acceptance_when_resuming_late_phases(self):
        for phase in ('verify', 'delivery'):
            with self.subTest(phase=phase):
                if phase == 'delivery':
                    data = cli.read(self.task)
                    data['status'] = 'running'
                    cli.write(self.task, data)
                else:
                    self.prepare_adoption()
                data = cli.read(self.task)
                data['criteria'][0]['text'] = 'weakened'
                cli.write(self.task, data)
                cli.write(self.task.parent / 'autopilot.json', {'schema': cli.SCHEMA, 'phase': phase,
                    'issue_digest': hashlib.sha256(self.issue.read_bytes()).hexdigest(), 'input_digest': None,
                    'plan': self.plan, 'baseline_reports': ['a', 'b']})
                with self.assertRaisesRegex(autopilot.AutopilotError, 'acceptance changed'):
                    self.run_adoption()
                self.assertNotIn('final-review', self.calls)

    def test_adoption_rejects_missing_modified_or_symlinked_legacy_snapshot(self):
        self.prepare_adoption()
        snapshot = self.task.parent / 'legacy-task.json'
        original = snapshot.read_bytes()
        for change in ('missing', 'modified', 'symlink'):
            with self.subTest(change=change):
                if snapshot.is_symlink() or snapshot.exists():
                    snapshot.unlink()
                if change == 'modified':
                    snapshot.write_text('changed')
                elif change == 'symlink':
                    outside = self.root / 'outside.json'
                    outside.write_bytes(original)
                    snapshot.symlink_to(outside)
                data = cli.read(self.task)
                data['status'] = 'running'
                cli.write(self.task, data)
                with self.assertRaisesRegex(autopilot.AutopilotError, 'snapshot or request path changed'):
                    self.run_adoption()
                self.assertNotIn('plan', self.calls)

    def test_adoption_rejects_request_copied_outside_new_task(self):
        self.prepare_adoption()
        outside = self.root / 'outside-issue.txt'
        outside.write_bytes(self.issue.read_bytes())
        self.issue = outside
        with self.assertRaisesRegex(autopilot.AutopilotError, 'snapshot or request path changed'):
            self.run_adoption()

    def test_adoption_recovers_if_interrupted_after_fast_forward(self):
        _, candidate = self.prepare_adoption()
        original = subprocess.run
        def interrupted(command, **kwargs):
            result = original(command, **kwargs)
            if command[:1] == ['git'] and 'merge' in command:
                raise subprocess.TimeoutExpired(command, 1)
            return result
        with patch('workbench.autopilot.subprocess.run', side_effect=interrupted):
            with self.assertRaisesRegex(autopilot.AutopilotError, 'cannot fast-forward'):
                self.run_adoption()
        self.assertEqual(cli.sha(self.workspace, 'HEAD'), candidate)
        data = cli.read(self.task)
        self.assertEqual(data['status'], 'paused')
        data['status'] = 'running'
        cli.write(self.task, data)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            self.run_adoption()
        self.assertEqual(self.calls.count('reproduction:independent-verifier'), 1)
        self.assertIn('resolution:independent-verifier', self.calls)
        events = [e for e in cli.read(self.task)['events'] if e.get('type') == 'candidate-adopted']
        self.assertEqual(len(events), 1)

    def test_separate_images_and_resource_limit_reach_each_check(self):
        checks = []
        def check(**kwargs):
            checks.append((kwargs['kind'], kwargs['image'], kwargs.pop('source_mode'),
                           kwargs.pop('memory_mb')))
            return self.check(**kwargs)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            autopilot.run(self.task, self.issue, 'baseline', candidate_image='candidate',
                          source_mode='image', memory_mb=4096, model=self.model, check=check)
        self.assertEqual([x[1] for x in checks[:2]], ['baseline', 'baseline'])
        self.assertTrue(all(x[1] == 'candidate' for x in checks[2:]))
        self.assertTrue(all(x[2:] == ('image', 4096) for x in checks))

    def test_candidate_image_requires_verified_image_mode(self):
        with self.assertRaisesRegex(autopilot.AutopilotError, 'requires image source mode'):
            autopilot.run(self.task, self.issue, 'baseline', candidate_image='candidate')

    def test_source_image_is_built_for_candidate_and_used_for_checks(self):
        checks = []
        def check(**kwargs):
            checks.append((kwargs['kind'], kwargs['image']))
            kwargs.pop('source_mode')
            return self.check(**kwargs)
        with patch.object(workflow, 'build_source_image', return_value='sha256:candidate') as builder:
            with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
                autopilot.run(self.task, self.issue, 'baseline', source_mode='image',
                              build_source_image=True, model=self.model, check=check)
        self.assertEqual([image for _, image in checks[:2]], ['baseline', 'baseline'])
        self.assertTrue(all(image == 'sha256:candidate' for _, image in checks[2:]))
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(builder.call_args.args[1], cli.read(self.task)['base_commit'])

    def test_full_local_path_requires_pr_and_preserves_evidence(self):
        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check)
        data = cli.read(self.task)
        self.assertEqual(data['status'], 'paused')
        self.assertEqual((self.workspace / 'code.txt').read_text(), 'fixed')
        self.assertIn('reproduction:independent-verifier', self.calls)
        self.assertIn('resolution:independent-verifier', self.calls)
        self.assertIn('final-review', self.calls)
        self.assertEqual(cli.read(self.task.parent / 'autopilot.json')['phase'], 'delivery')

    def test_resume_delivery_uses_guard(self):
        with self.assertRaises(autopilot.AutopilotError):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check)
        data = cli.read(self.task)
        data['status'] = 'running'
        cli.write(self.task, data)
        merged = []
        def merge(task, repo, pr):
            merged.append((task, repo, pr))
            return {'merged':True}
        result = autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check,
                               pr_repo='user/fork', pr_number=1, merge=merge)
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(merged[0][1:], ('user/fork', 1))

    def test_reverify_preserves_previous_review_and_creates_a_new_one(self):
        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check)
        data = cli.read(self.task)
        old = next(item for item in data['evidence'] if item['kind'] == 'independent-review')
        data['status'] = 'running'; cli.write(self.task, data)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check, reverify=True)
        data = cli.read(self.task)
        reviews = [item for item in data['evidence'] if item['kind'] == 'independent-review']
        self.assertEqual(len(reviews), 2)
        self.assertNotEqual(reviews[0]['path'], reviews[1]['path'])
        self.assertEqual(cli.digest(reviews[0]['path']), old['sha256'])

    def test_changed_task_request_blocks_resume(self):
        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check)
        data = cli.read(self.task)
        data['status'] = 'running'
        cli.write(self.task, data)
        self.issue.write_text('different request')
        with self.assertRaisesRegex(autopilot.AutopilotError, 'request or supplied logs changed'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check)
        self.assertEqual(cli.read(self.task)['status'], 'paused')

    def test_request_is_bound_even_when_initial_plan_fails(self):
        def bad_model(*args, **kwargs):
            return {'exit_code': 0, 'timed_out': False, 'stdout': 'not json', 'stderr': ''}
        with self.assertRaisesRegex(autopilot.AutopilotError, 'valid JSON'):
            autopilot.run(self.task, self.issue, 'image', model=bad_model, check=self.check)
        data = cli.read(self.task)
        data['status'] = 'running'
        cli.write(self.task, data)
        self.issue.write_text('different request')
        with self.assertRaisesRegex(autopilot.AutopilotError, 'request or supplied logs changed'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check)

    def test_log_snapshots_and_manifest_are_bound_on_resume(self):
        directory = self.task.parent / 'inputs'
        directory.mkdir()
        original = b'original log\n'
        (directory / 'log-1.txt').write_bytes(original)
        manifest = {'logs': [{'file': 'log-1.txt', 'original_name': 'failure.log',
                              'sha256': hashlib.sha256(original).hexdigest()}]}
        (directory / 'manifest.json').write_text(json.dumps(manifest))
        def bad_model(*args, **kwargs):
            return {'exit_code': 0, 'timed_out': False, 'stdout': 'not json', 'stderr': ''}
        with self.assertRaises(autopilot.AutopilotError):
            autopilot.run(self.task, self.issue, 'image', model=bad_model, check=self.check)
        data = cli.read(self.task)
        data['status'] = 'running'
        cli.write(self.task, data)
        changed = b'changed log\n'
        (directory / 'log-1.txt').write_bytes(changed)
        manifest['logs'][0]['sha256'] = hashlib.sha256(changed).hexdigest()
        (directory / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(autopilot.AutopilotError, 'input snapshot changed'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check)
        self.assertEqual(cli.read(self.task)['status'], 'paused')

    def test_screenshot_snapshot_tampering_is_detected(self):
        directory = self.task.parent / 'inputs'
        directory.mkdir()
        content = b'\x89PNG\r\n\x1a\nexample'
        (directory / 'screenshot-1.png').write_bytes(content)
        manifest = {'logs': [], 'screenshots': [{'file': 'screenshot-1.png',
                    'original_name': 'screen.png', 'sha256': hashlib.sha256(content).hexdigest()}]}
        (directory / 'manifest.json').write_text(json.dumps(manifest))
        self.assertIsNotNone(autopilot._input_digest(self.task))
        (directory / 'screenshot-1.png').write_bytes(b'changed')
        with self.assertRaisesRegex(autopilot.AutopilotError, 'snapshot changed'):
            autopilot._input_digest(self.task)

    def test_unstructured_plan_stops_without_editing(self):
        self.plan['reproduction']['marker'] = ''
        with self.assertRaisesRegex(autopilot.AutopilotError, 'observable marker'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check)
        self.assertNotIn('implement', self.calls)

    def test_planning_receives_existing_criteria_and_command_contract(self):
        data = cli.read(self.task)
        criteria = [{'id': 'real-lock', 'text': 'Acquire a real SQLite lock during Gateway startup.'}]
        data['criteria'] = criteria
        cli.write(self.task, data)
        prompts = []
        self.plan['reproduction']['marker'] = ''
        def model(workspace, prompt, timeout, *, read_only):
            prompts.append(prompt)
            self.assertTrue(read_only)
            return self.model(workspace, prompt, timeout, read_only=read_only)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'observable marker'):
            autopilot.run(self.task, self.issue, 'image', model=model, check=self.check)
        self.assertEqual(len(prompts), 1)
        self.assertIn(json.dumps(criteria, ensure_ascii=False), prompts[0])
        self.assertIn('at most 40 strings', prompts[0])
        self.assertIn('at most 8192 characters', prompts[0])
        self.assertIn('preserve their IDs and text', prompts[0])
        self.assertIn('acquire an actual SQLite lock', prompts[0])
        self.assertIn('not substitute a simulated lock error', prompts[0])
        self.assertEqual(cli.read(self.task)['criteria'], criteria)
        self.assertNotIn('implement', self.calls)

    def test_oversized_planning_argument_is_rejected_before_execution(self):
        self.plan['reproduction']['command'] = ['node', '-e', 'x' * 8193]
        with self.assertRaisesRegex(autopilot.AutopilotError, 'bounded argument array'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check)
        self.assertEqual(self.calls, ['plan'])
        self.assertEqual((self.workspace / 'code.txt').read_text(), 'broken')

    def test_publishes_verified_task_before_guarded_merge(self):
        events = []
        def publisher(task, repo):
            events.append(('publish', repo))
            return {'repo': repo, 'pr': 12, 'head': 'x', 'url': 'https://github.com/user/fork/pull/12'}
        def merge(task, repo, number):
            events.append(('merge', repo, number))
            return {'merged': True}
        result = autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check,
                               pr_repo='user/fork', publisher=publisher, merge=merge)
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(events, [('publish', 'user/fork'), ('merge', 'user/fork', 12)])
        self.assertEqual(cli.read(self.task.parent / 'autopilot.json')['published_pr']['pr'], 12)

    def test_issue_close_failure_can_resume_without_merging_twice(self):
        data = cli.read(self.task)
        data['source'] = 'https://github.com/user/fork/issues/12'
        data['original_source'] = data['source']
        cli.write(self.task, data)
        merges, closes = [], []
        def merge(*args):
            merges.append(args)
            return {'merged': True}
        def fail_close(*args):
            closes.append(args)
            raise delivery.DeliveryError('issue service unavailable')
        with self.assertRaisesRegex(autopilot.AutopilotError, 'issue service unavailable'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check,
                          pr_repo='user/fork', pr_number=5, merge=merge, closer=fail_close)
        self.assertEqual(cli.read(self.task.parent / 'autopilot.json')['phase'], 'close-issue')
        data = cli.read(self.task)
        data['status'] = 'running'
        cli.write(self.task, data)
        result = autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check,
                               merge=merge, closer=lambda *args: {'closed': True, 'issue': 12})
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(len(merges), 1)
        self.assertEqual(len(closes), 1)
        self.assertEqual(result['issue']['issue'], 12)

    def test_invalid_criterion_never_persists_partial_set(self):
        self.plan['criteria'].append({'id':'bad id', 'text':'second'})
        with self.assertRaises(autopilot.AutopilotError):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check)
        self.assertEqual(cli.read(self.task)['criteria'], [])

    def test_ui_task_stops_without_screenshot_plan(self):
        data = cli.read(self.task)
        data['surface'] = 'ui'
        cli.write(self.task, data)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'screenshot'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check)
        self.assertNotIn('implement', self.calls)

    def test_ui_task_review_binds_independent_screenshot(self):
        data = cli.read(self.task); data['surface'] = 'ui'; cli.write(self.task, data)
        self.plan['screenshot'] = {'command': ['node', '/workspace/capture.js'], 'marker': 'CAPTURED', 'exit_code': 0}
        def check(**kwargs):
            if kwargs['kind'] != 'screenshot':
                return self.check(**kwargs)
            data = cli.read(kwargs['task']); commit = cli.sha(data['repo'], 'HEAD')
            screenshot = self.task.parent / 'actual.png'; screenshot.write_bytes(tiny_png())
            report = self.task.parent / 'actual.json'
            cli.write(report, {'kind': 'screenshot', 'actor': kwargs['actor'], 'commit': commit,
                              'source_mode': 'workspace', 'source_verified_commit': commit,
                              'workspace': str(self.workspace), 'command': kwargs['command'],
                              'container_created': True, 'cleanup_confirmed': True, 'timed_out': False,
                              'exit_code': 0, 'expected_exit': 0, 'stdout': 'CAPTURED', 'expected_marker': 'CAPTURED',
                              'screenshot_path': str(screenshot), 'screenshot_sha256': cli.digest(screenshot)})
            cli.evidence_add(argparse.Namespace(file=str(screenshot), kind='screenshot', commit=commit,
                actor=kwargs['actor'], description='capture', criterion=[]), data)
            data['evidence'][-1].update(execution_report=str(report), execution_report_sha256=cli.digest(report))
            cli.write(self.task, data)
            return {'report': str(report), 'screenshot': str(screenshot)}
        prompts = []
        def model(workspace, prompt, timeout, *, read_only):
            if 'Independently inspect the candidate' in prompt:
                prompts.append(prompt)
            return self.model(workspace, prompt, timeout, read_only=read_only)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            autopilot.run(self.task, self.issue, 'image', model=model, check=check,
                          vision=lambda data, timeout: 'The UI shows the expected status')
        item = next(item for item in cli.read(self.task)['evidence'] if item['kind'] == 'independent-review')
        review = json.loads(Path(item['path']).read_text())
        self.assertEqual(review['screenshot_hashes'], [cli.digest(self.task.parent / 'actual.png')])
        evidence = cli.read(self.task)['evidence']
        self.assertTrue(any(item['kind'] == 'screenshot' and item['actor'] == 'independent-verifier'
                            and item['sha256'] == review['screenshot_hashes'][0] for item in evidence))
        self.assertIn('The UI shows the expected status', prompts[0])
        self.assertIn('// screenshot fixture', prompts[0])
        delivery._independent_verification(cli.read(self.task), cli.sha(self.workspace, 'HEAD'))

    def test_feature_records_tracked_function_documentation(self):
        data = cli.read(self.task)
        data['kind'] = 'feature'
        cli.write(self.task, data)
        self.plan['feature_doc'] = 'feature.md'
        def model(workspace, prompt, timeout, *, read_only):
            result = self.model(workspace, prompt, timeout, read_only=read_only)
            if 'Implement this explicitly' in prompt:
                (workspace / 'feature.md').write_text('Feature behavior for users.\n')
            return result
        with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
            autopilot.run(self.task, self.issue, 'image', model=model, check=self.check)
        self.assertIn('feature-doc', [item['kind'] for item in cli.read(self.task)['evidence']])

    def test_final_reviewer_receives_baseline_contents_and_criteria_history(self):
        data = cli.read(self.task)
        cli.criterion_add(argparse.Namespace(id='one', text='original', reason='user'), data)
        basis = self.root / 'basis.txt'
        basis.write_text('documented reason for revision')
        cli.criterion_change(argparse.Namespace(id='one', text='clarified', reason='clarification', basis=str(basis)), data)
        cli.write(self.task, data)
        seen = []
        def model(workspace, prompt, timeout, *, read_only):
            if 'Independently inspect the candidate' in prompt:
                seen.append(prompt)
            return self.model(workspace, prompt, timeout, read_only=read_only)
        with self.assertRaises(autopilot.AutopilotError):
            autopilot.run(self.task, self.issue, 'image', model=model, check=self.check)
        self.assertEqual(len(seen), 1)
        self.assertIn('documented reason for revision', seen[0])
        self.assertIn('original', seen[0])
        self.assertIn('BROKEN', seen[0])

    def test_failed_check_is_repaired_with_raw_evidence_then_reverified(self):
        failed = []
        repair_prompts = []
        def check(**kwargs):
            if kwargs['kind'] == 'resolution' and not failed:
                failed.append(True)
                report = self.task.parent / 'failed-actual.json'
                cli.write(report, {'stdout':'real assertion failed: expected 42', 'exit_code':1,
                                   'cleanup_confirmed':True, 'timed_out':False})
                raise workflow.WorkflowError(f'check failed; raw report saved at {report}')
            return self.check(**kwargs)
        def model(workspace, prompt, timeout, *, read_only):
            if prompt.startswith('Repair the current candidate'):
                repair_prompts.append(prompt)
                (workspace / 'code.txt').write_text('repaired after failure')
                return {'exit_code':0, 'timed_out':False, 'stdout':'repaired', 'stderr':''}
            return self.model(workspace, prompt, timeout, read_only=read_only)
        result = autopilot.run(self.task, self.issue, 'image', model=model, check=check,
                               pr_repo='user/fork', pr_number=1,
                               merge=lambda *args: {'merged':True})
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(cli.read(self.task)['iterations'], 2)
        self.assertIn('real assertion failed: expected 42', repair_prompts[0])
        self.assertEqual((self.workspace / 'code.txt').read_text(), 'repaired after failure')
        self.assertTrue((self.task.parent / 'verification-failure-1.json').exists())
        self.assertIn('resolution:independent-verifier', self.calls)

    def test_ci_failure_repairs_reverifies_and_updates_same_pr(self):
        prompts, updates, checks = [], [], []
        failure = {'workflow': 'CI', 'run_id': 12, 'attempt': 1, 'head': 'old',
                   'jobs': [{'id': 5, 'name': 'test', 'annotations': [{'message': 'assertion failed'}]}]}
        def model(workspace, prompt, timeout, *, read_only):
            if prompt.startswith('Repair the verified candidate'):
                prompts.append(prompt)
                (workspace / 'code.txt').write_text('fixed after CI')
                return {'exit_code': 0, 'timed_out': False, 'stdout': 'done', 'stderr': ''}
            return self.model(workspace, prompt, timeout, read_only=read_only)
        def check(**kwargs):
            checks.append((kwargs['kind'], kwargs['actor'], cli.sha(self.workspace, 'HEAD')))
            return self.check(**kwargs)
        def republish(task, repo, number, old):
            updates.append((repo, number, old, cli.sha(self.workspace, 'HEAD')))
            return {'repo': repo, 'pr': number, 'head': updates[-1][3]}
        def merge(task, repo, number): return {'merged': True}
        count = []
        def wait(task, repo, number, head):
            count.append(head)
            if len(count) == 1:
                raise autopilot.CIFailure({**failure, 'head': head})
        with patch.object(autopilot, '_wait_for_ci', side_effect=wait), \
             patch.object(delivery, 'guarded_merge', merge), patch.object(autopilot.publish, 'republish', republish):
            result = autopilot.run(self.task, self.issue, 'image', model=model, check=check,
                                   pr_repo='user/fork', pr_number=1, merge=merge)
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(cli.read(self.task)['iterations'], 2)
        self.assertEqual(len(updates), 1)
        self.assertNotEqual(updates[0][2], updates[0][3])
        self.assertIn('untrusted data', prompts[0])
        self.assertIn('assertion failed', prompts[0])
        self.assertEqual(len([c for c in checks if c[:2] == ('resolution', 'independent-verifier')]), 2)

    def test_ci_failure_at_iteration_limit_preserves_failure_and_does_not_repair(self):
        data = cli.read(self.task)
        data['limits']['iterations'] = 1
        cli.write(self.task, data)
        def wait(task, repo, number, head):
            raise autopilot.CIFailure({'workflow': 'CI', 'run_id': 12, 'attempt': 1,
                                       'head': head, 'jobs': [{'id': 5, 'annotations': [{'message': 'failed'}]}]})
        def merge(task, repo, number): self.fail('merge must not run')
        with patch.object(autopilot, '_wait_for_ci', side_effect=wait), patch.object(delivery, 'guarded_merge', merge):
            with self.assertRaisesRegex(autopilot.AutopilotError, 'iteration limit'):
                autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check,
                              pr_repo='user/fork', pr_number=1, merge=merge)
        self.assertEqual(cli.read(self.task)['iterations'], 1)
        self.assertEqual(cli.read(self.task)['status'], 'paused')
        self.assertEqual(len(list(self.task.parent.glob('ci-failure-*.json'))), 1)

    def test_interruption_after_ci_iteration_record_does_not_charge_twice(self):
        real_write = cli.write
        def interrupt(path, data):
            if Path(path).name == 'autopilot.json' and data.get('phase') == 'repairing':
                raise OSError('simulated crash before repair state persisted')
            return real_write(path, data)
        def wait(task, repo, number, head):
            raise autopilot.CIFailure({'workflow': 'CI', 'run_id': 12, 'attempt': 1,
                                       'head': head, 'jobs': [{'id': 5, 'annotations': [{'message': 'failed'}]}]})
        def merge(task, repo, number): self.fail('merge must not run')
        with patch.object(autopilot, '_wait_for_ci', side_effect=wait), patch.object(delivery, 'guarded_merge', merge):
            with patch.object(cli, 'write', side_effect=interrupt):
                with self.assertRaises(autopilot.AutopilotError):
                    autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check,
                                  pr_repo='user/fork', pr_number=1, merge=merge)
            data = cli.read(self.task)
            self.assertEqual(data['iterations'], 2)
            data['status'] = 'running'
            cli.write(self.task, data)
            with self.assertRaisesRegex(autopilot.AutopilotError, 'already recorded'):
                autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check,
                              pr_repo='user/fork', pr_number=1, merge=merge)
        self.assertEqual(cli.read(self.task)['iterations'], 2)

    def test_source_image_is_rebuilt_after_repair(self):
        built = []
        failed = []
        def build(repo, base, image, timeout):
            built.append(cli.sha(repo, 'HEAD'))
            return 'candidate-' + str(len(built))
        def check(**kwargs):
            kwargs.pop('source_mode')
            if kwargs['kind'] == 'resolution' and not failed:
                failed.append(True)
                raise workflow.WorkflowError('assertion failed')
            return self.check(**kwargs)
        def model(workspace, prompt, timeout, *, read_only):
            if prompt.startswith('Repair the current candidate'):
                (workspace / 'code.txt').write_text('repaired')
                return {'exit_code': 0, 'timed_out': False, 'stdout': 'repaired', 'stderr': ''}
            return self.model(workspace, prompt, timeout, read_only=read_only)
        with patch.object(workflow, 'build_source_image', build):
            with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
                autopilot.run(self.task, self.issue, 'baseline', source_mode='image',
                              build_source_image=True, model=model, check=check)
        self.assertEqual(len(built), 2)
        self.assertNotEqual(built[0], built[1])

    def test_failed_verification_at_iteration_limit_cannot_publish(self):
        data = cli.read(self.task)
        data['limits']['iterations'] = 1
        cli.write(self.task, data)
        def check(**kwargs):
            if kwargs['kind'] == 'resolution':
                raise workflow.WorkflowError('test assertion failed')
            return self.check(**kwargs)
        published = []
        with self.assertRaisesRegex(autopilot.AutopilotError, 'iteration limit'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=check,
                          pr_repo='user/fork', publisher=lambda *a: published.append(a))
        self.assertEqual(published, [])
        self.assertEqual(cli.read(self.task)['status'], 'paused')
        self.assertTrue((self.task.parent / 'verification-failure-1.json').exists())

    def test_failed_independent_review_requires_repair_before_publish(self):
        reviews = []
        events = []
        def model(workspace, prompt, timeout, *, read_only):
            if prompt.startswith('Repair the current candidate'):
                self.assertIn('unsafe behavior remains', prompt)
                (workspace / 'code.txt').write_text('independently repaired')
                return {'exit_code':0, 'timed_out':False, 'stdout':'repaired', 'stderr':''}
            if 'Independently inspect the candidate' in prompt:
                reviews.append(True)
                if len(reviews) == 1:
                    return {'exit_code':0, 'timed_out':False,
                            'stdout':json.dumps({'status':'blocked','reason':'unsafe behavior remains'}), 'stderr':''}
            return self.model(workspace, prompt, timeout, read_only=read_only)
        result = autopilot.run(self.task, self.issue, 'image', model=model, check=self.check,
                               pr_repo='user/fork', pr_number=1,
                               merge=lambda *args: events.append('merge') or {'merged':True})
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(len(reviews), 2)
        self.assertEqual(events, ['merge'])

    def test_sandbox_unavailable_pauses_without_repair(self):
        def check(**kwargs):
            if kwargs['kind'] == 'resolution':
                report = self.task.parent / 'environment.json'
                cli.write(report, {'error':'Docker unavailable', 'cleanup_confirmed':False})
                raise workflow.WorkflowError(f'check failed; raw report saved at {report}')
            return self.check(**kwargs)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'environment unavailable'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=check)
        self.assertEqual(cli.read(self.task)['iterations'], 1)

    def test_credentials_are_not_committed(self):
        def model(workspace, prompt, timeout, *, read_only):
            result = self.model(workspace, prompt, timeout, read_only=read_only)
            if 'Implement this explicitly' in prompt:
                (workspace / '.env').write_text('secret value')
            return result
        with self.assertRaisesRegex(autopilot.AutopilotError, 'credential artifact'):
            autopilot.run(self.task, self.issue, 'image', model=model, check=self.check)
        self.assertTrue((self.workspace / '.env').exists())
        self.assertEqual(cli.sha(self.workspace, 'HEAD'), cli.read(self.task)['base_commit'])

    def test_interrupted_repair_preserves_worktree_and_never_publishes(self):
        def check(**kwargs):
            if kwargs['kind'] == 'resolution':
                raise workflow.WorkflowError('candidate assertion failed')
            return self.check(**kwargs)
        def model(workspace, prompt, timeout, *, read_only):
            if prompt.startswith('Repair the current candidate'):
                (workspace / 'partial.txt').write_text('partial work')
                return {'exit_code':None, 'timed_out':True, 'stdout':'', 'stderr':'timeout'}
            return self.model(workspace, prompt, timeout, read_only=read_only)
        published = []
        with self.assertRaisesRegex(autopilot.AutopilotError, 'model failed'):
            autopilot.run(self.task, self.issue, 'image', model=model, check=check,
                          pr_repo='user/fork', publisher=lambda *a: published.append(a))
        self.assertEqual(cli.read(self.task)['status'], 'paused')
        self.assertEqual(cli.read(self.task.parent / 'autopilot.json')['phase'], 'repairing')
        self.assertTrue((self.workspace / 'partial.txt').exists())
        self.assertEqual(published, [])

    def test_expired_budget_pauses_before_model_or_publish(self):
        data = cli.read(self.task)
        data['started_at'] = '2000-01-01T00:00:00+00:00'
        cli.write(self.task, data)
        with self.assertRaisesRegex(autopilot.AutopilotError, 'budget exhausted'):
            autopilot.run(self.task, self.issue, 'image', model=self.model, check=self.check)
        self.assertEqual(cli.read(self.task)['status'], 'paused')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
