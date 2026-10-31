import unittest
from unittest.mock import patch
from workbench import autopilot, cli
from test_autopilot import AutopilotTests


class LocalPolicyFlowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = AutopilotTests(methodName='test_failed_reproduction_stops_before_modification')
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_guard_command_is_run_for_both_actors_after_reproduction(self):
        f = self.fixture
        data = cli.read(f.task)
        data['local_gate'] = {'mode': 'test'}
        cli.write(f.task, data)
        calls = []
        def check(**kwargs):
            calls.append((kwargs['kind'], kwargs['actor'], kwargs['command'], kwargs['image']))
            kwargs.pop('source_mode', None)
            return f.check(**kwargs)
        policy = {'command': ['node', 'fixed-vitest']}
        def record(task, head, image, results):
            d = cli.read(task)
            d['events'].append({'type': 'local-gate-run', 'head': head,
                                'reports': [{'path': r['report']} for r in results]})
            cli.write(task, d)
        with patch('workbench.local_gate.validate_configuration', return_value=policy), \
             patch('workbench.workflow._verified_image', return_value='image'), \
             patch('workbench.local_gate.record_run', side_effect=record):
            with self.assertRaisesRegex(autopilot.AutopilotError, 'PR must exist'):
                autopilot.run(f.task, f.issue, 'image', source_mode='image', model=f.model, check=check)
        selected = [item for item in calls if item[2] == ['node', 'fixed-vitest']]
        self.assertEqual([(c[0], c[1]) for c in selected], [('test-result', 'implementer'), ('test-result', 'independent-verifier')])
        self.assertEqual([item[0] for item in calls[:2]], ['reproduction', 'reproduction'])


if __name__ == '__main__': unittest.main()
