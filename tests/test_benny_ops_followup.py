import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from test_benny_operations import IDENTITY, OPERATIONS, REPOSITORY, SOURCE, WORKSPACE, OperationsFixture
from workbench import benny_operations, benny_ops_evidence, benny_ops_followup, cli
from workbench.benny_slack import RetryAfter, SlackRejected


FACTS = {'R1': {'kind': 'reproduction', 'text': 'The independent probe reproduced the reported symptom twice.'},
         'R2': {'kind': 'verification', 'text': 'No repaired candidate has passed verification.'}}


class BennyOperationsFollowupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name).resolve()/'operations.json'
        self.slack = OperationsFixture()
        self.epoch = int(self.slack.root.split('.')[0])
        self.wall = self.epoch + .1
        self.clock = patch.object(benny_ops_followup.time, 'time', side_effect=lambda: self.wall)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        operations = benny_operations.Operations(
            self.path, workspace=WORKSPACE, source_channel=SOURCE, source_root=self.slack.root,
            channel=OPERATIONS, identity=IDENTITY, repository=REPOSITORY, slack=self.slack, writer=self.slack,
        )
        operations.status('Reproduced')
        self.operations = {'path': str(self.path), 'sha256': cli.digest(self.path)}
        self.outcome = {'status': 'reproduced', 'operations': self.operations}
        self.evidence = Mock(return_value=FACTS)
        evidence = patch.object(benny_ops_evidence, 'packet', self.evidence)
        evidence.start()
        self.addCleanup(evidence.stop)
        self.plan_path = self.path.with_name(benny_ops_followup.PLAN)
        self.plan = self.arm()
        self.wall += .8
        self.model = Mock(side_effect=self.choose)

    def answer(self, **fields):
        return {'exit_code': 0, 'timed_out': False, 'stdout': json.dumps({'status': 'ready', **fields})}

    def choose(self, workspace, prompt, remaining, **flags):
        self.assertTrue((workspace.is_dir() and not list(workspace.iterdir())))
        self.assertTrue(flags['read_only'] and flags['file_tools_only'] and flags['private_workspace'])
        self.assertGreater(remaining, 0)
        return self.answer(citations=[{'id': 'R2', 'quote': 'passed verification.'}])

    def arm(self):
        return benny_ops_followup.arm(self.plan_path, operations=self.operations, outcome=self.outcome, seconds=45)

    def post(self, microsecond, text, **attrs):
        stamp = f'{self.epoch}.{microsecond:06d}'
        row = self.slack.message(stamp, text, thread_ts=self.slack.root, **{'user': 'UHUMAN', **attrs})
        self.slack.replies.append(row)
        return stamp

    def step(self):
        return benny_ops_followup.step(self.plan, slack=self.slack, writer=self.slack, model=self.model)

    def test_answers_at_most_one_direct_question_at_a_time_with_whole_facts(self):
        self.post(150000, 'Did verification pass?')
        self.post(180000, '<@UBENNY> Did it pass?', bot_id='BOTHER')
        first = self.post(210000, '<@UBENNY> Did verification pass?')
        later = self.post(230000, 'Hey <@UBENNY>, where is the evidence?')
        self.slack.reply_ts = f'{self.epoch}.400000'
        state = self.step()
        self.model.assert_called_once()
        self.assertEqual(set(state['handled']), {first})
        self.assertEqual(state['handled'][first]['status'], 'confirmed')
        answer = self.slack.replies[-1]
        self.assertEqual(answer['thread_ts'], self.slack.root)
        self.assertIn(FACTS['R2']['text'], answer['text'])
        self.slack.reply_ts = f'{self.epoch}.410000'
        self.assertEqual(set(self.step()['handled']), {first, later})
        self.assertEqual(self.slack.writes(), ['post-root', 'reply', 'reply'])
        self.assertEqual(self.step()['status'], 'watching')
        self.assertEqual(self.model.call_count, 2)

    def test_stops_before_answering_an_earlier_unanswered_question(self):
        self.post(210000, '<@UBENNY> Did verification pass?')
        self.post(220000, 'Please stop Benny.')
        self.assertEqual(self.step()['status'], 'stopped')
        calls = list(self.slack.calls)
        self.assertEqual(self.step()['status'], 'stopped')
        self.assertEqual(self.slack.calls, calls)
        self.assertEqual(self.slack.writes(), ['post-root'])
        self.model.assert_not_called()

    def test_stop_arriving_during_model_selection_prevents_the_send(self):
        self.post(210000, '<@UBENNY> Did verification pass?')

        def model(*args, **kwargs):
            self.post(250000, '<@UBENNY> stop')
            return self.choose(*args, **kwargs)

        self.model.side_effect = model
        self.assertEqual(self.step()['status'], 'stopped')
        self.assertEqual(self.slack.writes(), ['post-root'])

    def test_resumes_confirmed_reply_when_worker_exits_before_receipt(self):
        question = self.post(210000, '<@UBENNY> Did verification pass?')
        self.slack.reply_ts = f'{self.epoch}.400000'
        self.slack.reply_interrupted = True
        with self.assertRaises(KeyboardInterrupt):
            self.step()
        self.assertEqual(self.step()['handled'][question]['status'], 'confirmed')
        self.assertEqual(self.slack.writes(), ['post-root', 'reply'])
        self.model.assert_called_once()

    def test_missing_reply_holds_without_resending_or_processing_another_question(self):
        self.post(210000, '<@UBENNY> Did verification pass?')
        self.post(220000, '<@UBENNY> Where is the evidence?')
        self.slack.reply_unobserved = True
        state = self.step()
        self.assertEqual(state['status'], 'reply-unconfirmed')
        self.post(300000, state['pending']['body'], user='UOTHER')
        self.assertEqual(self.step()['status'], 'reply-unconfirmed')
        self.assertEqual(self.slack.writes(), ['post-root', 'reply'])
        self.model.assert_called_once()

    def test_unsupported_model_claims_hold(self):
        self.post(210000, '<@UBENNY> Did verification pass?')
        self.model.side_effect = None
        self.model.return_value = self.answer(citations=[{'id': 'R1', 'quote': 'approved a fix'}])
        self.assertEqual(self.step()['status'], 'model-blocked')
        self.assertEqual(self.step()['status'], 'model-blocked')
        self.assertEqual(self.slack.writes(), ['post-root'])
        self.model.assert_called_once()

    def test_write_rejection_without_an_observed_reply_holds(self):
        self.post(210000, '<@UBENNY> Did verification pass?')
        with patch.object(self.slack, 'reply', side_effect=SlackRejected('denied')) as writer:
            self.assertEqual(self.step()['status'], 'reply-unconfirmed')
            self.assertEqual(self.step()['status'], 'reply-unconfirmed')
        writer.assert_called_once()

    def test_slack_write_error_reconciles_a_reply_received_by_the_server(self):
        question = self.post(210000, '<@UBENNY> Did verification pass?')
        self.slack.reply_ts = f'{self.epoch}.400000'
        real_reply = self.slack.reply

        def applied_then_rejected(**kwargs):
            real_reply(**kwargs)
            raise SlackRejected('Slack reported an error after applying the request')

        with patch.object(self.slack, 'reply', side_effect=applied_then_rejected) as writer:
            self.assertEqual(self.step()['handled'][question]['status'], 'confirmed')
            self.assertEqual(self.step()['status'], 'watching')
            writer.assert_called_once()

    def test_short_watch_step_defers_model_selection(self):
        self.post(210000, '<@UBENNY> Did verification pass?')
        result = benny_ops_followup.step(self.plan, slack=self.slack, writer=self.slack, seconds=1, model=self.model)
        self.assertEqual(result['status'], 'watching')
        self.assertEqual(result['handled'], {})
        self.model.assert_not_called()

    def test_rearm_keeps_window_and_expiry_avoids_all_network_calls(self):
        original = cli.read(self.plan_path)
        self.wall += 10
        self.assertEqual(self.arm(), self.plan)
        self.assertEqual(cli.read(self.plan_path), original)
        self.wall = original['expires_at']
        calls = list(self.slack.calls)
        self.assertEqual(self.step()['status'], 'expired')
        self.assertEqual(self.slack.calls, calls)
        self.model.assert_not_called()

    def test_root_change_withholds_replies(self):
        self.post(210000, '<@UBENNY> Did verification pass?')
        self.slack.root_rows[0]['text'] = 'Changed root'
        self.assertEqual(self.step()['status'], 'source-changed')
        self.assertEqual(self.slack.writes(), ['post-root'])
        self.model.assert_not_called()

    def test_evidence_changed_during_model_selection_withholds_reply(self):
        self.post(210000, '<@UBENNY> Did verification pass?')

        def choose_then_change(*args, **kwargs):
            self.evidence.return_value = {'R1': FACTS['R1']}
            return self.choose(*args, **kwargs)

        self.model.side_effect = choose_then_change
        self.assertEqual(self.step()['status'], 'source-changed')
        self.assertEqual(self.slack.writes(), ['post-root'])

    def test_rate_limit_returns_bounded_delay_without_posting(self):
        self.post(210000, '<@UBENNY> Did verification pass?')
        with patch.object(self.slack, 'messages', side_effect=RetryAfter(18)):
            result = self.step()
        self.assertEqual((result['status'], result['retry_after']), ('watching', 18))
        self.assertEqual(self.slack.writes(), ['post-root'])
        self.model.assert_not_called()
