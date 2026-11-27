from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, poteto_mode as mode


class PotetoModeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.request = self.root / 'request.txt'
        self.request.write_text('Check the committed skill and attach the approved result.\n')
        self.receipt = str(self.root / 'skill-check.json')
        self.checked = {'action': 'check', 'args': ['--repo', str(self.root), '--base', 'HEAD^', '--candidate', 'HEAD',
                                                  '--out', self.receipt], 'outputs': [self.receipt],
                        'expect': [{'path': self.receipt, 'field': ['status'], 'equals': 'passed'}]}
        self.attached = {'action': 'attach', 'args': ['--task', str(self.root / 'task.json'),
                        '--receipt', {'step': 1, 'path': self.receipt, 'value': 'path'},
                        '--sha256', {'step': 1, 'path': self.receipt, 'value': 'sha256'}]}

    def freeze(self, steps, playbook='authoring-a-skill'):
        choice = mode.select(request=self.request, playbook=playbook, out=self.root / f'{playbook}-selection.json')
        specification = self.root / 'manifest.json'
        specification.write_text(json.dumps({'playbook': playbook, 'seconds': 60, 'steps': steps}))
        plan = mode.freeze(selection=choice['path'], sha256=choice['sha256'],
                           manifest=specification, out=self.root / 'run')
        return {'plan': plan['path'], 'sha256': plan['sha256']}

    def execute(self, argv, out, err, seconds):
        out.write_text('ok')
        err.write_text('')
        if argv[3] == 'check':
            Path(self.receipt).write_text(json.dumps({'status': 'passed'}))
        return 0

    def test_every_upstream_playbook_has_loadable_native_argument_parsers(self):
        routes = mode.catalog()
        self.assertEqual(len(routes), 23)
        self.assertEqual(sum(route['source'].startswith(mode.PREFIX) for route in routes.values()), 22)
        actions = [action for route in routes.values() for action in route['actions'].values()]
        with ThreadPoolExecutor(max_workers=3) as workers:
            self.assertTrue(all(workers.map(mode._options, actions)))

    def test_approved_role_selection_unwraps_the_real_profile_response_shape(self):
        profile = self.root / 'profile.json'
        profile.write_text('{}')
        role = {'provider': 'glm', 'model': 'glm-test', 'effort': 'high'}
        response = {'answer': '{"playbook":"investigation","reason":"Explain without edits.","citations":["U1"]}',
                    'observed_model': 'glm-test', 'requested_effort': 'high'}
        with patch.object(mode.model_profiles, 'load', return_value={'routes': {'judgment-prose': role}}):
            selected = mode.select(request=self.request, profile=profile, profile_sha256=cli.digest(profile),
                                   out=self.root / 'selection.json', profile_reviewer=lambda *_: response)
        self.assertEqual(selected['playbook'], 'investigation')
        self.assertEqual(cli.read(selected['path'])['invocation']['observed_model'], 'glm-test')

    def test_model_selection_cannot_authorize_a_command(self):
        with self.assertRaisesRegex(ValueError, 'known playbook'):
            mode.select(request=self.request, model='gpt-test', out=self.root / 'selection.json',
                        reviewer=lambda *_: {'playbook': 'investigation', 'reason': 'Read', 'citations': ['U1'],
                                              'args': ['--pr-repo', 'different/project']})
        self.assertFalse((self.root / 'selection.json').exists())

    def test_cross_playbook_action_and_fixed_option_override_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'option names'):
            self.freeze([{'action': 'start', 'args': ['--kind=feature']}], playbook='bug-fix')
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            self.freeze([{'action': 'open', 'args': []}], playbook='investigation')

    def test_handoff_preserves_receipt_and_reruns_are_idempotent(self):
        approved = self.freeze([self.checked, self.attached])
        observed = []
        def capture(argv, *rest):
            observed.append(argv)
            return self.execute(argv, *rest)
        first = mode.run(**approved, once=True, execute=capture)
        self.assertEqual(first['status'], 'ready')
        self.assertEqual(first['stages'][0]['conditions'][0]['observed'], 'passed')
        final = mode.run(**approved, execute=capture)
        self.assertEqual(final['status'], 'steps-complete')
        self.assertIn(cli.digest(self.receipt), observed[1])
        self.assertIn(self.receipt, observed[1])
        self.assertEqual(mode.run(**approved, execute=capture), final)
        self.assertEqual(len(observed), 2)

    def test_changed_handoff_prevents_the_followup_from_starting(self):
        approved = self.freeze([self.checked, self.attached])
        mode.run(**approved, once=True, execute=self.execute)
        Path(self.receipt).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'earlier stage output changed'):
            mode.run(**approved, execute=lambda *_: self.fail('modified input reached the adapter'))

    def test_native_success_exit_does_not_overrule_disagreeing_receipt(self):
        approved = self.freeze([self.checked, self.attached])
        def bad(argv, *rest):
            value = self.execute(argv, *rest)
            Path(self.receipt).write_text('{"status":"failed"}')
            return value
        log = mode.run(**approved, execute=bad)
        self.assertEqual(log['status'], 'blocked')
        self.assertEqual(log['stages'][0]['reason'], 'native evidence did not satisfy status')
        self.assertEqual(len(log['stages']), 1)

    def test_interrupted_native_action_is_not_replayed_automatically(self):
        approved = self.freeze([self.checked])
        def interrupted(*_args):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            mode.run(**approved, execute=interrupted)
        self.assertEqual(mode.run(**approved, execute=lambda *_: self.fail('replayed action'))['status'], 'interrupted')
