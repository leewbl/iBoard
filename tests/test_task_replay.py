import argparse
from contextlib import redirect_stdout
from functools import partial
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workbench import (autonomous_run, autopilot, autopilot_full, cli, delivery, publish,
                       start, task_replay, task_replay_delivery, workflow)


class ReplayFixture(unittest.TestCase):
    predicate = 'published'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'source'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q', '-b', 'main')
        for key, value in (('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid'),
                           ('commit.gpgsign', 'false')):
            cli.git(self.repo, 'config', key, value)
        (self.repo / 'code').write_text('broken\n')
        (self.repo / 'check.js').write_text('// fixture command\n')
        self.original_base = self.commit('Original fault')
        issue = self.root / 'issue.txt'
        issue.write_text('The user-visible command displays BROKEN.')
        log = self.root / 'console.txt'
        log.write_text('Observed BROKEN on the original command.\n')
        run = subprocess.run

        def inspect_image(args, **options):
            if args == ['docker', 'image', 'inspect', 'fixture']:
                return subprocess.CompletedProcess(args, 0, b'[]', b'')
            return run(args, **options)

        with redirect_stdout(io.StringIO()), patch('workbench.start.shutil.which', return_value='/fixture'), \
                patch('workbench.start.subprocess.run', side_effect=inspect_image):
            info = start.start(task_id='fixture', repo=self.repo, issue_file=issue,
                               source='https://github.com/example/fork/issues/42',
                               kind='bug', surface='non-ui', iterations=2, seconds=180,
                               image='fixture', tasks_root=self.root / 'tasks',
                               workspaces_root=self.root / 'worktrees', pr_repo='example/fork',
                               criteria=[{'id': 'fixed', 'text': 'The original command displays FIXED.'}],
                               publish_only=self.predicate == 'published', prepare_only=True, log_files=[log],
                               required_ci_workflows=['CI'] if self.predicate == 'merged' else None)
        self.task = Path(info['task'])
        plan = autonomous_run.freeze(self.task, self.predicate, self.root / 'individual')
        manifest = self.root / 'manifest.json'
        cli.write(manifest, {'schema': cli.SCHEMA, 'parallelism': 1, 'interval': 1,
                             'members': [{key: plan[key] for key in ('plan', 'sha256')}]})
        group = autopilot_full.freeze(manifest, self.root / 'queue')
        self.approval = (group['plan'], group['sha256'])
        self.member = cli.read(Path(group['plan']))['members'][0]
        (self.repo / 'unrelated').write_text('new main\n')
        self.base = self.commit('Advance main')
        self.calls = []

        def publisher(*args):
            raise publish.PublishError('base changed since verification')

        def resume(task, *, run):
            return start.resume_task(task, run=partial(run, model=self.model, check=self.check, publisher=publisher))

        self.initial = autonomous_run.run(plan['plan'], plan['sha256'], once=True, resume=resume)
        self.assertEqual(self.initial['status'], 'blocked', self.initial)
        failed_check = self.task.with_name('verification-failure-1.json')
        self.assertIn('base changed since verification', self.initial['reason'],
                      failed_check.read_text()[:3000] if failed_check.exists() else self.initial['reason'])
        self.source_state = cli.read(self.task.with_name('autopilot.json'))
        self.original_digest = cli.digest(self.task)
        self.calls.clear()
        model = patch.object(autopilot, 'execute_model', side_effect=self.model)
        checks = patch.object(workflow, 'run_check', side_effect=self.check)
        for patcher in (model, checks):
            patcher.start()
            self.addCleanup(patcher.stop)

    def commit(self, message):
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', message)
        return cli.sha(self.repo, 'HEAD')

    def model(self, workspace, prompt, timeout, *, read_only):
        if 'Return only JSON:' in prompt:
            output = {'status': 'ready', 'criteria': [{'id': 'fixed', 'text': 'The original command displays FIXED.'}],
                      'reproduction': {'command': ['node', '/workspace/check.js'], 'marker': 'BROKEN', 'exit_code': 0},
                      'resolution': {'command': ['node', '/workspace/check.js'], 'marker': 'FIXED', 'exit_code': 0},
                      'tests': {'command': ['node', '/workspace/check.js', 'test'], 'marker': 'PASS', 'exit_code': 0}}
            self.calls.append('plan')
        elif not read_only:
            (workspace / 'code').write_text('fixed\n')
            output = {'status': 'ready'}
            self.calls.append('edit')
        elif 'actual baseline run' in prompt:
            output = {'status': 'ready', 'verdict': 'PASS'}
            self.calls.append('baseline-review')
        else:
            output = {'status': 'ready', 'verdict': 'PASS', 'assessments': [
                {'id': 'fixed', 'verdict': 'PASS', 'reason': 'The command printed FIXED.', 'evidence': ['R4']}],
                'impact': {'surface': 'fixture command', 'invariant': 'The command shows the correct state.',
                           'path': 'code', 'line': 1, 'observed': 'The check showed FIXED.', 'evidence': ['R4']}}
            self.calls.append('final-review')
        return {'exit_code': 0, 'timed_out': False, 'stdout': json.dumps(output), 'stderr': ''}

    def check(self, *, task, kind, image, command, timeout, expect_exit, marker, actor, description):
        data = cli.read(task)
        workspace = Path(data['repo'])
        head = cli.sha(workspace, 'HEAD')
        value = (workspace / 'code').read_text().strip()
        observed = value.upper() if len(command) == 2 else 'PASS' if value == 'fixed' else 'FAIL'
        self.calls.append((kind, actor, head))
        path = task.parent / f'check-{len(self.calls)}-{kind}.json'
        cli.write(path, {'kind': kind, 'actor': actor, 'commit': head, 'container_created': True,
                         'cleanup_confirmed': True, 'timed_out': False, 'exit_code': expect_exit,
                         'expected_exit': expect_exit, 'stdout': observed, 'expected_marker': marker,
                         'workspace': str(workspace), 'command': command, 'source_mode': 'workspace',
                         'source_verified_commit': head})
        if observed != marker:
            raise workflow.WorkflowError(f'expected {marker}, observed {observed}')
        cli.evidence_add(argparse.Namespace(file=str(path), kind=kind, commit=head, actor=actor,
                                            description=description, criterion=[]), data)
        cli.write(task, data)
        return {'report': str(path), 'commit': head, 'kind': kind}

    def verify(self, base=None, *, owner=None):
        return task_replay.verify(*self.approval, self.task, base or self.base,
                                  out=self.root / 'replay', owner=owner)


class TaskReplayTests(ReplayFixture):
    def test_queue_starts_reverification_and_preserves_the_original_contract(self):
        calls = []
        def main_at_new_base(repo, branch):
            self.assertEqual((repo, branch), ('example/fork', 'main'))
            calls.append(branch)
            return self.base

        class GitHub:
            branch = staticmethod(main_at_new_base)

        result = autopilot_full.run(*self.approval, once=True, reverify_stale_main=True,
                                   observe_base=partial(task_replay.observe, owner=GitHub()))
        self.assertEqual(result['status'], 'blocked')
        report = result['members'][0]['reverification']
        self.assertEqual(report['status'], 'verified', report)
        self.assertEqual(calls, ['main'] * 3)
        old = cli.read(self.task)
        child = Path(report['task'])
        record = cli.read(child)
        self.assertEqual(cli.digest(self.task), self.original_digest)
        self.assertNotEqual(report['candidate'], self.source_state['candidate_commit'])
        self.assertEqual(cli.git(record['repo'], 'show', 'HEAD:unrelated'), 'new main')
        self.assertEqual([call for call in self.calls if call in ('plan', 'edit')], [])
        self.assertEqual([call[2] for call in self.calls if isinstance(call, tuple) and call[0] == 'reproduction'],
                         [self.base, self.base])
        self.assertEqual({call[2] for call in self.calls if isinstance(call, tuple) and call[0] != 'reproduction'},
                         {report['candidate']})
        self.assertEqual({key: record[key] for key in ('criteria', 'limits', 'started_at', 'iterations')},
                         {key: old[key] for key in ('criteria', 'limits', 'started_at', 'iterations')})
        with self.assertRaisesRegex(publish.PublishError, 'verification-only'):
            publish.publish(child, 'example/fork', object())
        with self.assertRaisesRegex(publish.PublishError, 'verification-only'):
            publish.republish(child, 'example/fork', 1, report['origin']['head'], object())
        with self.assertRaisesRegex(delivery.DeliveryError, 'verification-only'):
            delivery.guarded_merge(child, 'example/fork', 1, object())
        with self.assertRaisesRegex(delivery.DeliveryError, 'verification-only'):
            delivery.close_verified_issue(child, 'example/fork', 1, object())

    def test_conflicting_main_does_not_start_validation_or_change_the_candidate(self):
        (self.repo / 'code').write_text('ambiguous change\n')
        new_base = self.commit('Edit the task line on main')
        report = self.verify(new_base)
        self.assertEqual(report['status'], 'blocked', report)
        self.assertIn('could not apply', report['reason'])
        self.assertEqual(self.calls, [])
        self.assertEqual(cli.digest(self.task), self.original_digest)
        self.assertFalse((self.root / 'replay' / 'task.json').exists())

    def test_hold_inherits_parent_control_and_pass_cannot_survive_receipt_tampering(self):
        autopilot_full.hold(*self.approval, reason='Hold replay admission')
        stopped = self.verify()
        self.assertEqual(stopped['status'], 'held')
        self.assertEqual(self.calls, [])
        autopilot_full.release(*self.approval)
        accepted = self.verify()
        self.assertEqual(accepted['status'], 'verified', accepted)
        old_calls = self.calls.copy()
        self.assertEqual(self.verify()['candidate'], accepted['candidate'])
        self.assertEqual(self.calls, old_calls)
        Path(accepted['patch']['path']).write_text('{}')
        refused = self.verify()
        self.assertEqual(refused['status'], 'blocked')
        self.assertEqual(self.calls, old_calls)

    def test_base_changed_during_verification_cannot_return_a_usable_verdict(self):
        bases = iter((self.base, 'a' * 40))

        class GitHub:
            def branch(self, repo, branch):
                return next(bases)

        refused = self.verify(owner=GitHub())
        self.assertEqual(refused['status'], 'blocked')
        self.assertIn('main changed after fresh verification', refused['reason'])
        self.assertNotIn('review', refused)
        self.assertEqual(cli.read(Path(refused['task']).with_name('autopilot.json'))['phase'], 'delivery')

class ReplayGitHub:
    branch_name = 'workbench/fixture'

    def __init__(self, base, old_head=None):
        self.base = base
        self.head = old_head
        self.number = 9 if old_head else None
        self.body = ''
        self.writes = []
        self.green = set()
        self.queries = []
        self.merged = False
        self.closed = False
        self.lose_merge_response = False

    def login(self):
        return 'example'

    def repository(self, repo):
        return {'fork': True, 'owner': {'login': 'example'}, 'default_branch': 'main'}

    def branch(self, repo, branch):
        return self.base

    def remote_head(self, repo, branch):
        return self.head

    def branch_info(self, repo, branch):
        return {'protected': False, 'commit': {'sha': self.base}}

    def pull(self, repo, number):
        if self.number is None or number != self.number:
            raise ValueError('unexpected PR number')
        checks = [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}] if self.head in self.green else []
        return {'number': number, 'headRefOid': self.head, 'headRefName': self.branch_name,
                'baseRefName': 'main', 'baseRefOid': self.base, 'isDraft': False,
                'isCrossRepository': False, 'state': 'MERGED' if self.merged else 'OPEN',
                'mergeStateStatus': 'CLEAN' if self.head in self.green else 'BLOCKED',
                'statusCheckRollup': checks}

    def pulls(self, repo, branch):
        if self.number is None or self.merged:
            return []
        return [{**self.pull(repo, self.number), 'headRepository': {'name': 'fork'},
                 'headRepositoryOwner': {'login': 'example'}, 'body': self.body}]

    def push_new(self, workspace, repo, branch, head):
        assert self.head is None and branch == self.branch_name
        self.writes.append(('push', head))
        self.head = head

    def push_rebased(self, workspace, repo, branch, old, head):
        assert self.head == old and branch == self.branch_name
        self.writes.append(('lease', old, head))
        self.head = head

    def create(self, repo, branch, ident, data):
        assert self.number is None and branch == self.branch_name
        self.number = 9
        self.writes.append(('create', self.number))
        self.body = data['_prepared_pr_body']
        return self.number

    def update_body(self, repo, number, body):
        assert self.number == number
        self.writes.append(('body', number))
        self.body = body

    def _call(self, *args):
        if args[0] == 'run':
            head = args[args.index('--commit') + 1]
            self.queries.append(head)
            if head not in self.green:
                return []
            return [{'databaseId': 123, 'workflowName': 'CI', 'headSha': head, 'headBranch': self.branch_name,
                     'event': 'pull_request', 'status': 'completed', 'conclusion': 'success'}]
        if args[0] == 'api' and '/jobs?' in args[1]:
            return {'total_count': 1, 'jobs': [{'id': 111, 'run_id': 123, 'status': 'completed',
                                               'conclusion': 'success'}]}
        if args[0] == 'api' and args[1].endswith('/runs/123'):
            return {'id': 123, 'run_attempt': 1, 'name': 'CI', 'head_sha': self.head,
                    'head_branch': self.branch_name, 'event': 'pull_request', 'status': 'completed',
                    'conclusion': 'success', 'pull_requests': [
                        {'number': self.number, 'base': {'ref': 'main', 'sha': self.base},
                         'head': {'ref': self.branch_name, 'sha': self.head}}]}
        raise AssertionError(args)

    def fast_forward(self, workspace, repo, base, head):
        assert self.base == base and self.head == head and head in self.green
        assert cli.git(workspace, 'merge-base', base, head) == base
        self.writes.append(('merge', head))
        self.merged = True
        self.base = head
        if self.lose_merge_response:
            raise delivery.DeliveryError('merge response lost after accepted write')

    def issue(self, repo, number):
        return {'number': number, 'state': 'closed' if self.closed else 'open'}

    def close_issue(self, repo, number):
        assert self.merged
        self.closed = True
        self.writes.append(('close', number))
        return self.issue(repo, number)


class ReplayPublishTests(ReplayFixture):
    def test_opt_in_creates_a_pr_for_the_fresh_candidate_without_merge_authority(self):
        github = ReplayGitHub(self.base)
        options = dict(once=True, deliver_replayed=True, observe_base=partial(task_replay.observe, owner=github),
                       advance_delivery=partial(task_replay_delivery.advance, github=github))
        result = autopilot_full.run(*self.approval, **options)
        self.assertEqual(result['status'], 'complete', result)
        member = result['members'][0]
        self.assertEqual(member['proof']['remote']['state'], 'OPEN')
        self.assertEqual(member['proof']['base'], self.base)
        self.assertNotEqual(member['proof']['candidate'], self.source_state['candidate_commit'])
        self.assertEqual(github.writes, [('push', member['proof']['candidate']), ('create', 9)])
        self.assertEqual(github.queries, [])
        self.assertEqual(autopilot_full.run(*self.approval, **options), result)
        self.assertEqual(len(github.writes), 2)


class ReplayMergeTests(ReplayFixture):
    predicate = 'merged'

    def setUp(self):
        super().setUp()
        prior = self.source_state['candidate_commit']
        self.source_state['published_pr'] = {'repo': 'example/fork', 'pr': 9, 'head': prior}
        cli.write(self.task.with_name('autopilot.json'), self.source_state)
        self.github = ReplayGitHub(self.base, prior)
        self.github.green.add(prior)

    def run_queue(self, *, ci=autopilot._wait_for_ci):
        return autopilot_full.run(*self.approval, once=True, deliver_replayed=True,
                                  observe_base=partial(task_replay.observe, owner=self.github),
                                  advance_delivery=partial(task_replay_delivery.advance, github=self.github, ci=ci))

    def test_updates_original_pr_then_waits_for_new_ci_and_merges_after_release(self):
        tick = self.run_queue()
        self.assertEqual(tick['status'], 'watching', tick)
        member = tick['members'][0]
        head = member['identity']['head']
        self.assertEqual(member['identity']['pr'], 9)
        self.assertEqual(self.github.writes, [('lease', self.source_state['candidate_commit'], head), ('body', 9)])
        self.assertEqual(self.github.queries, [head])
        self.assertEqual(cli.read(Path(member['reverification']['task']))['status'], 'paused')
        autopilot_full.hold(*self.approval, reason='Inspect the pending replay')
        self.github.green.add(head)
        self.assertEqual(self.run_queue()['status'], 'held')
        self.assertEqual(len(self.github.writes), 2)
        autopilot_full.release(*self.approval)
        complete = self.run_queue()
        self.assertEqual(complete['status'], 'complete', complete)
        self.assertEqual(complete['members'][0]['proof']['remote']['main'], head)
        self.assertTrue(complete['members'][0]['issue']['closed'])
        self.assertEqual(self.github.writes[-2:], [('merge', head), ('close', 42)])
        self.assertTrue(all(query == head for query in self.github.queries))
        self.assertEqual(self.run_queue()['status'], 'complete')
        self.assertEqual(len(self.github.writes), 4)
        self.assertEqual(cli.digest(self.task), self.original_digest)

    def test_refuses_pr_reset_by_someone_else(self):
        self.github.head = 'b' * 40
        tick = self.run_queue()
        self.assertEqual(tick['status'], 'blocked')
        self.assertIn('PR no longer matches', tick['members'][0]['reason'])
        self.assertEqual(self.github.writes, [])

    def test_refuses_to_merge_after_main_changes_during_ci_wait(self):
        waiting = self.run_queue()
        head = waiting['members'][0]['identity']['head']
        self.github.green.add(head)
        self.github.base = 'b' * 40
        tick = self.run_queue()
        self.assertEqual(tick['status'], 'blocked')
        self.assertIn('main moved', tick['members'][0]['reason'])
        self.assertEqual(len(self.github.writes), 2)

    def test_recovers_exact_merge_after_the_remote_response_is_lost(self):
        waiting = self.run_queue()
        head = waiting['members'][0]['identity']['head']
        self.github.green.add(head)
        self.github.lose_merge_response = True
        lost = self.run_queue()
        self.assertEqual(lost['status'], 'blocked')
        self.assertIn('response lost', lost['members'][0]['reason'])
        self.assertEqual(self.github.writes[-1], ('merge', head))
        recovered = self.run_queue()
        self.assertEqual(recovered['status'], 'complete', recovered)
        self.assertEqual(self.github.writes[-2:], [('merge', head), ('close', 42)])
        self.assertEqual(len(self.github.writes), 4)

    def test_retains_failed_new_head_ci_without_merging(self):
        waiting = self.run_queue()
        head = waiting['members'][0]['identity']['head']

        def fail_ci(task, repo, pr, current, **kwargs):
            self.assertEqual((repo, pr, current), ('example/fork', 9, head))
            raise autopilot.CIFailure({'workflow': 'CI', 'head': current, 'attempt': 1})

        blocked = self.run_queue(ci=fail_ci)['members'][0]
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(json.loads(Path(blocked['failure']).read_bytes())['head'], head)
        self.assertEqual(len(self.github.writes), 2)


if __name__ == '__main__':
    unittest.main()
