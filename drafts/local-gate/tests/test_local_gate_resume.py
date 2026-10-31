import subprocess
import unittest
from unittest.mock import patch
import test_delivery as fixtures
from workbench import cli, local_gate, start


class LocalGateResumeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DeliveryTests('test_good_evidence_and_strict_checks_permit_merge')
        self.f.setUp()
        self.task = self.f.task
        self.state = self.task.parent / 'autopilot.json'
        cli.write(self.state, {'schema': cli.SCHEMA, 'phase': 'delivery', 'candidate_commit': self.f.head})
        data = cli.read(self.task)
        data['status'] = 'paused'
        cli.write(self.task, data)
        self.mock = patch('workbench.local_vitest.command', return_value=['node', 'guard'])
        self.mock.start()

    def tearDown(self):
        self.mock.stop()
        self.f.tearDown()

    def select(self):
        return local_gate.configure(self.task, ['src/a.test.ts'], 'User selected local verification')

    def test_select_marks_request_without_changing_phase(self):
        self.select()
        self.assertEqual(cli.read(self.state)['phase'], 'delivery')
        request = cli.read(self.task)['local_gate_reverify']
        self.assertEqual(request['candidate'], self.f.head)
        self.assertTrue(local_gate.resume_reverify(self.task))

    def test_real_resume_passes_existing_reverify_option(self):
        self.select()
        stage = self.task.parent / 'input-stage.json'
        cli.write(stage, {'schema': cli.SCHEMA})
        settings = {'image': 'image', 'pr_repo': 'alice/project', 'source_mode': 'image',
                    'candidate_image': None, 'build_source_image': True, 'memory_mb': 4096}
        calls = []
        def run(*args, **kwargs):
            calls.append(kwargs)
            self.assertEqual(cli.read(self.state)['phase'], 'delivery')
            return {'phase': 'verify'}
        with patch('workbench.start._finish_screenshots', return_value={'run_settings': settings}):
            start.resume_task(self.task, run=run)
        self.assertTrue(calls[0]['reverify'])

    def test_rejects_dirty_or_changed_delivery_before_configuring(self):
        (self.f.workspace / 'app').write_text('uncommitted')
        with self.assertRaises(local_gate.LocalGateError): self.select()
        self.assertNotIn('local_gate', cli.read(self.task))

    def test_rejects_tampered_request_before_resume(self):
        self.select()
        data = cli.read(self.task)
        data['local_gate_reverify']['policy_digest'] = 'wrong'
        cli.write(self.task, data)
        with self.assertRaises(local_gate.LocalGateError): local_gate.resume_reverify(self.task)

    def test_candidate_changed_after_selection_is_rejected(self):
        self.select()
        (self.f.workspace / 'app').write_text('new candidate')
        subprocess.run(['git', '-C', str(self.f.workspace), 'add', 'app'], check=True)
        subprocess.run(['git', '-C', str(self.f.workspace), 'commit', '-qm', 'new candidate'], check=True)
        with self.assertRaises(local_gate.LocalGateError): local_gate.resume_reverify(self.task)

    def test_invalid_delivery_identity_is_rejected(self):
        state = cli.read(self.state)
        state['candidate_commit'] = 'wrong'
        cli.write(self.state, state)
        with self.assertRaises(local_gate.LocalGateError): self.select()
        self.assertNotIn('local_gate', cli.read(self.task))

    def test_interruption_after_transition_consumes_once(self):
        self.select()
        data = cli.read(self.task)
        data['events'].append({'type': 'reverify', 'at': cli.now()})
        cli.write(self.task, data)
        state = cli.read(self.state)
        state['phase'] = 'verify'
        cli.write(self.state, state)
        self.assertFalse(local_gate.resume_reverify(self.task))
        self.assertNotIn('local_gate_reverify', cli.read(self.task))
        self.assertFalse(local_gate.resume_reverify(self.task))

    def test_reverify_event_without_phase_transition_retries_safely(self):
        self.select()
        data = cli.read(self.task)
        data['events'].append({'type': 'reverify', 'at': cli.now()})
        cli.write(self.task, data)
        self.assertTrue(local_gate.resume_reverify(self.task))

    def test_completed_verification_consumes_without_repeating(self):
        self.select()
        data = cli.read(self.task)
        data['events'].append({'type': 'reverify', 'at': cli.now()})
        cli.write(self.task, data)
        with patch('workbench.local_gate.validate'):
            self.assertFalse(local_gate.resume_reverify(self.task))
        self.assertNotIn('local_gate_reverify', cli.read(self.task))
