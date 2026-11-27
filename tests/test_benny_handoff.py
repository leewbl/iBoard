import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from test_benny import CHANNEL, Fixture, LINK, ROOT, RULES, reply
from test_benny_triage import EXPECTED, OBSERVED, TRACKER, issue, search_result
from workbench import benny, benny_handoff, benny_tracker, benny_triage, cli, delivery


class FakeTracker:
    actor = 'fixture-operator'

    def __init__(self):
        self.items = {}
        self.entries = []
        self.actions = []
        self.linked = []
        self.failure = None
        self.after_create = None

    def identity(self):
        return self.actor

    def preflight(self, repo, labels, *, create):
        self.actions.append(('preflight', repo, tuple(labels), create))
        if self.failure == 'no-compensation':
            raise delivery.DeliveryError('cannot compensate')

    def source_linked(self, repo, source):
        return self.linked

    def recent(self, repo, actor, marker):
        return next((item for item in self.items.values() if benny_tracker.owns(item, actor, marker)), None)

    def read(self, repo, number):
        return self.items[number].copy()

    def comments(self, repo, number):
        return [item for item in self.entries if item['number'] == number]

    def create(self, repo, title, body, labels):
        self.actions.append(('create', repo))
        if self.failure == 'not-observed':
            raise delivery.DeliveryError('tracker outcome unknown')
        number = 1 + max(self.items, default=0)
        row = {**issue(number, body), 'title': title, 'user': {'login': self.actor}, 'labels': list(labels)}
        self.items[number] = row
        if self.after_create:
            self.after_create()
        if self.failure == 'lost-create':
            raise delivery.DeliveryError('lost create response')
        return row.copy()

    def comment(self, repo, number, body):
        self.actions.append(('comment', repo, number))
        self.entries.append({'id': len(self.entries) + 1, 'number': number, 'body': body, 'user': {'login': self.actor}})
        if self.failure == 'lost-comment':
            raise delivery.DeliveryError('lost comment response')
        return self.entries[-1]

    def close(self, repo, number):
        self.actions.append(('close', repo, number))
        row = self.items[number]
        if self.failure != 'close-pending':
            row.update(state='closed', state_reason='not_planned')
        return row.copy()


class HandoffTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixture = Fixture(self.root)
        self.addCleanup(self.fixture.__exit__)
        self.fixture.__enter__()
        self.fixture.reply_behavior = 'accepted'
        self.fixture.root['text'] = OBSERVED + '\n' + EXPECTED
        self.tracker = FakeTracker()
        self.repo = self.root/'checkout'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        (self.repo/'labels.py').write_text('def rename_item(label):\n    return label\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'Keep selected label')

    def prepare(self, *, category='bug', duplicate=False, followup_seconds=0):
        frozen = benny.freeze(rules=RULES, slack=self.fixture.slack, channel=CHANNEL, message_ts=ROOT,
                              out=self.root/'frozen')
        result = {'status': 'ready', 'category': category, 'title': 'Rename clears the label', 'summary': OBSERVED,
                  'facts': dict.fromkeys(benny_triage.FACTS), 'match': None, 'question': None,
                  'trace': {'basis': 'code-path', 'finding': 'Rename returns its input.',
                            'citations': [{'id': 'S1', 'quote': 'return label'}]}}
        result['facts'].update(observed={'id': 'R1', 'quote': OBSERVED}, expected={'id': 'R1', 'quote': EXPECTED})
        search = Mock()
        search.search.return_value = search_result()
        if duplicate:
            self.tracker.items[7] = {**issue(7, OBSERVED), 'user': {'login': 'previous-author'}}
            search.search.return_value = search_result(self.tracker.items[7])
            result['match'] = {'id': 'T1', 'kind': 'confident-duplicate', 'report': {'id': 'R1', 'quote': OBSERVED},
                               'tracker': {'id': 'T1', 'quote': OBSERVED}}
        output = self.root/'triage'
        triage = benny_triage.triage(slack=self.fixture.slack, receipt=frozen['receipt'], sha256=frozen['sha256'],
                                    out=output, repo=self.repo, revision='HEAD', selections=('labels.py',),
                                    tracker_repo=TRACKER, terms=('rename label',), search_source=True, tracker=search,
                                    model=lambda *_a, **_k: {'exit_code': 0, 'stdout': json.dumps(result)})
        self.assertEqual(triage['status'], 'ready', triage.get('reason'))
        receipt = output/'result.json'
        return benny_handoff.prepare(triage=receipt, sha256=cli.digest(receipt), state_dir=self.root/'delivery',
                                     labels=('bug',) if not duplicate and category == 'bug' else (),
                                     followup_seconds=followup_seconds)

    def apply(self, plan):
        return benny_handoff.apply(plan=plan['plan'], sha256=plan['sha256'], slack=self.fixture.slack,
                                   writer=self.fixture.writer, tracker=self.tracker)

    def test_creates_one_issue_and_replies_only_under_source_with_a_trusted_marker(self):
        plan = self.prepare()
        self.assertEqual((self.tracker.items, self.fixture.sent), ({}, []))
        result = self.apply(plan)
        self.assertEqual(result['status'], 'confirmed', result.get('reason'))
        self.assertEqual(len(self.tracker.items), 1)
        self.assertIn(LINK, self.tracker.items[1]['body'])
        self.assertEqual(self.tracker.items[1]['labels'], ['bug'])
        self.assertEqual(self.fixture.sent[0]['thread_ts'], ROOT)
        self.assertFalse(self.fixture.sent[0]['unfurl_links'])
        self.assertTrue(self.fixture.sent[0]['text'].endswith('[benny:bug] tracker=https://github.com/owner/product/issues/1'))
        self.assertEqual(self.apply(plan), result)
        self.assertEqual(len(self.fixture.sent), 1)

    def test_duplicate_adds_source_comment_without_editing_the_existing_issue(self):
        self.fixture.reply_behavior = 'reject'
        plan = self.prepare(duplicate=True)
        original = self.tracker.items[7].copy()
        result = self.apply(plan)
        self.assertEqual(result['status'], 'reply-rejected')
        self.assertEqual(self.tracker.items, {7: original})
        self.assertEqual(len(self.tracker.entries), 1)
        self.assertIn(LINK, self.tracker.entries[0]['body'])
        self.assertFalse(any(step[0] == 'close' for step in self.tracker.actions))

    def test_rejected_reply_closes_and_rechecks_only_the_new_issue(self):
        self.tracker.items[7] = issue(7, 'Unrelated existing issue')
        self.fixture.reply_behavior = 'reject'
        plan = self.prepare()
        result = self.apply(plan)
        self.assertEqual(result['status'], 'compensated')
        self.assertEqual(self.tracker.items[7]['state'], 'open')
        self.assertEqual(self.tracker.items[8]['state_reason'], 'not_planned')
        self.assertEqual(len(self.fixture.sent), 1)

    def test_uncertain_slack_send_is_never_repeated_or_assumed_rejected(self):
        self.fixture.reply_behavior = 'uncertain'
        plan = self.prepare()
        self.assertEqual(self.apply(plan)['status'], 'reply-unconfirmed')
        self.assertEqual(self.apply(plan)['status'], 'reply-unconfirmed')
        self.assertEqual(len(self.fixture.sent), 1)
        self.assertEqual(self.tracker.items[1]['state'], 'open')
        self.fixture.pages[-1].append(reply(100, self.fixture.sent[0]['text'], user=self.fixture.user))
        self.assertEqual(self.apply(plan)['status'], 'confirmed')

    def test_lost_http_responses_recover_both_real_receipts_without_duplicate_writes(self):
        self.fixture.reply_behavior = 'lost'
        self.tracker.failure = 'lost-create'
        plan = self.prepare()
        self.assertEqual(self.apply(plan)['status'], 'confirmed')
        self.assertEqual(len(self.fixture.sent), 1)
        self.assertEqual(len(self.tracker.items), 1)

    def test_unknown_create_without_positive_evidence_never_creates_again(self):
        self.tracker.failure = 'not-observed'
        plan = self.prepare()
        self.assertEqual(self.apply(plan)['status'], 'issue-unconfirmed')
        self.tracker.failure = None
        self.assertEqual(self.apply(plan)['status'], 'issue-unconfirmed')
        self.assertEqual([step[0] for step in self.tracker.actions].count('create'), 1)
        self.assertEqual(self.fixture.sent, [])

    def test_source_drift_after_create_compensates_before_any_slack_write(self):
        self.tracker.after_create = lambda: self.fixture.pages[-1].append(reply(5, 'The symptoms have changed.'))
        plan = self.prepare()
        result = self.apply(plan)
        self.assertEqual(result['status'], 'compensated')
        self.assertEqual(result['compensation_reason'], 'source-changed')
        self.assertEqual(self.fixture.sent, [])

    def test_wrong_writer_and_no_compensation_permission_block_all_writes(self):
        plan = self.prepare()
        triage_identity = self.fixture.user
        self.fixture.user = 'U123WRONG'
        self.assertEqual(self.apply(plan)['status'], 'pending-review')
        self.assertEqual(self.tracker.actions, [])
        self.fixture.user = triage_identity
        self.tracker.failure = 'no-compensation'
        self.assertEqual(self.apply(plan)['status'], 'pending-review')
        self.assertFalse(self.tracker.items)
        self.assertFalse(self.fixture.sent)

    def test_feature_posts_a_single_other_verdict_without_a_tracker_write(self):
        plan = self.prepare(category='feature')
        self.assertEqual(self.apply(plan)['status'], 'confirmed')
        self.assertEqual(self.tracker.actions, [])
        self.assertTrue(self.fixture.sent[0]['text'].endswith('[benny:other]'))

    def test_failed_compensation_can_resume_without_resending_reply(self):
        self.fixture.reply_behavior = 'reject'
        self.tracker.failure = 'close-pending'
        plan = self.prepare()
        self.assertEqual(self.apply(plan)['status'], 'compensation-unconfirmed')
        self.tracker.failure = None
        self.fixture.team = 'T999OTHER'
        result = self.apply(plan)
        self.assertEqual(result['status'], 'compensated')
        self.assertEqual(len(self.fixture.sent), 1)
        self.assertEqual(len(self.tracker.items), 1)

    def test_changed_evidence_prevents_external_effects(self):
        plan = self.prepare()
        evidence = self.root/'triage/evidence.json'
        saved = evidence.read_bytes()
        evidence.write_bytes(saved + b'\n')
        self.assertEqual(self.apply(plan)['status'], 'source-changed')
        self.assertEqual((self.tracker.actions, self.fixture.sent), ([], []))

    def test_already_linked_source_does_not_create_another_issue_or_send_reply(self):
        plan = self.prepare()
        self.tracker.linked = [benny_tracker.issue_url(TRACKER, 27)]
        self.assertEqual(self.apply(plan)['status'], 'source-already-linked')
        self.assertEqual((self.tracker.items, self.fixture.sent), ({}, []))

    def test_gh_adapter_uses_explicit_repo_and_json_stdin_for_mutations(self):
        row = {**issue(3, 'Body'), 'user': {'login': 'actor'}}
        proc = subprocess.CompletedProcess([], 0, json.dumps(row), '')
        with patch('workbench.benny_tracker.subprocess.run', return_value=proc) as run:
            self.assertEqual(benny_tracker.Tracker().create(TRACKER, 'Title', 'Body', ['bug']), row)
        args, opts = run.call_args
        self.assertEqual(args[0], ['gh', 'api', '--hostname', 'github.com', '--method', 'POST',
                                  'repos/owner/product/issues', '--input', '-'])
        self.assertEqual(json.loads(opts['input']), {'title': 'Title', 'body': 'Body', 'labels': ['bug']})
