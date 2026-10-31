import unittest
from datetime import datetime, timedelta, timezone

from workbench import autopilot, cli, delivery


class UnboundedBudgetTest(unittest.TestCase):
    def test_unbounded_task_never_exhausts_overall_budget(self):
        task = {
            'started_at': (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
            'status': 'running',
            'limits': {'seconds': None, 'iterations': None},
            'iterations': 1000,
        }
        self.assertEqual(cli.enforce_budget(task), [])
        self.assertFalse(cli.iteration_limit_reached(task))
        self.assertGreater(autopilot._remaining(task), 0)
        github = delivery.GitHub()
        github._deadline = 0
        github.set_task_deadline(task)
        self.assertEqual(github._bounded(60), 60)

    def test_independent_bounded_limits_are_still_enforced(self):
        task = {
            'started_at': (datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat(),
            'status': 'running',
            'limits': {'seconds': 1, 'iterations': None},
            'iterations': 1000,
        }
        self.assertIn('time limit reached', cli.enforce_budget(task))
        task['limits'] = {'seconds': None, 'iterations': 3}
        self.assertTrue(cli.iteration_limit_reached(task))
        self.assertIn('iteration limit exceeded', cli.enforce_budget(task))


if __name__ == '__main__':
    unittest.main()
