from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import autonomous_run, autopilot, cli, run_control, workflow
from workbench.task_lock import task_lock


class AutonomousRunTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        repo = self.root / 'source'
        repo.mkdir()
        cli.git(repo, 'init', '-q')
        cli.git(repo, 'config', 'user.name', 'Fixture')
        cli.git(repo, 'config', 'user.email', 'fixture@example.invalid')
        cli.git(repo, 'config', 'commit.gpgsign', 'false')
        (repo / 'code').write_text('original')
        cli.git(repo, 'add', 'code')
        cli.git(repo, 'commit', '-qm', 'Fixture base')
        info = workflow.prepare(task_id='case', repo=repo, tasks_root=self.root/'tasks',
                                workspaces_root=self.root/'worktrees', source='Selected task',
                                kind='bug', surface='non-ui', iterations=3, seconds=300, implementer='fixture',
                                required_ci_workflows=['CI'])
        self.task = Path(info['task'])
        self.repo = Path(info['workspace'])
        self.data = cli.read(self.task)
        self.base = info['base_commit']
        self.destination = 'alice/fork'
        for name in ('issue.txt', 'input-base.txt'):
            self.task.with_name(name).write_text('Original task request')
        cli.write(self.task.with_name('input-stage.json'), {
            'schema': cli.SCHEMA, 'phase': 'ready', 'base_digest': cli.digest(self.task.with_name('input-base.txt')),
            'request_digest': cli.digest(self.task.with_name('issue.txt')), 'input_digest': None,
            'run_settings': {'image': 'fixture', 'pr_repo': self.destination, 'source_mode': 'workspace',
                             'candidate_image': None, 'build_source_image': False, 'memory_mb': 2048},
        })
        self.data['criteria'] = [{'id': 'fix', 'text': 'The original property holds.', 'revision': 1}]
        self.data['status'] = 'paused'
        cli.write(self.task, self.data)

    def freeze(self, intent='merged'):
        info = autonomous_run.freeze(self.task, intent, self.root/'supervise', interval=1)
        return Path(info['plan']), info['sha256']

    def test_changed_criteria_and_mismatched_outcome_cannot_launch(self):
        with self.assertRaisesRegex(autonomous_run.RunError, 'existing delivery authorization'):
            self.freeze('published')
        plan, digest = self.freeze()
        self.data['criteria'][0]['text'] = 'A weaker property.'
        cli.write(self.task, self.data)
        result = autonomous_run.run(plan, digest, once=True, resume=lambda *a, **k: self.fail('unexpected launch'))
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('acceptance conditions', result['reason'])
        with self.assertRaises(autonomous_run.RunError):
            autonomous_run.run(plan, '0' * 64, once=True)

    def test_hold_survives_restart_and_is_never_released_by_supervisor(self):
        plan, digest = self.freeze()
        first = run_control.hold(self.task, 'Operator pause')
        for _ in range(2):
            result = autonomous_run.run(plan, digest, once=True,
                                        resume=lambda *a, **k: self.fail('held task launched'))
            self.assertEqual(result['request'], first['request'])
            self.assertEqual(result['status'], 'held')
        self.assertEqual(cli.read(result['journal'])['events'][0]['status'], 'held')
        self.assertEqual(result['attempts'], 0)

    def test_task_lock_prevents_duplicate_launch(self):
        plan, digest = self.freeze()
        with task_lock(self.task):
            result = autonomous_run.run(plan, digest, once=True,
                                        resume=lambda *a, **k: self.fail('busy task was duplicated'))
        self.assertEqual((result['status'], result['attempts']), ('active', 0))

    def test_external_ci_wait_does_not_start_worker_until_ready(self):
        plan, digest = self.freeze()
        calls = []
        candidate = 'a' * 40
        pin = {'repo': self.destination, 'pr': 9, 'head': candidate, 'base': self.base}

        def resume(task, *, run):
            self.assertEqual(task, self.task)
            self.assertTrue(run.keywords['ci_once'])
            calls.append(task)
            state = {'schema': cli.SCHEMA, 'candidate_commit': candidate,
                     'published_pr': {key: pin[key] for key in ('repo', 'pr', 'head')}}
            state.update(phase='delivery', ci_wait=pin) if len(calls) == 1 else state.update(phase='complete')
            cli.write(task.with_name('autopilot.json'), state)

        def watch(task, repo, number, head, **options):
            self.assertEqual((task, repo, number, head), (self.task, self.destination, 9, candidate))
            self.assertTrue(options['observe'] and options['once'])
            return next(signals)

        def proof(plan, data, state, reader):
            return {'candidate': candidate} if state['phase'] == 'complete' else None

        signals = iter([False, False, True])
        with patch.object(autonomous_run.pickup, '_inspect', return_value={'execution': 'paused'}), \
             patch.object(autonomous_run, '_proof', side_effect=proof):
            results = [autonomous_run.run(plan, digest, once=True, resume=resume, observe_ci=watch)
                       for _ in range(4)]
        self.assertEqual([item['status'] for item in results], ['waiting-ci'] * 3 + ['complete'])
        self.assertEqual([item['attempts'] for item in results], [1, 1, 1, 2])
        self.assertEqual([e['status'] for e in cli.read(results[-1]['journal'])['events']],
                         ['resume', 'waiting-ci', 'resume', 'complete'])

    def test_recovers_owned_clean_local_checkpoint_and_refuses_dirty_rerun(self):
        plan, digest = self.freeze()
        journal = plan.with_name('run.json')
        cli.write(journal, {'schema': cli.SCHEMA, 'plan_sha256': digest, 'attempts': 1,
                            'in_flight': True, 'events': []})
        data = cli.read(self.task)
        data['status'] = 'running'
        cli.write(self.task, data)
        (self.repo / 'code').write_text('unrecorded change')
        stopped = autonomous_run.run(plan, digest, once=True, resume=lambda *a, **k: self.fail('dirty relaunch'))
        self.assertIn('outside its recorded checkpoint', stopped['reason'])
        self.assertEqual(cli.read(self.task)['status'], 'running')
        cli.git(self.repo, 'restore', 'code')
        # A different retained run with a witnessed launch can reconcile the clean baseline.
        journal.unlink()
        cli.write(journal, {'schema': cli.SCHEMA, 'plan_sha256': digest, 'attempts': 1,
                            'in_flight': True, 'events': []})
        with patch.object(autonomous_run, '_proof', side_effect=[None, {'candidate': self.base}]):
            def resume(task, *, run):
                self.assertEqual(cli.read(task)['status'], 'paused')
            recovered = autonomous_run.run(plan, digest, once=True, resume=resume)
        self.assertEqual((recovered['status'], recovered['attempts']), ('complete', 2))
        self.assertEqual([e['status'] for e in cli.read(journal)['events']], ['recovered', 'resume', 'complete'])


if __name__ == '__main__':
    unittest.main()
