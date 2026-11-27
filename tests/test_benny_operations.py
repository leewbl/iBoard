import copy
from pathlib import Path
import tempfile
import time
import unittest

from workbench import benny_operations, cli
from workbench.benny_slack import SlackError


WORKSPACE = 'TBENNY'
SOURCE = 'CREPORTS'
OPERATIONS = 'GOPERATIONS'
IDENTITY = 'UBENNY'
REPOSITORY = 'example/repo'
PR = f'https://github.com/{REPOSITORY}/pull/27'


class OperationsFixture:
    def __init__(self):
        epoch = int(time.time())
        self.root = f'{epoch}.000001'
        self.reply_ts = f'{epoch}.000002'
        self.root_rows = []
        self.replies = []
        self.calls = []
        self.root_interrupted = False
        self.reply_interrupted = False
        self.update_interrupted = False
        self.root_unobserved = False
        self.reply_unobserved = False
        self.authorized = True

    def record(self, action, channel, value, deadline):
        self.calls.append((action, channel, value, deadline))
        if deadline <= time.monotonic():
            raise SlackError('operation exceeded the deadline')
        if channel != OPERATIONS:
            raise AssertionError('operation touched a channel other than the designated operations channel')

    def message(self, ts, text, **attributes):
        return {'type': 'message', 'channel': OPERATIONS, 'ts': ts, 'user': IDENTITY, 'text': text, **attributes}

    def check_team(self, workspace, deadline):
        self.record('team', OPERATIONS, workspace, deadline)
        if workspace != WORKSPACE:
            raise SlackError('wrong workspace')

    def identity(self, workspace, user, deadline):
        self.record('identity', OPERATIONS, (workspace, user), deadline)
        if not self.authorized or (workspace, user) != (WORKSPACE, IDENTITY):
            raise SlackError('writer is not authorized')

    def history(self, channel, *, oldest, deadline):
        self.record('history', channel, oldest, deadline)
        return copy.deepcopy(self.root_rows)

    def messages(self, channel, root, deadline):
        self.record('thread', channel, root, deadline)
        if root != self.root:
            raise SlackError('unknown root timestamp')
        return copy.deepcopy([*self.root_rows, *self.replies])

    def operations_root(self, *, channel, text, deadline):
        self.record('post-root', channel, text, deadline)
        if self.root_unobserved:
            raise SlackError('response and root are unobserved')
        self.root_rows.append(self.message(self.root, text))
        if self.root_interrupted:
            raise KeyboardInterrupt
        return self.root

    def update_status(self, *, channel, root, text, deadline):
        self.record('edit-root', channel, (root, text), deadline)
        if root != self.root:
            raise SlackError('unknown edit target')
        self.root_rows[0]['text'] = text
        if self.update_interrupted:
            raise KeyboardInterrupt
        return root

    def reply(self, *, channel, root, text, deadline):
        self.record('reply', channel, (root, text), deadline)
        if root != self.root or self.reply_unobserved:
            raise SlackError('response and reply are unobserved')
        self.replies.append(self.message(self.reply_ts, text, thread_ts=root))
        if self.reply_interrupted:
            raise KeyboardInterrupt
        return self.reply_ts

    def writes(self):
        return [row[0] for row in self.calls if row[0] in ('post-root', 'edit-root', 'reply')]


class BennyOperationsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'operations.json'
        self.slack = OperationsFixture()

    def operations(self, **changes):
        kwargs = {'workspace': WORKSPACE, 'source_channel': SOURCE, 'source_root': '1700000000.000001',
                  'channel': OPERATIONS, 'identity': IDENTITY, 'repository': REPOSITORY,
                  'slack': self.slack, 'writer': self.slack}
        return benny_operations.Operations(self.path, **(kwargs | changes))

    def test_updates_one_operations_root_and_posts_one_bound_draft_reply(self):
        run = self.operations()
        self.assertEqual(self.slack.calls, [])
        for status in ('Reproducing', 'Reproduced', 'Attempting bounded fix'):
            self.assertEqual(run.status(status), {'channel': OPERATIONS, 'root': self.slack.root, 'status': status})
        self.slack.calls.clear()
        start = time.monotonic()
        receipt = run.draft(PR)
        end = time.monotonic()
        self.assertEqual(receipt['draft'], {'url': PR, 'timestamp': self.slack.reply_ts})
        self.assertEqual(self.operations().draft(PR), receipt)
        self.assertEqual(self.slack.writes(), ['edit-root', 'reply'])
        self.assertEqual(len(self.slack.root_rows), 1)
        self.assertEqual(self.slack.replies[0]['thread_ts'], self.slack.root)
        self.assertEqual(self.slack.replies[0]['text'], f'Draft pull request opened. {PR}')
        deadlines = {call[-1] for call in self.slack.calls}
        self.assertEqual(len(deadlines), 1)
        self.assertTrue(start + benny_operations.SECONDS <= deadlines.pop() <= end + benny_operations.SECONDS)
        self.assertNotIn('intent', cli.read(self.path))

    def test_recovers_root_saved_by_slack_before_worker_exit(self):
        self.slack.root_interrupted = True
        with self.assertRaises(KeyboardInterrupt):
            self.operations().status('Reproducing')
        self.assertEqual(cli.read(self.path)['intent']['kind'], 'root')
        self.assertEqual(self.operations().status('Reproducing')['root'], self.slack.root)
        self.assertEqual(self.slack.writes(), ['post-root'])

    def test_missing_root_receipt_holds_writes_on_restart(self):
        self.slack.root_unobserved = True
        for _ in range(2):
            with self.assertRaisesRegex(SlackError, 'no unique receipt'):
                self.operations().status('Reproducing')
        self.assertEqual(self.slack.writes(), ['post-root'])

    def test_duplicate_root_markers_are_ambiguous_even_for_the_same_author(self):
        self.slack.root_interrupted = True
        with self.assertRaises(KeyboardInterrupt):
            self.operations().status('Reproducing')
        self.slack.root_rows.append(self.slack.message(self.slack.reply_ts, self.slack.root_rows[0]['text']))
        with self.assertRaisesRegex(SlackError, 'no unique receipt'):
            self.operations().status('Blocked')
        self.assertEqual(self.slack.writes(), ['post-root'])

    def test_reconciles_update_if_slack_already_changed_root_before_worker_exit(self):
        self.operations().status('Reproducing')
        self.slack.update_interrupted = True
        with self.assertRaises(KeyboardInterrupt):
            self.operations().status('Reproduced')
        self.assertEqual(cli.read(self.path)['intent']['kind'], 'update')
        self.assertEqual(self.operations().status('Reproduced')['status'], 'Reproduced')
        self.assertEqual(self.slack.writes(), ['post-root', 'edit-root'])

    def test_rejects_changed_root_author_or_body_before_update(self):
        run = self.operations()
        run.status('Reproducing')
        body = self.slack.root_rows[0]['text']
        for changes in ({'ts': self.slack.reply_ts}, {'user': 'USOMEONEELSE'}, {'text': 'Edited outside journal'}):
            self.slack.root_rows[0] = self.slack.message(self.slack.root, body) | changes
            with self.assertRaises(SlackError):
                run.status('Reproduced')
        self.assertEqual(self.slack.writes(), ['post-root'])

    def test_recovers_existing_draft_reply_after_interruption_before_receipt(self):
        self.operations().status('Attempting bounded fix')
        self.slack.reply_interrupted = True
        with self.assertRaises(KeyboardInterrupt):
            self.operations().draft(PR)
        self.assertEqual(cli.read(self.path)['intent']['kind'], 'draft')
        self.slack.replies[0]['text'] = f'Draft pull request opened. <{PR}>'
        self.assertEqual(self.operations().draft(PR)['draft']['timestamp'], self.slack.reply_ts)
        self.assertEqual(self.slack.writes(), ['post-root', 'edit-root', 'reply'])

    def test_unknown_draft_outcome_and_foreign_reply_do_not_create_a_second_post(self):
        self.operations().status('Attempting bounded fix')
        self.slack.reply_unobserved = True
        with self.assertRaisesRegex(SlackError, 'no unique receipt'):
            self.operations().draft(PR)
        self.slack.replies.append(self.slack.message(self.slack.reply_ts, f'Draft pull request opened. {PR}',
                                                    thread_ts=self.slack.root) | {'user': 'UIMPOSTOR'})
        with self.assertRaisesRegex(SlackError, 'exact Benny draft reply and author'):
            self.operations().draft(PR)
        with self.assertRaisesRegex(ValueError, 'different pull request'):
            self.operations().draft(PR.replace('/27', '/28'))
        self.assertEqual(self.slack.writes(), ['post-root', 'edit-root', 'reply'])

    def test_enforces_channel_workspace_writer_and_exact_pr_link(self):
        with self.assertRaisesRegex(ValueError, 'differ from the source'):
            self.operations(channel=SOURCE)
        with self.assertRaisesRegex(ValueError, 'exact pull request URL'):
            self.operations().draft('https://github.com/example/other/pull/1')
        with self.assertRaisesRegex(ValueError, 'exact pull request URL'):
            self.operations().draft(PR + '?redirect=example.org')
        with self.assertRaisesRegex(SlackError, 'wrong workspace'):
            self.operations(workspace='TOTHER').status('Reproducing')
        self.slack.authorized = False
        with self.assertRaisesRegex(SlackError, 'not authorized'):
            self.operations().status('Reproducing')
        self.assertEqual(self.slack.writes(), [])
        self.assertFalse(self.path.exists())

    def test_changed_retained_destination_blocks_further_reads_and_writes(self):
        self.operations().status('Reproducing')
        self.slack.calls.clear()
        with self.assertRaisesRegex(ValueError, 'different run or destination'):
            self.operations(channel='GOTHEROPS').status('Reproduced')
        self.assertEqual(self.slack.calls, [])


if __name__ == '__main__':
    unittest.main()
