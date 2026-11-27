from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Barrier, Event
import unittest

from workbench import autonomous_run, autopilot_full, cli, run_control, workflow
from workbench.task_lock import task_lock


class AutopilotFullTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.repo = self.root / 'source'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        for key, value in (('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid'),
                           ('commit.gpgsign', 'false')):
            cli.git(self.repo, 'config', key, value)
        (self.repo / 'tracked').write_text('baseline\n')
        cli.git(self.repo, 'add', 'tracked')
        cli.git(self.repo, 'commit', '-qm', 'Fixture base')
        self.tasks = {}
        self.plans = []
        for name in ('one', 'two'):
            info = workflow.prepare(task_id=name, repo=self.repo, tasks_root=self.root / 'tasks',
                                    workspaces_root=self.root / 'worktrees', source='Selected task',
                                    kind='bug', surface='non-ui', iterations=2, seconds=120, implementer='fixture')
            task = Path(info['task'])
            self.tasks[name] = task
            for filename in ('issue.txt', 'input-base.txt'):
                task.with_name(filename).write_text(f'Repair case {name}\n')
            cli.write(task.with_name('input-stage.json'), {
                'schema': cli.SCHEMA, 'phase': 'ready',
                'base_digest': cli.digest(task.with_name('input-base.txt')),
                'request_digest': cli.digest(task.with_name('issue.txt')), 'input_digest': None,
                'run_settings': {'image': 'fixture', 'pr_repo': 'alice/fork', 'source_mode': 'workspace',
                                 'candidate_image': None, 'build_source_image': False, 'memory_mb': 2048},
            })
            record = cli.read(task)
            record.update(status='paused', criteria=[{'id': 'fixed', 'text': f'Case {name} works.', 'revision': 1}])
            cli.write(task, record)
            frozen = autonomous_run.freeze(task, 'merged', self.root / f'plan-{name}')
            self.plans.append({key: frozen[key] for key in ('plan', 'sha256')})

    def freeze(self, parallelism=2, *, members=None, out=None, interval=1):
        manifest = self.root / 'queue-input.json'
        cli.write(manifest, {'schema': cli.SCHEMA, 'parallelism': parallelism, 'interval': interval,
                            'members': self.plans if members is None else members})
        result = autopilot_full.freeze(manifest, out or self.root / 'queue')
        return Path(result['plan']), result['sha256']

    def test_enrollment_is_idempotent_and_rejects_duplicate_or_overlapping_members(self):
        with self.assertRaisesRegex(autopilot_full.QueueError, 'separate task records'):
            self.freeze(members=[self.plans[0]] * 2)
        self.assertIsNone(run_control.group(self.tasks['one']))
        with self.assertRaisesRegex(autopilot_full.QueueError, 'member task directories'):
            self.freeze(out=self.tasks['one'].parent)
        queue = self.freeze()
        self.assertEqual(self.freeze(), queue)
        for task in self.tasks.values():
            self.assertEqual(run_control.group(task), queue[0])
        with self.assertRaisesRegex(autopilot_full.QueueError, 'another queue'):
            self.freeze(out=self.root / 'second-queue')

    def test_parallel_members_have_individual_results_and_pin_their_original_plans(self):
        queue = self.freeze()
        self.assertFalse(queue[0].with_name('group-run.json').exists())
        self.assertEqual([m['status'] for m in autopilot_full.show(*queue)['members']],
                         ['not-yet-observed'] * 2)
        barrier = Barrier(2, timeout=5)

        def drive(plan, digest, *, once):
            self.assertTrue(once)
            self.assertEqual(cli.digest(Path(plan)), digest)
            name = cli.read(Path(plan))['binding']['id']
            with task_lock(self.tasks[name], reentrant=False):
                barrier.wait()
                return {'status': 'complete', 'proof': {'member': name}}

        for _ in range(2):
            result = autopilot_full.run(*queue, once=True, drive=drive)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual([r['proof']['member'] for r in result['members']], ['one', 'two'])
        self.assertEqual(len(cli.read(Path(result['journal']))['events']), 2)
        self.assertEqual(autopilot_full.show(*queue)['members'], result['members'])

    def test_hold_blocks_new_tasks_and_individual_hold_survives_group_release(self):
        queue = self.freeze(parallelism=1)
        admitted, finish = Event(), Event()
        calls = []

        def drive(plan, digest, *, once):
            task = Path(cli.read(Path(plan))['task'])
            calls.append(task)
            with task_lock(task, reentrant=False):
                admitted.set()
                self.assertTrue(finish.wait(8))
                with self.assertRaises(run_control.Held):
                    run_control.admit(task)
            return {'status': 'held', 'request': run_control.read(task)['request']}

        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(autopilot_full.run, *queue, once=True, drive=drive)
            try:
                self.assertTrue(admitted.wait(5))
                group_hold = autopilot_full.hold(*queue, reason='Operator group hold')
                self.assertEqual(autopilot_full.show(*queue)['hold'], group_hold)
                self.assertEqual(run_control.read(self.tasks['two'])['request'], group_hold['request'])
                with self.assertRaisesRegex(autopilot_full.QueueError, 'finishing admitted'):
                    autopilot_full.release(*queue)
            finally:
                finish.set()
            result = running.result(timeout=10)
        self.assertEqual(result['status'], 'held')
        self.assertEqual(calls, [self.tasks['one']])
        self.assertEqual({m['request'] for m in result['members']}, {group_hold['request']})
        personal = run_control.hold(self.tasks['two'], 'Operator individual hold')
        autopilot_full.release(*queue)
        self.assertTrue(run_control.released_pause(self.tasks['one'], group_hold['request']))
        self.assertEqual(run_control.read(self.tasks['two']), personal)
        with self.assertRaises(run_control.Held):
            run_control.admit(self.tasks['two'])

    def test_writes_on_same_destination_are_serialized_and_stale_main_is_blocked(self):
        queue = self.freeze()
        both_ready = Barrier(2, timeout=5)
        frozen_base = cli.read(queue[0])['members'][0]['base']
        remote = {'main': frozen_base, 'merges': []}
        held_lane = Event()
        second_ready = Event()

        def drive(plan, digest, *, once):
            record = cli.read(Path(plan))
            name = record['binding']['id']
            both_ready.wait()
            if name == 'two':
                self.assertTrue(held_lane.wait(5))
                second_ready.set()
            with run_control.delivery_lane(record['task'], 'alice/FORK' if name == 'two' else 'Alice/fork'):
                if remote['main'] != frozen_base:
                    return {'status': 'blocked', 'reason': 'main changed since independent verification'}
                self.assertEqual(name, 'one')
                held_lane.set()
                self.assertTrue(second_ready.wait(5))
                remote['main'] = 'candidate-one'
                remote['merges'].append(name)
                return {'status': 'complete', 'proof': {'candidate': remote['main']}}

        result = autopilot_full.run(*queue, once=True, drive=drive)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual([row['status'] for row in result['members']], ['complete', 'blocked'])
        self.assertEqual(remote['merges'], ['one'])

    def test_modified_group_approval_fails_closed_for_queue_and_direct_task(self):
        queue = self.freeze()
        record = cli.read(queue[0])
        record['parallelism'] = 1
        cli.write(queue[0], record)
        with self.assertRaisesRegex(autopilot_full.QueueError, 'SHA-256'):
            autopilot_full.run(*queue, once=True, drive=lambda *a, **k: self.fail('unexpected dispatch'))
        with self.assertRaisesRegex(run_control.ControlError, 'invalid or incomplete'):
            run_control.admit(self.tasks['one'])

    def test_hold_wakes_queue_during_long_ci_poll_interval(self):
        queue = self.freeze(interval=3600)
        delays = []
        calls = []

        def sleep(seconds):
            delays.append(seconds)
            self.assertEqual(delays, [1])
            autopilot_full.hold(*queue, reason='Stop waiting for CI')

        def drive(plan, digest, *, once):
            calls.append(plan)
            return {'status': 'waiting-ci', 'identity': {'sha256': digest}}

        result = autopilot_full.run(*queue, drive=drive, sleep=sleep)
        self.assertEqual(result['status'], 'held')
        self.assertCountEqual(calls, [item['plan'] for item in self.plans])
        self.assertEqual([m['status'] for m in result['members']], ['held', 'held'])


if __name__ == '__main__':
    unittest.main()
