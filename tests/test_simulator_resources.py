import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench.simulator_resources import Simulator, SimulatorError


TEST_DEVICE = '11111111-1111-1111-1111-111111111111'
UNAVAILABLE_DEVICE = '22222222-2222-2222-2222-222222222222'
AVAILABLE_DEVICE = '33333333-3333-3333-3333-333333333333'
ACTIVE_DEVICE = '44444444-4444-4444-4444-444444444444'
PARENT_IMAGE = '55555555-5555-5555-5555-555555555555'
OLD_IMAGE = '66666666-6666-6666-6666-666666666666'
BUILTIN_IMAGE = '77777777-7777-7777-7777-777777777777'
RUNTIME = 'com.apple.CoreSimulator.SimRuntime.iOS-17-0'
OLD_RUNTIME = 'com.apple.CoreSimulator.SimRuntime.iOS-16-0'
BUILTIN_RUNTIME = 'com.apple.CoreSimulator.SimRuntime.iOS-18-0'

FIND = ('--find', 'simctl')
TESTING = ('simctl', '--set', 'testing', 'list', 'devices', '-j')
DEFAULT = ('simctl', 'list', 'devices', '-j')
IMAGES = ('simctl', 'runtime', 'list', '-j')


def device(ident, available=True, state='Shutdown'):
    return {'udid': ident, 'name': 'Fixture Simulator', 'state': state, 'isAvailable': available}


def image(ident, runtime, deletable=True, state='Ready'):
    return {'identifier': ident, 'runtimeIdentifier': runtime, 'name': 'Fixture image',
            'state': state, 'deletable': deletable, 'build': '23A11', 'sizeBytes': 120}


class SimulatorResourceTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.native_tool = Path(root.name) / 'simctl'
        self.native_tool.write_text('tool identity fixture')
        self.responses = {
            FIND: (str(self.native_tool) + '\n').encode(),
            TESTING: {'devices': {RUNTIME: [device(TEST_DEVICE), device(ACTIVE_DEVICE, state='Booted')]}},
            DEFAULT: {'devices': {RUNTIME: [device(UNAVAILABLE_DEVICE, available=False), device(AVAILABLE_DEVICE)]}},
            IMAGES: {ident: image(ident, runtime, deletable)
                     for ident, runtime, deletable in ((PARENT_IMAGE, RUNTIME, True), (OLD_IMAGE, OLD_RUNTIME, True),
                                                       (BUILTIN_IMAGE, BUILTIN_RUNTIME, False))},
        }
        self.simulator = Simulator()
        mock = patch.object(self.simulator, '_run', side_effect=self.output)
        self.run = mock.start()
        self.addCleanup(mock.stop)

    def output(self, *args, timeout=20):
        if 'delete' in args:
            return b''
        result = self.responses[args]
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, bytes) else json.dumps(result).encode()

    def rows(self, report=None):
        report = self.simulator.inventory() if report is None else report
        return {row['id']: row for row in report['resources']}

    def test_inventory_protects_running_available_dependent_and_builtin_resources(self):
        report = self.simulator.inventory()
        self.assertEqual(report['status'], 'available')
        self.assertEqual(report['tool'], str(self.native_tool.resolve()))
        self.assertEqual(report['tool_identity']['path'], report['tool'])
        self.assertEqual(set(report['collections'].values()), {'available'})
        rows = self.rows(report)
        self.assertEqual(rows[f'simulator:testing:{TEST_DEVICE}']['reasons'], [])
        self.assertIn('not shut down', rows[f'simulator:testing:{ACTIVE_DEVICE}']['reasons'][0])
        self.assertEqual(rows[f'simulator:unavailable:{UNAVAILABLE_DEVICE}']['reasons'], [])
        self.assertIn('is available', rows[f'simulator:unavailable:{AVAILABLE_DEVICE}']['reasons'][0])
        self.assertIn('devices still reference', rows[f'simulator:runtime:{PARENT_IMAGE}']['reasons'][0])
        self.assertIn('deletable', rows[f'simulator:runtime:{BUILTIN_IMAGE}']['reasons'][0])
        self.assertEqual(rows[f'simulator:runtime:{OLD_IMAGE}']['estimated_bytes'], 120)
        self.assertEqual(rows[f'simulator:runtime:{OLD_IMAGE}']['reasons'], [])

    def test_removal_invokes_exact_scope_and_native_id(self):
        rows = self.rows()
        for resource, command in (
            (f'simulator:testing:{TEST_DEVICE}', ('simctl', '--set', 'testing', 'delete', TEST_DEVICE)),
            (f'simulator:unavailable:{UNAVAILABLE_DEVICE}', ('simctl', 'delete', UNAVAILABLE_DEVICE)),
            (f'simulator:runtime:{OLD_IMAGE}', ('simctl', 'runtime', 'delete', OLD_IMAGE)),
        ):
            with self.subTest(resource=resource):
                self.run.reset_mock()
                self.simulator.remove(rows[resource])
                self.assertEqual(self.run.call_args.args, command)
                self.assertEqual(self.run.call_count, 2)  # Native rediscovery, then a single deletion.

    def test_removal_rejects_bulk_identifier_protected_row_and_changed_binary(self):
        rows = self.rows()
        candidate = rows[f'simulator:testing:{TEST_DEVICE}']
        old_tool = dict(candidate['identity']['tool'])
        candidates = [dict(candidate, id='simulator:testing:all'),
                      rows[f'simulator:testing:{ACTIVE_DEVICE}'],
                      dict(candidate, kind='simulator-runtime')]
        self.run.reset_mock()
        for row in candidates:
            with self.subTest(row=row):
                with self.assertRaisesRegex(SimulatorError, 'one identified'):
                    self.simulator.remove(row)
        self.run.assert_not_called()
        self.native_tool.write_text('replacement tool fixture with different size')
        self.assertEqual(candidate['identity']['tool'], old_tool)
        with self.assertRaisesRegex(SimulatorError, 'changed before deletion'):
            self.simulator.remove(candidate)
        self.assertEqual(self.run.call_args.args, FIND)

    def test_partial_collection_keeps_dependency_unknown_and_independent_device_eligible(self):
        self.responses[TESTING] = SimulatorError('testing set not found')
        report = self.simulator.inventory()
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['collections'], {'testing': 'unavailable', 'unavailable': 'available',
                                                  'runtime': 'available'})
        rows = self.rows(report)
        self.assertNotIn(f'simulator:testing:{TEST_DEVICE}', rows)
        self.assertIn('dependencies could not be verified', rows[f'simulator:runtime:{OLD_IMAGE}']['reasons'][0])
        self.assertEqual(rows[f'simulator:unavailable:{UNAVAILABLE_DEVICE}']['reasons'], [])

    def test_runtime_with_unidentified_default_dependencies_is_held(self):
        self.responses[DEFAULT] = {'devices': {'Unknown': [device(UNAVAILABLE_DEVICE, available=False)]}}
        rows = self.rows()
        self.assertIn('dependencies could not be verified', rows[f'simulator:runtime:{OLD_IMAGE}']['reasons'][0])

    def test_resources_remain_observable_during_state_changes(self):
        self.responses[DEFAULT]['devices'][RUNTIME][0]['isAvailable'] = True
        self.responses[IMAGES][OLD_IMAGE]['state'] = 'Deleting'
        rows = self.rows()
        self.assertTrue(rows[f'simulator:unavailable:{UNAVAILABLE_DEVICE}']['reasons'])
        self.assertEqual(rows[f'simulator:runtime:{OLD_IMAGE}']['state'], 'Deleting')
        self.assertTrue(rows[f'simulator:runtime:{OLD_IMAGE}']['reasons'])

    def test_malformed_or_duplicate_native_records_are_not_complete_observations(self):
        self.responses[TESTING]['devices'][RUNTIME].append(device(TEST_DEVICE))
        self.responses[IMAGES] = b'not a JSON result'
        report = self.simulator.inventory()
        self.assertEqual(report['status'], 'partial')
        self.assertIn('duplicate device', report['issues']['testing'])
        self.assertIn('valid JSON', report['issues']['runtime'])
        self.assertEqual(report['collections']['runtime'], 'unavailable')

    def test_tool_unavailable_cannot_authorize_cleanup(self):
        self.responses[FIND] = SimulatorError('tool missing')
        result = self.simulator.inventory()
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['tool'])
        self.assertEqual(set(result['collections'].values()), {'unavailable'})
        self.assertEqual(result['resources'], [])
        self.run.assert_called_once_with(*FIND)

    def test_tool_change_during_listing_invalidates_all_absence_checks(self):
        replacement = self.native_tool.with_name('next-simctl')
        replacement.write_text('replacement fixture')

        def output(*args, timeout=20):
            if args == IMAGES:
                self.responses[FIND] = str(replacement).encode()
            return self.output(*args, timeout=timeout)

        self.run.side_effect = output
        result = self.simulator.inventory()
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['tool_identity'])
        self.assertEqual(set(result['collections'].values()), {'unavailable'})
        self.assertEqual(result['resources'], [])
