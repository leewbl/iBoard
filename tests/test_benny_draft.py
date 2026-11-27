import argparse
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

import test_benny
import test_benny_repair
from workbench import benny, benny_draft, cli, delivery, publish, run_control


REPO = 'leewbl/benny-local-fixture'


class DraftRemote:
    def __init__(self, base):
        self.base = base
        self.head = None
        self.requests = []
        self.open = []
        self.fail = None

    def repository(self, repo):
        return {'full_name': REPO, 'owner': {'login': 'leewbl'}, 'fork': True, 'default_branch': 'main'}

    def login(self):
        return 'leewbl'

    def branch(self, repo, ref):
        return self.base

    def remote_head(self, repo, branch):
        return self.head

    def pulls(self, repo, branch, *, base, state):
        assert state == 'all'
        return self.open

    def push_new(self, workspace, repo, branch, head):
        self.requests.append('push')
        if self.head is not None:
            raise AssertionError('the simulated empty branch lease did not hold')
        self.head = head
        if self.fail == 'push-response':
            raise publish.PublishError('simulated missing branch response')

    def create(self, repo, branch, task_id, data, *, draft):
        self.requests.append('create')
        if self.fail != 'create-unobserved':
            self.open[:] = [{'number': 27, 'isDraft': draft, 'state': 'OPEN', 'title': data['_prepared_pr_title'],
                           'headRefName': branch, 'headRefOid': self.head, 'baseRefName': 'main',
                           'baseRefOid': self.base, 'isCrossRepository': False,
                           'headRepository': {'name': REPO.split('/')[1]}, 'headRepositoryOwner': {'login': 'leewbl'},
                           'body': data['_prepared_pr_body']}]
        if self.fail and self.fail.startswith('create-'):
            raise publish.PublishError('simulated missing PR creation response')
        return 27


class BennyDraftTests(unittest.TestCase):
    def setUp(self):
        helper = test_benny_repair.BennyRepairTests()
        self.addCleanup(helper.doCleanups)
        helper.setUp()
        source = helper.source
        self.source = source
        self.open = DraftRemote(source.base)
        cli.git(source.repo, 'remote', 'set-url', 'origin', f'https://github.com/{REPO}.git')
        source.source.pages[0][1]['text'] = f'[benny:bug] tracker=https://github.com/{REPO}/issues/11'
        source.route = benny.freeze(rules={**test_benny.RULES, 'repository': REPO}, slack=source.source.slack,
                                    channel=test_benny.CHANNEL, message_ts=test_benny.ROOT,
                                    out=source.root/'draft-source')
        source.reproduce(out=source.root/'draft-before')
        before = source.root/'draft-before/result.json'
        with patch.object(source, 'receipt', return_value={'receipt': before, 'sha256': cli.digest(before)}):
            self.task = task = helper.prepare()
            after = source.after()
            self.assertEqual(after['status'], 'verified')
        record = cli.read(task)
        cli.criterion_add(argparse.Namespace(id='fixed', text='Existing priority behavior is preserved.', reason='Fixture'), record)
        path = source.root/'after/result.json'
        proof = {'path': str(path), 'sha256': cli.digest(path)}
        record['benny_candidate'] = proof
        self.head = head = source.runtime.revision
        cli.evidence_add(argparse.Namespace(file=before, kind='reproduction', commit=source.base, actor='independent',
                                            description='Before', criterion=['fixed']), record)
        cli.git(record['repo'], 'merge', '--ff-only', head)
        self.review = task.with_name('independent.json')
        cli.write(self.review, {'benny_candidate': proof, 'causal_verification': {
            'explanation': 'Numeric coercion handles the empty value too early; check the missing value first.'},
            'impact': {'observed': 'Other inputs still accept the established valid values.'}})
        for kind, report in [('test-result', path), ('resolution', path), ('runtime-log', path),
                             ('independent-review', self.review)]:
            cli.evidence_add(argparse.Namespace(file=report, kind=kind, commit=head, actor='independent',
                                                description='After', criterion=['fixed']), record)
        cli.write(task, record)
        cli.write(task.with_name('autopilot.json'), {'schema': cli.SCHEMA, 'phase': 'delivery', 'candidate_commit': head})
        review = patch.object(delivery, '_independent_verification', return_value=record['evidence'][-1])
        review.start()
        self.addCleanup(review.stop)
        self.journal = task.with_name('benny-draft.json')

    def deliver(self):
        return benny_draft.deliver(task=self.task, slack=self.source.source.slack, tracker=self.source.tracker,
                                   github=self.open)

    def test_draft_uses_exact_fork_and_evidence_and_reconciles_without_more_writes(self):
        first = self.deliver()
        self.assertEqual((first['head'], first['pr'], first['status']), (self.head, 27, 'draft'))
        self.assertEqual(self.open.requests, ['push', 'create'])
        row = self.open.open[0]
        self.assertTrue(row['isDraft'])
        self.assertIn('Refs #11', row['body'])
        self.assertIn('Candidate replay SHA-256', row['body'])
        self.assertIn('Numeric coercion', row['body'])
        self.assertNotIn('slack.com', row['body'])
        self.assertNotIn(str(self.source.root), row['body'])
        self.assertEqual(self.deliver(), first)
        self.assertEqual(self.open.requests, ['push', 'create'])

    def test_source_claim_between_push_and_creation_stops_after_exact_branch(self):
        push = self.open.push_new

        def claim(*args):
            push(*args)
            self.source.source.pages[0].append(test_benny.reply(2, "I'll fix this."))

        self.open.push_new = claim
        with self.assertRaisesRegex(ValueError, 'source changed'):
            self.deliver()
        self.assertEqual(self.open.requests, ['push'])
        self.assertEqual(self.open.head, self.head)

    def test_new_claim_during_remote_lookup_blocks_first_write(self):
        real = self.open.pulls
        calls = 0

        def pulls(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.source.source.pages[0].append(test_benny.reply(2, "I'll fix this."))
            return real(*args, **kwargs)

        self.open.pulls = pulls
        with self.assertRaisesRegex(ValueError, 'source changed'):
            self.deliver()
        self.assertEqual(self.open.requests, [])

    def test_closed_draft_with_deleted_branch_blocks_a_duplicate(self):
        self.open.open = [{'number': 7, 'state': 'CLOSED', 'isDraft': True}]
        with self.assertRaisesRegex(ValueError, 'outside this Benny draft operation'):
            self.deliver()
        self.assertEqual(self.open.requests, [])

    def test_data_tampered_during_remote_read_is_rechecked_before_write(self):
        real = self.open.pulls
        calls = 0

        def pulls(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.review.write_text('{}')
            return real(*args, **kwargs)

        self.open.pulls = pulls
        with self.assertRaisesRegex(ValueError, 'current acceptance evidence'):
            self.deliver()
        self.assertEqual(self.open.requests, [])

    def test_existing_unjournaled_branch_and_stale_base_fail_before_writes(self):
        self.open.head = self.head
        with self.assertRaisesRegex(ValueError, 'branch already exists'):
            self.deliver()
        self.open.base = 'f'*40
        with self.assertRaisesRegex(ValueError, 'base moved'):
            self.deliver()
        self.assertEqual(self.open.requests, [])

    def test_uncertain_push_continues_from_exact_observed_tip(self):
        self.open.fail = 'push-response'
        self.assertEqual(self.deliver()['pr'], 27)
        self.assertEqual(self.open.requests, ['push', 'create'])

    def test_uncertain_draft_response_reconciles_exact_observed_remote_draft(self):
        self.open.fail = 'create-response'
        self.assertEqual(self.deliver()['pr'], 27)
        self.assertEqual(cli.read(self.journal)['phase'], 'draft')
        self.open.open[0]['isDraft'] = False
        with self.assertRaisesRegex(ValueError, 'expected Benny draft'):
            self.deliver()
        self.assertEqual(self.open.requests, ['push', 'create'])

    def test_unobserved_creation_never_retries(self):
        self.open.fail = 'create-unobserved'
        with self.assertRaisesRegex(ValueError, 'creation could not be confirmed'):
            self.deliver()
        self.assertEqual(cli.read(self.journal)['phase'], 'create-intent')
        with self.assertRaisesRegex(ValueError, 'no matching remote receipt'):
            self.deliver()
        self.assertEqual(self.open.requests, ['push', 'create'])

    def test_restart_reconciles_created_draft_when_worker_exits_before_reading_remote(self):
        create = self.open.create

        def interrupted(*args, **kwargs):
            create(*args, **kwargs)
            raise KeyboardInterrupt

        with patch.object(self.open, 'create', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.deliver()
        self.assertEqual(cli.read(self.journal)['phase'], 'create-intent')
        result = self.deliver()
        self.assertEqual((result['status'], result['head'], result['pr']), ('draft', self.head, 27))
        self.assertEqual(cli.read(self.journal)['phase'], 'draft')
        self.assertEqual(self.open.requests, ['push', 'create'])

    def test_requires_openclaw_gate_in_the_same_independent_review(self):
        with patch.object(benny_draft.verify_openclaw, 'require',
                          side_effect=benny_draft.verify_openclaw.GatewayGateError('offline Gateway receipt missing')):
            with self.assertRaisesRegex(benny_draft.verify_openclaw.GatewayGateError, 'Gateway receipt missing'):
                self.deliver()
        self.assertEqual(self.open.requests, [])

    def test_secret_removed_from_final_diff_still_blocks_outgoing_history(self):
        repo = self.source.repo
        name = repo/'intermediate.txt'
        name.write_text('gh' + 'p_' + 'z'*36)
        cli.git(repo, 'add', 'intermediate.txt')
        cli.git(repo, 'commit', '-qm', 'Intermediate iteration')
        cli.git(repo, 'rm', '-q', 'intermediate.txt')
        cli.git(repo, 'commit', '-qm', 'Remove intermediate value')
        tip = cli.sha(repo, 'HEAD')
        with self.assertRaisesRegex(ValueError, 'outgoing history contains probable credentials'):
            benny_draft._diff({'repo':str(repo), 'base_commit':self.source.base}, tip)

    def test_local_only_gate_accepts_paused_receipt_and_checks_actions(self):
        data = cli.read(self.task)
        cli.write(self.task, {**data, 'local_gate': {'fixture': True}})
        from workbench import local_gate
        self.open.actions_permissions = Mock(return_value={'enabled': False})
        with patch.object(local_gate, 'validate') as validate:
            self.assertEqual(self.deliver()['status'], 'draft')
        self.assertTrue(validate.called)
        self.assertTrue(self.open.actions_permissions.called)
        self.assertEqual(self.open.requests, ['push', 'create'])

    def test_held_task_writes_nothing(self):
        run_control.hold(self.task, 'Pause this task')
        with self.assertRaises(run_control.Held):
            self.deliver()
        self.assertEqual(self.open.requests, [])

    def test_destination_identity_must_match_configured_fork(self):
        self.open.repository = Mock(return_value={'full_name': 'leewbl/other', 'owner': {'login': 'leewbl'},
                                                  'fork': True, 'default_branch': 'main'})
        with self.assertRaisesRegex(ValueError, 'configured personal fork'):
            self.deliver()
        self.assertEqual(self.open.requests, [])


class DraftCliAdapterTests(unittest.TestCase):
    def test_actual_github_cli_arguments_include_the_draft_flag(self):
        def run(argv, **_):
            self.assertIn('--draft', argv)
            self.assertIn(REPO, argv)
            self.assertEqual(Path(argv[argv.index('--body-file') + 1]).read_text(), 'Proof')
            return subprocess.CompletedProcess(argv, 0, f'https://github.com/{REPO}/pull/27\n', '')

        with patch.object(publish.subprocess, 'run', run):
            result = publish.Publisher().create(REPO, 'benny/report-topic', 'case',
                {'_prepared_pr_body': 'Proof', '_prepared_pr_title': 'Fix observed behavior'}, draft=True)
        self.assertEqual(result, 27)
