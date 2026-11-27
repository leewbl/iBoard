from pathlib import Path
import unittest
from unittest.mock import patch

import test_benny
import test_benny_reproduce
from workbench import benny, benny_notice, cli
from workbench.benny_slack import SlackError


OPS = 'https://workspace.slack.com/archives/G123OPS/p1765221000000080'
ISSUE = 'https://github.com/leewbl/myopenclaw/issues/7'


class BennyNoticeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = test_benny_reproduce.BennyReproductionTests()
        self.addCleanup(self.runtime.doCleanups)
        self.runtime.setUp()
        self.source = self.runtime.source
        self.source.reply_behavior = 'accepted'
        self.path = self.runtime.root/'source-notice.json'

    def options(self, **overrides):
        receipt = self.runtime.receipt()
        return {'path': self.path, 'reproduction': {'path': str(receipt['receipt']), 'sha256': receipt['sha256']},
                'slack': self.source.slack, 'writer': self.source.writer, 'identity': test_benny.TRIAGE} | overrides

    def with_tracker(self):
        self.source.pages[0][1]['text'] = f'[benny:bug] tracker={ISSUE}'
        self.runtime.route = benny.freeze(rules=test_benny.RULES, slack=self.source.slack, channel=test_benny.CHANNEL,
            message_ts=test_benny.ROOT, out=self.runtime.root/'with-tracker')

    def test_only_the_confirmed_text_and_approved_links_go_to_the_source_thread(self):
        self.with_tracker()
        self.runtime.reproduce()
        options = self.options(operations_link=OPS)
        reference = benny_notice.publish(**options)
        self.assertEqual(benny_notice.publish(**options), reference)
        self.assertEqual(len(self.source.sent), 1)
        sent = self.source.sent[0]
        self.assertEqual((sent['channel'], sent['thread_ts']), (test_benny.CHANNEL, test_benny.ROOT))
        self.assertEqual(sent['text'].splitlines(), [benny_notice.SUMMARY, f'Operations {OPS}', f'Tracker {ISSUE}'])
        self.assertNotIn(self.source.root['text'], sent['text'])
        self.assertFalse(sent['unfurl_links'])
        row = benny_notice.confirmed(reference, reproduction=options['reproduction'],
                                     proof=cli.read(Path(options['reproduction']['path'])))
        self.assertEqual(row['text'], sent['text'])
        self.assertEqual(row['author'], test_benny.TRIAGE)

    def test_provider_markup_is_reconciled_after_interruptions_without_another_post(self):
        self.runtime.reproduce()
        options = self.options(operations_link=OPS)
        post = self.source.writer.reply

        def after_post(**kwargs):
            post(**kwargs)
            self.source.pages[-1][-1]['text'] = self.source.pages[-1][-1]['text'].replace(OPS, f'<{OPS}>')
            self.source.pages[-1].append(test_benny.reply(200, "I'll fix it"))
            raise KeyboardInterrupt

        with patch.object(self.source.writer, 'reply', side_effect=after_post):
            with self.assertRaises(KeyboardInterrupt):
                benny_notice.publish(**options)
        self.assertEqual(cli.read(self.path)['phase'], 'posting')
        reference = benny_notice.publish(**options)
        self.assertEqual(cli.digest(self.path), reference['sha256'])
        journal = cli.read(self.path)
        self.assertTrue(journal['source_changed_after_post'])
        self.assertIn(f'<{OPS}>', journal['reply']['text'])
        self.assertEqual(len(self.source.sent), 1)

    def test_lost_slack_response_is_confirmed_by_readback(self):
        self.runtime.reproduce()
        self.source.reply_behavior = 'lost'
        reference = benny_notice.publish(**self.options())
        self.assertEqual(reference['path'], str(self.path))
        self.assertEqual(len(self.source.sent), 1)
        self.assertEqual(cli.read(self.path)['phase'], 'confirmed')

    def test_missing_reply_receipt_preserves_intent_without_retrying_the_write(self):
        self.runtime.reproduce()
        self.source.reply_behavior = 'uncertain'
        with self.assertRaisesRegex(SlackError, 'no unique reply receipt'):
            benny_notice.publish(**self.options())
        self.assertEqual(cli.read(self.path)['phase'], 'posting')
        self.source.reply_behavior = 'accepted'
        with self.assertRaisesRegex(SlackError, 'no unique reply receipt'):
            benny_notice.publish(**self.options())
        self.assertEqual(len(self.source.sent), 1)

    def test_changed_source_and_wrong_identity_cannot_authorize_a_write(self):
        self.runtime.reproduce()
        with self.assertRaisesRegex(SlackError, 'configured identity'):
            benny_notice.publish(**self.options(identity='U123OTHER'))
        self.source.pages[0].append(test_benny.reply(2, 'The original steps were incomplete.'))
        with self.assertRaisesRegex(SlackError, 'source changed before reproduction notice'):
            benny_notice.publish(**self.options())
        self.assertFalse(self.source.sent)
        self.assertFalse(self.path.exists())

    def test_unconfirmed_reproduction_does_not_open_a_slack_write(self):
        self.runtime.runtime.states.clear()
        self.assertEqual(self.runtime.reproduce()['status'], 'not-reproduced')
        with self.assertRaisesRegex(ValueError, 'confirmed, unchanged reproduction'):
            benny_notice.publish(**self.options())
        self.assertFalse(self.source.sent)

    def test_invalid_operations_links_and_alternate_journal_paths_are_rejected(self):
        self.runtime.reproduce()
        bad_links = (test_benny.LINK, OPS.replace('workspace.slack.com', 'outside.slack.com'), OPS + '?tag=outside')
        for value in bad_links:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'operations-thread permalink'):
                benny_notice.publish(**self.options(operations_link=value))
        with self.assertRaisesRegex(ValueError, 'original event journal'):
            benny_notice.publish(**self.options(path=self.path.with_name('other-journal.json')))
        self.assertFalse(self.source.sent)

    def test_modified_receipt_is_not_accepted_even_with_a_new_digest(self):
        self.runtime.reproduce()
        options = self.options()
        proof = cli.read(Path(options['reproduction']['path']))
        reference = benny_notice.publish(**options)
        record = cli.read(self.path)
        record['reply']['author'] = 'U123OTHER'
        cli.write(self.path, record)
        with self.assertRaisesRegex(ValueError, 'differs from the approved digest'):
            benny_notice.confirmed(reference, reproduction=options['reproduction'], proof=proof)
        with self.assertRaisesRegex(ValueError, 'does not match the confirmed reply'):
            benny_notice.confirmed({**reference, 'sha256': cli.digest(self.path)},
                                   reproduction=options['reproduction'], proof=proof)
