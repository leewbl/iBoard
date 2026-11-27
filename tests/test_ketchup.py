from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from workbench import ketchup, pickup


class KetchupTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.session = self.root / 'chat.jsonl'
        self.manifest = self.root / 'manifest.json'
        self.start = datetime(2026, 1, 2, tzinfo=timezone.utc)
        self.messages = [
            ('assistant', 'Should we keep the evidence for a week or a month?'),
            ('user', 'Check the draft pull request too.'),
            ('user', '<task-notification>One worker finished.</task-notification>'),
            ('assistant', 'The current draft is ready to inspect.'),
            ('user', '/ketchup-video'),
            ('user', '<command-name>/agent-workbench:ketchup</command-name>'),
            ('user', 'This message came after the call.'),
        ]

    def at(self, second):
        return (self.start + timedelta(seconds=second)).isoformat()

    def invoke(self, spec, **kwargs):
        self.session.write_text(''.join(json.dumps({
            'cwd': str(self.repo), 'sessionId': 'catch-up', 'uuid': str(index),
            'timestamp': self.at(index), 'type': role, 'message': {'role': role, 'content': content},
        }) + '\n' for index, (role, content) in enumerate(self.messages)))
        self.manifest.write_text(json.dumps(spec))
        return ketchup.collect(repo=self.repo, transcript=self.session, invocation_line=6,
                               manifest=self.manifest, **kwargs)

    def test_prior_waiting_decision_and_current_pr_state_are_independent_of_message_scope(self):
        artifact = self.root / 'retention.json'
        artifact.write_text(json.dumps({'at': self.at(0), 'current': 'One week'}))
        evidence = {'kind': 'data', 'path': str(artifact), 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    'time_field': ['at'], 'facts': {'policy': ['current']}, 'earlier_for_decision': True}
        pending = {'question': 'How long should evidence stay available?', 'evidence': ['E1'],
                   'options': [{'name': 'One week', 'gain': 'Less storage', 'cost': 'Shorter audit window'},
                               {'name': 'One month', 'gain': 'More audit time', 'cost': 'More storage'}],
                   'recommendation': 'One week'}
        reader = Mock()
        reader.pull.return_value = {'number': 5, 'state': 'OPEN', 'isDraft': True,
                                    'statusCheckRollup': [{'status': 'IN_PROGRESS'}]}
        result = self.invoke({'evidence': [evidence], 'pending': [pending],
                              'pull_requests': [{'repo': 'local/project', 'number': 5}]}, reader=reader)
        self.assertEqual(result['window']['last_user_line'], 2)
        self.assertEqual(result['window']['until'], self.at(5))
        self.assertEqual([row['line'] for row in result['window']['later_turn_context']], [4, 5])
        self.assertEqual(result['pending'], [pending])
        self.assertTrue(result['evidence']['E1']['decision_only'])
        self.assertEqual(result['evidence']['P1']['facts']['url'], 'https://github.com/local/project/pull/5')
        self.assertEqual(result['evidence']['P1']['checks'], {'pending': 1})
        reply = ketchup.render(result)
        self.assertIn('is an open draft. GitHub showed 1 pending.', reply)
        self.assertIn('How long should evidence stay available?', reply)
        self.assertIn('More audit time', reply)
        self.assertEqual(list(self.repo.iterdir()), [])
        self.assertEqual(len(reader.mock_calls), 1)
        reader.pull.assert_called_once_with('local/project', 5)

    def test_another_command_and_tool_reply_cannot_act_as_invocation(self):
        for fake in ('/ketchup-video', 'Please describe /ketchup', [
                {'type': 'tool_result', 'content': 'result'}, {'type': 'text', 'text': '/ketchup'}]):
            self.messages[5] = ('user', fake)
            with self.subTest(fake=fake), self.assertRaisesRegex(ValueError, 'actual ketchup invocation'):
                self.invoke({'evidence': []})

    def test_mismatched_remote_pr_is_reported_as_unknown(self):
        reader = Mock()
        reader.pull.return_value = {'number': 9, 'state': 'MERGED'}
        result = self.invoke({'evidence': [], 'pull_requests': [{'repo': 'local/project', 'number': 5}]}, reader=reader)
        self.assertEqual(result['evidence']['P1']['facts'], {'state': 'unknown'})
        self.assertIsNone(result['evidence']['P1']['checks'])

    def test_draft_and_absent_checks_do_not_imply_any_waiting_decision(self):
        reader = Mock()
        reader.pull.return_value = {'number': 5, 'state': 'OPEN', 'isDraft': True,
                                    'mergeStateStatus': 'CLEAN', 'statusCheckRollup': []}
        result = self.invoke({'evidence': [], 'pull_requests': [{'repo': 'local/project', 'number': 5}]}, reader=reader)
        self.assertEqual(ketchup.render(result), 'No decisions were identified for you.\n\n'
            '- [local/project#5](https://github.com/local/project/pull/5) is an open draft. GitHub showed no checks.')

    def test_task_events_are_bounded_before_the_limit_is_applied(self):
        data = {'events': [{'at': self.at(i), 'type': 'example', 'number': i} for i in range(50)]}
        self.assertEqual([event['number'] for event in pickup._timeline(data, self.at(1), self.at(5))], [2, 3, 4, 5])
