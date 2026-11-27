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

from workbench import autonomous_run, autopilot, autopilot_full, cli, delivery, publish, start, task_replay, workflow


class TaskReplayTests(unittest.TestCase):
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
            info = start.start(task_id='fixture', repo=self.repo, issue_file=issue, source='Selected bug',
                               kind='bug', surface='non-ui', iterations=2, seconds=180,
                               image='fixture', tasks_root=self.root / 'tasks',
                               workspaces_root=self.root / 'worktrees', pr_repo='example/fork',
                               criteria=[{'id': 'fixed', 'text': 'The original command displays FIXED.'}],
                               publish_only=True, prepare_only=True, log_files=[log])
        self.task = Path(info['task'])
        plan = autonomous_run.freeze(self.task, 'published', self.root / 'individual')
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


if __name__ == '__main__':
    unittest.main()
