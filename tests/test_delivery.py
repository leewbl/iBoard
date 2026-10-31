import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, delivery, workflow
from test_runner import tiny_png


def enable_skip_policy(data):
    data['ci_skip_policy'] = 'openclaw-selected-v1'
    data['events'].append({'type': 'ci-skip-policy', 'mode': 'creation', 'policy': 'openclaw-selected-v1',
                           'source': 'task-creation', 'reason': 'explicit test configuration',
                           'base_commit': data['base_commit'],
                           'required_ci_workflows': list(data['required_ci_workflows'])})


class FakeGitHub:
    def __init__(self, base, head):
        self.base, self.head = base, head
        self.merged = False
        self.strict = True
        self.protected = True
        self.pushed = False
        self.ci = True
        self.no_checks = False
        self.change_head_after_first_read = False
        self.reads = 0
        self.issue_state = 'open'
        self.closed_issues = []
        self.run_status = 'success'
        self.run_missing = False
        self.run_detail_changes = {}
        self.run_reads = 0
        self.details_reads = 0
        self.jobs_response = {'total_count': 1, 'jobs': [
            {'id': 111, 'run_id': 123, 'status': 'completed', 'conclusion': 'success'}]}
        self.change_attempt_after_jobs = False

    def login(self): return 'alice'
    def repository(self, repo): return {'fork': True, 'owner': {'login': 'alice'}, 'default_branch': 'main'}
    def protection(self, repo, branch): return {'required_status_checks': {'strict': self.strict, 'contexts': ['test']}}
    def _call(self, *args):
        if args[0] == 'run':
            self.run_reads += 1
            if self.run_missing:
                return []
            return [{'databaseId': 123, 'workflowName': 'CI', 'headSha': self.head,
                     'headBranch': 'workbench/issue-1', 'event': 'pull_request',
                     'status': 'completed', 'conclusion': self.run_status}]
        if args[0] == 'api':
            if '/jobs?' in args[1]:
                return self.jobs_response
            self.details_reads += 1
            details = {'id': 123, 'run_attempt': 1, 'name': 'CI', 'head_sha': self.head,
                       'head_branch': 'workbench/issue-1', 'event': 'pull_request',
                       'status': 'completed', 'conclusion': self.run_status,
                       'pull_requests': [{'number': 1, 'base': {'ref': 'main', 'sha': self.base},
                                          'head': {'ref': 'workbench/issue-1', 'sha': self.head}}]}
            details.update(self.run_detail_changes)
            if self.change_attempt_after_jobs and self.details_reads > 1:
                details['run_attempt'] = 2
            return details
        raise AssertionError(args)
    def branch(self, repo, branch): return self.base
    def branch_info(self, repo, branch): return {'protected': self.protected, 'commit': {'sha': self.base}}
    def issue(self, repo, number): return {'number': number, 'state': self.issue_state}
    def close_issue(self, repo, number):
        self.closed_issues.append(number)
        self.issue_state = 'closed'
        return {'number': number, 'state': 'closed'}
    def fast_forward(self, workspace, repo, base, head):
        self.pushed = True
        self.merged = True
        self.base = head
    def pull(self, repo, number):
        self.reads += 1
        head = '0' * 40 if self.change_head_after_first_read and self.reads > 1 else self.head
        return {'state': 'MERGED' if self.merged else 'OPEN', 'isDraft': False,
                'isCrossRepository': False, 'headRefName': 'workbench/issue-1',
                'headRefOid': head, 'baseRefName': 'main', 'baseRefOid': self.base,
                'mergeStateStatus': 'CLEAN', 'statusCheckRollup': [] if self.no_checks else [{'status': 'COMPLETED',
                  'conclusion': 'SUCCESS' if self.ci else 'FAILURE'}]}
    def merge(self, repo, number, sha): self.merged = True


class FakeOpenClawGitHub(FakeGitHub):
    def __init__(self, base, head):
        super().__init__(base, head)
        self.log = '2026-01-01T00:00:00Z check-docs: skipped (selected=false)\n'
        self.ci_skip_name = 'check-docs'
        self.optional_pr = 1
        self.optional_run_conclusion = 'skipped'
        self.extra_optional_skip = False
        self.gate_conclusion = 'success'
        self.extra_job_conclusion = 'success'
        self.ci_job_conclusion = 'skipped'
        self.workflow_attempt = 1

    def pull(self, repo, number):
        result = super().pull(repo, number)
        if not self.merged:
            result['statusCheckRollup'] = [
                {'status': 'COMPLETED', 'conclusion': 'SUCCESS'},
                {'status': 'COMPLETED', 'conclusion': 'SKIPPED', 'name': self.ci_skip_name,
                 'workflowName': 'CI', 'detailsUrl': 'https://github.com/alice/project/actions/runs/123/job/111'},
                {'status': 'COMPLETED', 'conclusion': 'SKIPPED', 'name': 'auto-response',
                 'workflowName': 'Auto response', 'detailsUrl': 'https://github.com/alice/project/actions/runs/456/job/333'},
            ]
        return result

    def _call(self, *args):
        if args[0] == 'run':
            name = args[args.index('--workflow') + 1]
            run_id = 123 if name == 'CI' else 456
            return [{'databaseId': run_id, 'workflowName': name, 'headSha': self.head,
                     'headBranch': 'workbench/issue-1', 'event': 'pull_request' if name == 'CI' else 'pull_request_target', 'status': 'completed',
                     'conclusion': 'success' if name == 'CI' else self.optional_run_conclusion}]
        if args[0] == 'api':
            ci = '/runs/123' in args[1]
            run_id = 123 if ci else 456
            if '/jobs?' in args[1]:
                if ci:
                    jobs = [{'id': 111, 'run_id': 123, 'name': self.ci_skip_name,
                             'status': 'completed', 'conclusion': self.ci_job_conclusion},
                            {'id': 222, 'run_id': 123, 'name': 'openclaw/ci-gate',
                             'status': 'completed', 'conclusion': self.gate_conclusion,
                             'steps': [{'name': 'Verify selected CI lanes', 'status': 'completed',
                                        'conclusion': 'success'}]},
                            {'id': 224, 'run_id': 123, 'name': 'other', 'status': 'completed',
                             'conclusion': self.extra_job_conclusion}]
                else:
                    jobs = [{'id': 333, 'run_id': 456, 'name': 'auto-response',
                             'status': 'completed', 'conclusion': 'skipped'}]
                    if self.extra_optional_skip:
                        jobs.append({'id': 334, 'run_id': 456, 'name': 'unknown-optional-job',
                                     'status': 'completed', 'conclusion': 'skipped'})
                return {'total_count': len(jobs), 'jobs': jobs}
            return {'id': run_id, 'run_attempt': self.workflow_attempt,
                    'name': 'CI' if ci else 'Auto response', 'head_sha': self.head,
                    'path': '.github/workflows/ci.yml' if ci else '.github/workflows/auto-response.yml',
                    'head_branch': 'workbench/issue-1', 'event': 'pull_request' if ci else 'pull_request_target',
                    'status': 'completed', 'conclusion': 'success' if ci else self.optional_run_conclusion,
                    'pull_requests': [{'number': 1 if ci else self.optional_pr,
                        'base': {'ref': 'main', 'sha': self.base},
                        'head': {'ref': 'workbench/issue-1', 'sha': self.head}}]}
        raise AssertionError(args)

    def job_log(self, repo, job_id):
        return self.log


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        repo = root / 'repo'
        repo.mkdir()
        for command in (['init', '-q'], ['config', 'user.email', 't@example.invalid'],
                        ['config', 'user.name', 'Test']):
            subprocess.run(['git', '-C', str(repo), *command], check=True)
        (repo / 'app').write_text('before')
        for name in ('before', 'after'):
            (repo / name).write_text('#!/bin/sh\nexit 0\n')
        subprocess.run(['git', '-C', str(repo), 'add', 'app', 'before', 'after'], check=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'base'], check=True)
        result = workflow.prepare(task_id='issue-1', repo=repo, tasks_root=root / 'tasks',
                  workspaces_root=root / 'workspaces', source='https://github.com/alice/project/issues/42', kind='bug',
                  surface='non-ui', iterations=3, seconds=120, implementer='implementer')
        self.task, self.workspace = Path(result['task']), Path(result['workspace'])
        data = cli.read(self.task)
        data['required_ci_workflows'] = ['CI']
        cli.criterion_add(argparse.Namespace(id='c1', text='issue solved', reason='issue'), data)
        cli.write(self.task, data)
        def runner(path, image, command, timeout):
            return {'exit_code': 1 if command[0] == '/workspace/before' else 0,
                    'stdout': 'BUG' if command[0] == '/workspace/before' else 'PASS', 'stderr': '',
                    'workspace': str(Path(path).resolve()), 'image': image, 'command': command,
                    'container_created': True, 'cleanup_confirmed': True, 'timed_out': False}
        self.runner = runner
        for actor in ('implementer', 'verifier'):
            self.runcheck('reproduction', actor, 'before', 1, 'BUG')
        (self.workspace / 'app').write_text('fixed')
        subprocess.run(['git', '-C', str(self.workspace), 'add', 'app'], check=True)
        subprocess.run(['git', '-C', str(self.workspace), 'commit', '-qm', 'fix'], check=True)
        for kind, actor in [('resolution', 'implementer'), ('test-result', 'implementer'),
                            ('runtime-log', 'implementer'), ('resolution', 'verifier'),
                            ('test-result', 'verifier')]:
            self.runcheck(kind, actor, 'after', 0, 'PASS')
        data = cli.read(self.task)
        self.head = cli.sha(self.workspace, 'HEAD')
        report = self.task.parent / 'review.json'
        test_report = next(e for e in data['evidence'] if e['kind'] == 'test-result' and e['actor'] == 'verifier')
        report.write_text(json.dumps({'verdict': 'PASS', 'candidate_commit': self.head,
                          'baseline_commit': data['base_commit'], 'criteria': ['c1'],
                          'criterion_digest': cli.criteria_digest(data),
                          'assessments': [{'id': 'c1', 'verdict': 'PASS', 'reason': 'Observed test passed', 'evidence': ['R1']}],
                          'reports': {'R1': {'path': test_report['path'], 'sha256': test_report['sha256']}}}))
        cli.evidence_add(argparse.Namespace(file=str(report), kind='independent-review',
                   commit=self.head, actor='verifier', description='independent review', criterion=['c1']), data)
        cli.write(self.task, data)
        self.github = FakeGitHub(data['base_commit'], self.head)

    def tearDown(self): self.tmp.cleanup()

    def runcheck(self, kind, actor, command, code, marker):
        workflow.run_check(task=self.task, kind=kind, image='image', command=['/workspace/' + command],
                           timeout=10, expect_exit=code, marker=marker, actor=actor,
                           description='test', runner=self.runner)

    def test_good_evidence_and_strict_checks_permit_merge(self):
        result = delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertTrue(result['merged'])

    def test_protected_branch_without_strict_checks_fails_closed(self):
        self.github.strict = False
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.merged)

    def test_unprotected_personal_fork_uses_guarded_fast_forward(self):
        self.github.protected = False
        result = delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertTrue(result['merged'])
        self.assertTrue(self.github.pushed)

    def test_unprotected_branch_refuses_empty_rollup_even_if_ci_succeeded(self):
        self.github.protected = False
        self.github.no_checks = True
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_direct_delivery_rejects_missing_required_workflow_configuration(self):
        data = cli.read(self.task)
        del data['required_ci_workflows']
        cli.write(self.task, data)
        self.github.protected = False
        with self.assertRaisesRegex(delivery.DeliveryError, 'required_ci_workflows'):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_cli_entry_rejects_missing_ci_configuration(self):
        data = cli.read(self.task)
        del data['required_ci_workflows']
        cli.write(self.task, data)
        self.github.protected = False
        guarded_merge = delivery.guarded_merge
        with patch.object(delivery, 'guarded_merge', side_effect=lambda task, repo, pr:
                          guarded_merge(task, repo, pr, self.github)):
            with self.assertRaisesRegex(delivery.DeliveryError, 'required_ci_workflows'):
                delivery.main(['--task', str(self.task), '--repo', 'alice/project', '--pr', '1'])
        self.assertFalse(self.github.pushed)

    def test_unrelated_successful_check_cannot_substitute_for_required_ci(self):
        self.github.protected = False
        self.github.run_missing = True
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_required_ci_failure_cannot_be_hidden_by_successful_rollup(self):
        self.github.protected = False
        self.github.run_status = 'failure'
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_required_ci_must_match_pr_and_base(self):
        self.github.protected = False
        self.github.run_detail_changes = {'pull_requests': [{'number': 2,
            'base': {'ref': 'main', 'sha': self.github.base},
            'head': {'ref': 'workbench/issue-1', 'sha': self.head}}]}
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_required_ci_rejects_mismatched_attempt_or_head(self):
        self.github.protected = False
        for changes in ({'head_sha': 'b' * 40}, {'head_branch': 'other'},
                        {'run_attempt': 0}, {'run_attempt': 2}, {'event': 'push'}, {'name': 'other'},
                        {'id': 124}, {'conclusion': 'skipped'}):
            with self.subTest(changes=changes):
                self.github.run_detail_changes = changes
                with self.assertRaises(delivery.DeliveryError):
                    delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
                self.assertFalse(self.github.pushed)

    def test_skipped_or_neutral_checks_remain_blocked(self):
        self.github.protected = False
        original = self.github.pull
        for conclusion in ('SKIPPED', 'NEUTRAL'):
            with self.subTest(conclusion=conclusion):
                def pull(repo, number):
                    result = original(repo, number)
                    result['statusCheckRollup'][0]['conclusion'] = conclusion
                    return result
                self.github.pull = pull
                with self.assertRaises(delivery.DeliveryError):
                    delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
                self.assertFalse(self.github.pushed)

    def test_openclaw_policy_accepts_only_proven_unselected_lanes(self):
        data = cli.read(self.task)
        enable_skip_policy(data)
        cli.write(self.task, data)
        self.github = FakeOpenClawGitHub(data['base_commit'], self.head)
        self.github.protected = False
        with patch.object(delivery, '_openclaw_ci_blob_matches', return_value=True), patch.object(delivery, '_openclaw_workflow_blob_matches', return_value=True):
            result = delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertTrue(result['merged'])

    def test_openclaw_policy_proves_ci_gate_when_only_optional_workflow_skips(self):
        data = cli.read(self.task)
        enable_skip_policy(data)
        cli.write(self.task, data)
        self.github = FakeOpenClawGitHub(data['base_commit'], self.head)
        self.github.protected = False
        self.github.ci_job_conclusion = 'success'
        original = self.github.pull
        def pull(repo, number):
            result = original(repo, number)
            if result['state'] == 'OPEN':
                result['statusCheckRollup'][1] = {
                    'status': 'COMPLETED', 'conclusion': 'SUCCESS', 'name': 'openclaw/ci-gate',
                    'workflowName': 'CI', 'detailsUrl': 'https://github.com/alice/project/actions/runs/123/job/222'}
            return result
        self.github.pull = pull
        with patch.object(delivery, '_openclaw_ci_blob_matches', return_value=True), patch.object(delivery, '_openclaw_workflow_blob_matches', return_value=True):
            result = delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertTrue(result['merged'])

    def test_openclaw_policy_requires_both_identical_macos_lane_names_unselected(self):
        data = cli.read(self.task)
        enable_skip_policy(data)
        cli.write(self.task, data)
        self.github = FakeOpenClawGitHub(data['base_commit'], self.head)
        self.github.protected = False
        self.github.ci_skip_name = 'macos-swift (${{ matrix.phase }})'
        self.github.log = 'macos-swift: skipped (selected=false)\n'
        with patch.object(delivery, '_openclaw_ci_blob_matches', return_value=True), patch.object(delivery, '_openclaw_workflow_blob_matches', return_value=True):
            with self.assertRaises(delivery.DeliveryError):
                delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_openclaw_policy_rejects_other_run_url(self):
        data = cli.read(self.task)
        enable_skip_policy(data)
        cli.write(self.task, data)
        self.github = FakeOpenClawGitHub(data['base_commit'], self.head)
        self.github.protected = False
        original = self.github.pull
        def pull(repo, number):
            result = original(repo, number)
            result['statusCheckRollup'][1]['detailsUrl'] = 'https://github.com/other/project/actions/runs/123/job/111'
            return result
        self.github.pull = pull
        with patch.object(delivery, '_openclaw_ci_blob_matches', return_value=True), patch.object(delivery, '_openclaw_workflow_blob_matches', return_value=True):
            with self.assertRaises(delivery.DeliveryError):
                delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_openclaw_policy_rejects_skipped_ci_job_missing_from_rollup(self):
        data = cli.read(self.task)
        enable_skip_policy(data)
        cli.write(self.task, data)
        self.github = FakeOpenClawGitHub(data['base_commit'], self.head)
        self.github.protected = False
        original = self.github.pull
        def pull(repo, number):
            result = original(repo, number)
            result['statusCheckRollup'][1] = {
                'status': 'COMPLETED', 'conclusion': 'SUCCESS', 'name': 'openclaw/ci-gate',
                'workflowName': 'CI', 'detailsUrl': 'https://github.com/alice/project/actions/runs/123/job/222'}
            return result
        self.github.pull = pull
        with patch.object(delivery, '_openclaw_ci_blob_matches', return_value=True), patch.object(delivery, '_openclaw_workflow_blob_matches', return_value=True):
            with self.assertRaises(delivery.DeliveryError):
                delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_openclaw_policy_rejects_optional_skipped_job_missing_from_rollup(self):
        data = cli.read(self.task)
        enable_skip_policy(data)
        cli.write(self.task, data)
        self.github = FakeOpenClawGitHub(data['base_commit'], self.head)
        self.github.protected = False
        self.github.extra_optional_skip = True
        with patch.object(delivery, '_openclaw_ci_blob_matches', return_value=True), \
             patch.object(delivery, '_openclaw_workflow_blob_matches', return_value=True):
            with self.assertRaises(delivery.DeliveryError):
                delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_openclaw_policy_refuses_missing_or_invalid_evidence(self):
        data = cli.read(self.task)
        enable_skip_policy(data)
        cli.write(self.task, data)
        for field, value in [('log', 'check-docs: skipped (selected=true)\n'),
                             ('log', 'check-docs: skipped (selected=false)\ncheck-docs: skipped (selected=false)\n'),
                             ('log', ''), ('ci_skip_name', 'unknown-lane'), ('optional_pr', 2),
                             ('optional_run_conclusion', 'failure'), ('gate_conclusion', 'failure'),
                             ('extra_job_conclusion', 'failure'), ('workflow_attempt', 2)]:
            with self.subTest(field=field, value=value):
                self.github = FakeOpenClawGitHub(data['base_commit'], self.head)
                self.github.protected = False
                setattr(self.github, field, value)
                with patch.object(delivery, '_openclaw_ci_blob_matches', return_value=True), patch.object(delivery, '_openclaw_workflow_blob_matches', return_value=True):
                    with self.assertRaises(delivery.DeliveryError):
                        delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
                self.assertFalse(self.github.pushed)

    def test_openclaw_policy_never_accepts_unverified_workflow_revision(self):
        data = cli.read(self.task)
        enable_skip_policy(data)
        cli.write(self.task, data)
        self.github = FakeOpenClawGitHub(data['base_commit'], self.head)
        self.github.protected = False
        with patch.object(delivery, '_openclaw_ci_blob_matches', return_value=False):
            with self.assertRaises(delivery.DeliveryError):
                delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_openclaw_policy_rejects_changed_optional_workflow(self):
        data = cli.read(self.task)
        enable_skip_policy(data)
        cli.write(self.task, data)
        self.github = FakeOpenClawGitHub(data['base_commit'], self.head)
        self.github.protected = False
        with patch.object(delivery, '_openclaw_ci_blob_matches', return_value=True), \
             patch.object(delivery, '_openclaw_workflow_blob_matches', return_value=False):
            with self.assertRaises(delivery.DeliveryError):
                delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_openclaw_policy_still_rejects_neutral(self):
        data = cli.read(self.task)
        enable_skip_policy(data)
        cli.write(self.task, data)
        self.github = FakeOpenClawGitHub(data['base_commit'], self.head)
        self.github.protected = False
        original = self.github.pull
        def neutral(repo, number):
            result = original(repo, number)
            result['statusCheckRollup'][0]['conclusion'] = 'NEUTRAL'
            return result
        self.github.pull = neutral
        with patch.object(delivery, '_openclaw_ci_blob_matches', return_value=True), patch.object(delivery, '_openclaw_workflow_blob_matches', return_value=True):
            with self.assertRaises(delivery.DeliveryError):
                delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_openclaw_policy_is_opt_in(self):
        data = cli.read(self.task)
        self.github = FakeOpenClawGitHub(data['base_commit'], self.head)
        self.github.protected = False
        with patch.object(delivery, '_openclaw_ci_blob_matches', return_value=True), patch.object(delivery, '_openclaw_workflow_blob_matches', return_value=True):
            with self.assertRaises(delivery.DeliveryError):
                delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_openclaw_policy_requires_frozen_ci_blob_and_support_files(self):
        data = cli.read(self.task)
        blob = delivery._OPENCLAW_CI_BLOB
        same = subprocess.CompletedProcess([], 0, stdout=f'{blob}\n{blob}\n')
        unchanged = subprocess.CompletedProcess([], 0, stdout='')
        with patch.object(delivery.subprocess, 'run', side_effect=[same, unchanged]):
            self.assertTrue(delivery._openclaw_ci_blob_matches(data, self.head))
        changed = subprocess.CompletedProcess([], 1, stdout='')
        with patch.object(delivery.subprocess, 'run', side_effect=[same, changed]):
            self.assertFalse(delivery._openclaw_ci_blob_matches(data, self.head))
        altered = subprocess.CompletedProcess([], 0, stdout=f'{blob}\n{"0" * 40}\n')
        with patch.object(delivery.subprocess, 'run', return_value=altered):
            self.assertFalse(delivery._openclaw_ci_blob_matches(data, self.head))

    def test_required_ci_is_rechecked_before_push(self):
        self.github.protected = False
        original = self.github._call
        def changed(*args):
            result = original(*args)
            if args[0] == 'run' and self.github.run_reads >= 2:
                result[0]['conclusion'] = 'failure'
            return result
        self.github._call = changed
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_successful_workflow_cannot_hide_failed_job(self):
        self.github.protected = False
        self.github.jobs_response['jobs'][0]['conclusion'] = 'failure'
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_successful_rollup_cannot_hide_skipped_required_job(self):
        self.github.protected = False
        self.github.jobs_response['jobs'][0]['conclusion'] = 'skipped'
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_required_ci_rejects_unknown_cancelled_or_incomplete_jobs(self):
        self.github.protected = False
        for changes in ({'conclusion': 'cancelled'}, {'conclusion': 'neutral'},
                        {'conclusion': None}, {'status': 'in_progress'},
                        {'run_id': 999}, {'run_attempt': 2}):
            with self.subTest(changes=changes):
                self.github.jobs_response = {'total_count': 1, 'jobs': [
                    {'id': 111, 'run_id': 123, 'status': 'completed',
                     'conclusion': 'success', **changes}]}
                with self.assertRaises(delivery.DeliveryError):
                    delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
                self.assertFalse(self.github.pushed)

    def test_duplicate_job_ids_are_rejected(self):
        self.github.protected = False
        job = {'id': 111, 'run_id': 123, 'status': 'completed', 'conclusion': 'success'}
        self.github.jobs_response = {'total_count': 2, 'jobs': [job, dict(job)]}
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_missing_job_page_fails_closed(self):
        self.github.protected = False
        self.github.jobs_response['total_count'] = 101
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_failure_on_later_job_page_fails_closed(self):
        self.github.protected = False
        original = self.github._call
        def paged(*args):
            if args[0] == 'api' and '/jobs?' in args[1]:
                page = int(args[1].split('page=')[-1])
                ids = range(1, 101) if page == 1 else [101]
                return {'total_count': 101, 'jobs': [
                    {'id': i, 'run_id': 123, 'status': 'completed',
                     'conclusion': 'failure' if i == 101 else 'success'} for i in ids]}
            return original(*args)
        self.github._call = paged
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_attempt_change_during_job_enumeration_fails_closed(self):
        self.github.protected = False
        self.github.change_attempt_after_jobs = True
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_unprotected_branch_still_rejects_failing_checks(self):
        self.github.protected = False
        self.github.ci = False
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_main_becoming_protected_stops_indirect_delivery(self):
        self.github.protected = False
        reads = []
        def branch_info(repo, branch):
            reads.append(True)
            return {'protected': len(reads) > 1, 'commit': {'sha': self.github.base}}
        self.github.branch_info = branch_info
        with self.assertRaisesRegex(delivery.DeliveryError, 'protection or base changed'):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_budget_expiring_during_remote_checks_prevents_push(self):
        self.github.protected = False
        reads = []
        def branch_info(repo, branch):
            reads.append(True)
            if len(reads) == 2:
                data = cli.read(self.task)
                data['started_at'] = '2000-01-01T00:00:00+00:00'
                cli.write(self.task, data)
            return {'protected': False, 'commit': {'sha': self.github.base}}
        self.github.branch_info = branch_info
        with self.assertRaisesRegex(delivery.DeliveryError, 'budget changed before push'):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.pushed)

    def test_github_operations_cannot_start_after_deadline(self):
        github = delivery.GitHub()
        github._deadline = 0
        with self.assertRaisesRegex(delivery.DeliveryError, 'time limit'):
            github._bounded(60)

    def test_fast_forward_does_not_push_after_ancestry_check_uses_budget(self):
        with patch.object(delivery.time, 'monotonic', side_effect=[0.0, 2.0]), \
             patch.object(delivery.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            with self.assertRaisesRegex(delivery.DeliveryError, 'time limit reached before Git push'):
                delivery._fast_forward_push(self.workspace, '/unused/remote', 'a' * 40, 'b' * 40, timeout=1)
        self.assertEqual(run.call_count, 1)

    def test_changed_pr_after_push_does_not_report_success(self):
        self.github.protected = False
        old_fast_forward = self.github.fast_forward
        def moved(*args):
            old_fast_forward(*args)
            self.github.head = '0' * 40
        self.github.fast_forward = moved
        with self.assertRaisesRegex(delivery.DeliveryError, 'has not confirmed'):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertTrue(self.github.pushed)

    def test_real_git_push_preserves_changed_remote(self):
        remote = Path(self.tmp.name) / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        base = cli.read(self.task)['base_commit']
        subprocess.run(['git', '-C', str(self.workspace), 'push', '-q', str(remote),
                        f'{base}:refs/heads/main'], check=True)
        delivery._fast_forward_push(self.workspace, str(remote), base, self.head)
        remote_head = subprocess.check_output(['git', '--git-dir', str(remote), 'rev-parse',
                                               'refs/heads/main'], text=True).strip()
        self.assertEqual(remote_head, self.head)
        (self.workspace / 'app').write_text('second change')
        subprocess.run(['git', '-C', str(self.workspace), 'add', 'app'], check=True)
        subprocess.run(['git', '-C', str(self.workspace), 'commit', '-qm', 'second'], check=True)
        next_head = cli.sha(self.workspace, 'HEAD')
        with self.assertRaisesRegex(delivery.DeliveryError, 'push failed'):
            delivery._fast_forward_push(self.workspace, str(remote), base, next_head)
        self.assertEqual(subprocess.check_output(['git', '--git-dir', str(remote), 'rev-parse',
                                                  'refs/heads/main'], text=True).strip(), self.head)

    def test_closes_only_named_personal_issue_after_verified_merge(self):
        data = cli.read(self.task)
        data['source'] = 'https://github.com/alice/project/issues/42'
        cli.write(self.task, data)
        with self.assertRaisesRegex(delivery.DeliveryError, 'not confirmed merged'):
            delivery.close_verified_issue(self.task, 'alice/project', 1, self.github)
        self.assertEqual(self.github.closed_issues, [])
        self.github.merged = True
        result = delivery.close_verified_issue(self.task, 'alice/project', 1, self.github)
        self.assertEqual(result['issue'], 42)
        self.assertEqual(self.github.closed_issues, [42])
        self.assertTrue(delivery.close_verified_issue(self.task, 'alice/project', 1, self.github)['already_closed'])
        data['source'] = 'https://github.com/upstream/project/issues/42'
        cli.write(self.task, data)
        self.assertFalse(delivery.close_verified_issue(self.task, 'alice/project', 1, self.github)['closed'])
        self.assertEqual(self.github.closed_issues, [42])

    def test_issue_url_must_match_destination_exactly(self):
        for source in ('https://github.com/alice/project/issues/42?query=1',
                       'https://github.com.evil.test/alice/project/issues/42',
                       'https://github.com/alice/project/pull/42',
                       'https://github.com/alice/project/issues/0'):
            self.assertIsNone(delivery.own_issue_number(source, 'alice/project'))

    def test_changed_or_missing_original_issue_never_closes(self):
        data = cli.read(self.task)
        data['source'] = 'https://github.com/alice/project/issues/43'
        cli.write(self.task, data)
        self.github.merged = True
        with self.assertRaisesRegex(delivery.DeliveryError, 'original issue source'):
            delivery.close_verified_issue(self.task, 'alice/project', 1, self.github)
        self.assertEqual(self.github.closed_issues, [])

    def test_issue_close_stops_if_budget_expires_during_remote_checks(self):
        self.github.merged = True
        def issue(repo, number):
            data = cli.read(self.task)
            data['started_at'] = '2000-01-01T00:00:00+00:00'
            cli.write(self.task, data)
            return {'number': number, 'state': 'open'}
        self.github.issue = issue
        with self.assertRaisesRegex(delivery.DeliveryError, 'blocked before write'):
            delivery.close_verified_issue(self.task, 'alice/project', 1, self.github)
        self.assertEqual(self.github.closed_issues, [])

    def test_stale_head_fails_closed(self):
        self.github.change_head_after_first_read = True
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.merged)

    def test_failing_ci_and_other_owner_fail_closed(self):
        self.github.ci = False
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'other/project', 1, self.github)
        self.assertFalse(self.github.merged)

    def test_tampered_evidence_blocks(self):
        data = cli.read(self.task)
        Path(data['evidence'][0]['path']).write_text('tampered')
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.merged)

    def test_reviewer_must_independently_rerun_baseline(self):
        data = cli.read(self.task)
        data['evidence'] = [e for e in data['evidence']
                            if not (e['kind'] == 'reproduction' and e['actor'] == 'verifier')]
        cli.write(self.task, data)
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)

    def test_ui_review_requires_independent_screenshot_and_review_binding(self):
        data = cli.read(self.task)
        data['surface'] = 'ui'
        cli.write(self.task, data)
        image = tiny_png()
        def runner(path, image_name, command, timeout, *, memory_mb, screenshot_output):
            Path(screenshot_output).write_bytes(image)
            return {'exit_code': 0, 'stdout': 'PASS', 'stderr': '', 'workspace': str(Path(path).resolve()),
                    'image': image_name, 'command': command, 'container_created': True,
                    'cleanup_confirmed': True, 'timed_out': False,
                    'screenshot_path': str(Path(screenshot_output).resolve()),
                    'screenshot_sha256': hashlib.sha256(image).hexdigest()}
        result = workflow.run_check(task=self.task, kind='screenshot', image='image',
                                    command=['/workspace/after'], timeout=10, expect_exit=0, marker='PASS',
                                    actor='verifier', description='capture', runner=runner)
        with self.assertRaisesRegex(delivery.DeliveryError, 'independent PASS'):
            delivery._independent_verification(cli.read(self.task), self.head)
        data = cli.read(self.task)
        item = next(e for e in data['evidence'] if e['kind'] == 'independent-review')
        review = json.loads(Path(item['path']).read_text())
        review['screenshot_hashes'] = [cli.digest(result['screenshot'])]
        review['reports']['R2'] = {'path': result['report'], 'sha256': cli.digest(result['report'])}
        review['assessments'][0]['evidence'].append('R2')
        Path(item['path']).write_text(json.dumps(review))
        item['sha256'] = cli.digest(item['path'])
        cli.write(self.task, data)
        delivery._independent_verification(cli.read(self.task), self.head)
        self.assertFalse(self.github.merged)

    def test_superseded_review_cannot_satisfy_delivery(self):
        data = cli.read(self.task)
        review = next(e for e in data['evidence'] if e['kind'] == 'independent-review')
        review['superseded'] = True
        _, issues = cli.gate(data, self.head)
        self.assertTrue(any('independent-review' in issue for issue in issues))
        with self.assertRaisesRegex(delivery.DeliveryError, 'independent PASS'):
            delivery._independent_verification(data, self.head)

    def test_legacy_evidence_without_source_binding_is_rejected(self):
        data = cli.read(self.task)
        item = next(e for e in data['evidence'] if e['kind'] == 'test-result' and e['actor'] == 'verifier')
        path = Path(item['path'])
        report = json.loads(path.read_text())
        del report['source_verified_commit']
        path.write_text(json.dumps(report))
        item['sha256'] = cli.digest(path)
        cli.write(self.task, data)
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(self.task, 'alice/project', 1, self.github)
        self.assertFalse(self.github.merged)


if __name__ == '__main__':
    unittest.main()
