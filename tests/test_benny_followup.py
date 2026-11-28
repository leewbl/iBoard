from datetime import datetime
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from test_benny import ROOT, TRIAGE, reply
import test_benny_handoff as handoff_tests
from workbench import benny_followup, cli


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.work = handoff_tests.HandoffTests()
        self.addCleanup(self.work.doCleanups)
        self.work.setUp()
        self.fixture = self.work.fixture
        self.model = Mock(return_value={'exit_code': 0, 'stdout': json.dumps({
            'status': 'ready', 'answer': 'See the issue linked by triage.',
            'citations': [{'id': 'T1', 'quote': 'https://github.com/owner/product/issues/1'}],
        })})

    def prepare(self, *, window=300):
        self.plan = self.work.prepare(followup_seconds=window)
        state = self.work.apply(self.plan)
        self.assertEqual(state['status'], 'confirmed')
        self.started = datetime.fromisoformat(state['confirmed_at']).timestamp()
        return state

    def add(self, number, text, **extra):
        row = reply(number, text, **extra)
        self.fixture.pages[-1].append(row)
        return row

    def run_watch(self, **extra):
        inputs = dict(plan=self.plan['plan'], sha256=self.plan['sha256'], slack=self.fixture.slack,
                      writer=self.fixture.writer, tracker=self.work.tracker, model=self.model,
                      seconds=3, poll_seconds=10, wall=lambda: self.started + 1)
        return benny_followup.watch(**(inputs | extra))

    def test_direct_question_posts_grounded_reply_once_and_ignores_side_chatter(self):
        self.prepare()
        self.add(120, 'Does anyone have an update?')
        self.add(121, f'<@{TRIAGE}>, where is the issue?', user='U123BOT', bot_id='B123BOT')
        question = self.add(122, f'<@{TRIAGE}>, where is the issue? Give the exact link.')
        state = self.run_watch()
        self.assertEqual(state['handled'][question['ts']]['status'], 'confirmed')
        self.assertEqual(len(self.fixture.sent), 2)
        self.assertEqual(self.fixture.sent[-1]['thread_ts'], ROOT)
        self.assertNotIn('[benny:', self.fixture.sent[-1]['text'])
        self.assertIn(question['ts'].replace('.', ''), self.fixture.sent[-1]['text'])
        self.assertEqual(len(state['model_runs'][question['ts']]['prompt_sha256']), 64)
        self.assertEqual(self.run_watch(), state)
        self.model.assert_called_once()

    def test_reporter_correction_appends_exact_field_and_leaves_original_issue_unchanged(self):
        self.prepare()
        issue = self.work.tracker.items[1].copy()
        self.add(120, f'<@{TRIAGE}> Correction: Environment: Windows', user='U123OTHER')
        correct = self.add(121, f'<@{TRIAGE}> 更正：环境：macOS 15')
        state = self.run_watch()
        self.assertEqual(state['handled'][correct['ts']]['field'], 'Environment')
        self.assertEqual(state['handled'][correct['ts']]['status'], 'confirmed')
        self.assertIn('macOS 15', self.work.tracker.entries[0]['body'])
        self.assertNotIn('Windows', self.work.tracker.entries[0]['body'])
        self.assertEqual(self.work.tracker.items[1], issue)
        self.assertEqual(len(self.fixture.sent), 1)
        self.run_watch()
        self.assertEqual(len(self.work.tracker.entries), 1)
        self.model.assert_not_called()

    def test_lost_issue_comment_response_is_recovered_without_duplicate_correction(self):
        self.prepare()
        self.work.tracker.failure = 'lost-comment'
        row = self.add(120, f'<@{TRIAGE}> Correction: Version: 1.2.3')
        state = self.run_watch()
        self.assertEqual(state['handled'][row['ts']]['status'], 'confirmed')
        self.assertEqual(self.run_watch(), state)
        self.assertEqual(len(self.work.tracker.entries), 1)

    def test_unknown_slack_send_is_only_reconciled_after_restarting(self):
        self.prepare()
        question = self.add(120, f'<@{TRIAGE}> Which issue did you file?')
        self.fixture.reply_behavior = 'uncertain'
        first = self.run_watch()
        self.assertEqual(first['status'], 'reply-unconfirmed')
        self.assertEqual(len(self.fixture.sent), 2)
        self.assertEqual(self.run_watch()['status'], 'reply-unconfirmed')
        self.add(121, first['pending']['body'], user=TRIAGE)
        result = self.run_watch()
        self.assertEqual(result['handled'][question['ts']]['status'], 'confirmed')
        self.assertEqual(len(self.fixture.sent), 2)

    def test_stop_signal_preempts_queued_question_and_does_not_reopen(self):
        self.prepare()
        self.add(120, f'<@{TRIAGE}> Where is the issue?')
        self.add(121, 'Stop Benny.')
        self.assertEqual(self.run_watch()['status'], 'stopped')
        self.fixture.pages[-1].pop()
        self.assertEqual(self.run_watch()['status'], 'stopped')
        self.model.assert_not_called()
        self.assertEqual(len(self.fixture.sent), 1)

    def test_expiration_is_anchored_to_original_verdict_and_replay_cannot_extend_it(self):
        self.prepare(window=15)
        self.add(120, f'<@{TRIAGE}> Where is the issue?')
        state = self.run_watch(wall=lambda: self.started + 15)
        self.assertEqual(state['status'], 'expired')
        self.assertEqual(self.run_watch()['status'], 'expired')
        self.model.assert_not_called()

    def test_handoff_without_an_approved_window_rejects_followup(self):
        self.prepare(window=0)
        with self.assertRaisesRegex(ValueError, 'did not enable'):
            self.run_watch()

    def test_source_verdict_change_and_unsupported_model_answers_withhold_reply(self):
        state = self.prepare()
        verdict = next(row for row in self.fixture.pages[-1] if row['ts'] == state['slack_ts'])
        self.model.return_value = {'exit_code': 0, 'stdout': json.dumps({
            'status': 'ready', 'answer': 'Unsupported decision', 'citations': [{'id': 'R1', 'quote': 'invented'}],
        })}
        question = self.add(120, f'<@{TRIAGE}> Where is the issue?')
        self.assertEqual(self.run_watch()['handled'][question['ts']]['status'], 'no-grounded-answer')
        verdict['text'] += '\nEdited.'
        self.add(121, f'<@{TRIAGE}> What next?')
        self.assertEqual(self.run_watch()['status'], 'source-changed')
        self.assertEqual(len(self.fixture.sent), 1)

    def test_model_provider_error_stops_the_window_without_another_model_call(self):
        self.prepare()
        self.add(120, f'<@{TRIAGE}> Could you explain the tracker decision?')
        self.model.return_value = {'exit_code': 7, 'stderr': 'Provider quota.'}
        self.assertEqual(self.run_watch()['status'], 'model-blocked')
        self.assertEqual(self.run_watch()['status'], 'model-blocked')
        self.model.assert_called_once()
        self.assertEqual(len(self.fixture.sent), 1)

    def test_followup_state_is_bound_to_the_reviewed_plan_and_initial_verdict(self):
        self.prepare()
        self.run_watch()
        path = Path(self.plan['plan']).parent/'followup.json'
        saved = cli.read(path)
        saved['verdict_ts'] = '1765221000.000011'
        cli.write(path, saved)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.run_watch()
