import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from workbench import cli, run_control
from workbench.task_lock import task_lock


class RunControlTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.task = Path(directory.name) / 'task.json'
        cli.write(self.task, {'schema': cli.SCHEMA, 'id': 'case', 'status': 'running', 'base_commit': 'base'})

    def invoke(self, *arguments):
        return subprocess.run([sys.executable, '-m', 'workbench.run_control', *arguments,
                               '--task', str(self.task)], capture_output=True, text=True, timeout=15)

    def test_separate_operator_can_hold_an_active_task_but_cannot_release_it(self):
        with task_lock(self.task):
            run_control.admit(self.task)
            result = self.invoke('hold', '--reason', 'after current step')
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads(result.stdout)
            self.assertEqual(state['status'], 'held')
            status = self.invoke('show')
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)['next'], 'wait-for-admitted-step')
            result = self.invoke('release')
            self.assertEqual(result.returncode, 2)
            self.assertIn('still finishing', result.stderr)
            with self.assertRaisesRegex(run_control.ControlError, 'still finishing'):
                run_control.release(self.task)
            with self.assertRaises(run_control.Held):
                run_control.admit(self.task)
        result = self.invoke('release')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['request'], state['request'])
        run_control.admit(self.task)

    def test_hold_is_idempotent_and_does_not_edit_task_budget_or_status(self):
        original = self.task.read_bytes()
        first = run_control.hold(self.task, 'first request')
        self.assertEqual(run_control.hold(self.task, 'duplicate'), first)
        self.assertEqual(run_control.release(self.task)['status'], 'released')
        second = run_control.hold(self.task, 'another request')
        self.assertNotEqual(first['request'], second['request'])
        self.assertEqual(self.task.read_bytes(), original)

    def test_malformed_hold_fails_closed(self):
        self.task.with_name(run_control.CONTROL_RECORD).write_text('[]')
        with self.assertRaisesRegex(run_control.ControlError, 'could not be read'):
            run_control.admit(self.task)


if __name__ == '__main__':
    unittest.main()
