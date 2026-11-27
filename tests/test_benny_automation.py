from datetime import datetime, timezone
import hashlib
import hmac
from http.client import HTTPConnection
import json
from pathlib import Path
from threading import Thread
import time
import unittest
from unittest.mock import Mock, patch

from test_benny import CHANNEL, PR, ROOT, RULES, TRIAGE, WORKSPACE, reply
import test_benny_triage
from workbench import benny, benny_automation, benny_deploy, benny_events, benny_triage, cli


APP = 'A123IBOARD'
SECRET = b'0123456789abcdef0123456789abcdef'


class BennyAutomationTests(unittest.TestCase):
    def setUp(self):
        source = test_benny_triage.BennyTriageTests()
        self.addCleanup(source.doCleanups)
        source.setUp()
        self.source = source
        self.root = source.root
        self.store = self.root/'state'
        cli.git(source.repo, 'remote', 'add', 'origin', 'https://github.com/' + RULES['repository'] + '.git')
        benny_deploy.install(source.repo)
        cli.git(source.repo, 'add', '.iboard')
        cli.git(source.repo, 'commit', '-qm', 'Deploy Benny')
        self.policy = self.root/'policy.json'
        helpers = self.root/'helpers'
        helpers.mkdir()
        (self.root/'manifest.json').write_text('{}')
        (self.root/'oracle.py').write_text('print("ORACLE_OK")')
        self.opts = {'schema': cli.SCHEMA,
                     'triage': {'selections': ['labels.py:1-2'], 'terms': ['rename label']},
                     'reproduce': {'manifest': 'manifest.json', 'helpers': 'helpers', 'oracle': 'oracle.py',
                                   'journey': 'rename', 'image': 'fixture:stable', 'oracle_marker': 'ORACLE_OK',
                                   'oracle_failure_marker': 'ORACLE_BAD'}}
        cli.write(self.policy, self.opts)
        self.server = benny_automation.receiver(repo=source.repo, rules=RULES, state=self.store,
                                                app_id=APP, selected_roots=[ROOT], signing_secret=SECRET, port=0)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def receive(self, payload, *, secret=SECRET):
        body = json.dumps(payload).encode()
        stamp = str(int(time.time()))
        signature = 'v0=' + hmac.new(secret, b'v0:' + stamp.encode() + b':' + body, hashlib.sha256).hexdigest()
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        try:
            connection.request('POST', '/slack/events', body=body, headers={
                'Content-Type': 'application/json', 'X-Slack-Signature': signature, 'X-Slack-Request-Timestamp': stamp})
            response = connection.getresponse()
            return response.status, json.load(response)
        finally:
            connection.close()

    def payload(self, ident, message):
        return {'type': 'event_callback', 'api_app_id': APP, 'team_id': WORKSPACE, 'event_id': ident,
                'event': {**message, 'channel': CHANNEL}}

    def work(self, ident, **overrides):
        args = dict(repo=self.source.repo, rules=RULES, state=self.store, policy=self.policy, event_id=ident,
                    slack=self.source.fixture.slack)
        return benny_automation.work(**(args | overrides))

    def test_http_enqueue_deduplicates_and_runs_real_thread_triage_outside_receiver(self):
        self.assertEqual(self.receive({'type': 'url_verification', 'challenge': 'challenge123'}),
                         (200, {'challenge': 'challenge123'}))
        payload = self.payload('Ev123TRIAGE', self.source.fixture.root)
        self.assertEqual(self.receive(payload, secret=b'bad signing credential')[0], 401)
        self.assertEqual(self.receive(payload), (200, {'ok': True}))
        self.assertEqual(self.receive(payload), (200, {'ok': True}))
        self.assertFalse(self.source.fixture.calls)
        retained = benny_events.load(self.store, 'Ev123TRIAGE')
        self.assertEqual(retained['event']['root'], ROOT)
        self.assertEqual(retained['deployment']['revision'], cli.sha(self.source.repo, 'HEAD'))
        self.assertNotIn('text', retained['event'])
        triage = benny_triage.triage

        def active(**kwargs):
            return triage(**kwargs, tracker=self.source.tracker, model=self.source.model)

        with patch.object(benny_triage, 'triage', side_effect=active):
            result = self.work('Ev123TRIAGE')
            self.assertEqual(self.work('Ev123TRIAGE'), result)
        self.assertEqual(cli.read(result['triage']['path'])['status'], 'ready')
        self.source.model.assert_called_once()
        self.assertFalse(any(method == 'chat.postMessage' for _, method, _ in self.source.fixture.calls))
        self.opts['triage']['terms'].append('edited approval')
        cli.write(self.policy, self.opts)
        with self.assertRaisesRegex(ValueError, 'policy changed'):
            self.work('Ev123TRIAGE')

    def test_synthetic_callback_text_never_replaces_the_fresh_verdict_and_watch_runs_existing_fix(self):
        neutral = reply(1, 'Acknowledged.', user=TRIAGE)
        self.source.fixture.pages[0].append(neutral)
        self.assertEqual(self.receive(self.payload('Ev111EARLY', {**neutral, 'text': '[benny:bug]'}))[0], 200)
        self.assertEqual(self.work('Ev111EARLY')['status'], 'awaiting-verdict')
        marker = reply(2, '[benny:bug]', user=TRIAGE)
        self.source.fixture.pages[0].extend([marker, reply(3, f'Fix: {PR}')])
        self.assertEqual(self.receive(self.payload('Ev222FIX', marker))[0], 200)
        real_verify = benny.verify

        def comparison(*, report, artifact, out, **kwargs):
            self.assertEqual(artifact, PR)
            self.assertIn(test_benny_triage.OBSERVED, report.read_text())
            out.mkdir()
            return {'outcome': 'confirmed'}

        def active(**kwargs):
            return real_verify(**kwargs, run_fix=comparison)

        with patch.object(benny, 'verify', side_effect=active) as verify:
            sweep = benny_automation.watch(repo=self.source.repo, rules=RULES, state=self.store, policy=self.policy,
                                           slack=self.source.fixture.slack, max_events=2, seconds=5, poll_seconds=1)
            self.assertEqual(self.work('Ev222FIX')['status'], 'confirmed')
            later = reply(4, '[benny:bug]', user=TRIAGE)
            self.source.fixture.pages[0].append(later)
            self.assertEqual(self.receive(self.payload('Ev333LATER', later))[0], 200)
            newer_sweep = benny_automation.watch(repo=self.source.repo, rules=RULES, state=self.store, policy=self.policy,
                                                 slack=self.source.fixture.slack, max_events=1, seconds=5, poll_seconds=1)
        self.assertEqual(sweep['events'], {'Ev111EARLY': 'awaiting-verdict', 'Ev222FIX': 'confirmed'})
        self.assertEqual(newer_sweep['events'], {'Ev333LATER': 'confirmed'})
        verify.assert_called_once()

    def test_changed_git_revision_withholds_queued_work(self):
        self.receive(self.payload('EvOLDHEAD', self.source.fixture.root))
        cli.git(self.source.repo, 'commit', '--allow-empty', '-qm', 'Move deployment HEAD')
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            self.work('EvOLDHEAD')
        self.assertEqual(self.source.fixture.calls, [])

    def test_opted_in_triage_writer_posts_once_and_only_under_the_root(self):
        self.source.reply.update(category='unclear', question='Which item was renamed?')
        self.source.fixture.reply_behavior = 'accepted'
        self.opts['triage']['apply_verdict'] = True
        cli.write(self.policy, self.opts)
        self.receive(self.payload('Ev999VERDICT', self.source.fixture.root))
        with self.assertRaisesRegex(ValueError, 'separate Slack writer'):
            self.work('Ev999VERDICT')
        triage = benny_triage.triage

        def active(**kwargs):
            return triage(**kwargs, tracker=self.source.tracker, model=self.source.model)

        with patch.object(benny_triage, 'triage', side_effect=active):
            result = self.work('Ev999VERDICT', writer=self.source.fixture.writer)
            self.assertEqual(self.work('Ev999VERDICT', writer=self.source.fixture.writer), result)
        self.assertEqual(len(self.source.fixture.sent), 1)
        self.assertEqual(self.source.fixture.sent[0]['thread_ts'], ROOT)
        self.assertTrue(self.source.fixture.sent[0]['text'].endswith('[benny:other]'))

    def test_interruption_after_native_handoff_recovers_without_posting_again(self):
        self.source.reply.update(category='unclear', question='Which item was renamed?')
        self.source.fixture.reply_behavior = 'accepted'
        self.opts['triage']['apply_verdict'] = True
        cli.write(self.policy, self.opts)
        event = 'Ev987RECOVERY'
        self.receive(self.payload(event, self.source.fixture.root))
        writer = self.source.fixture.writer
        triage = benny_triage.triage
        apply = benny_automation.benny_handoff.apply

        def active(**kwargs):
            return triage(**kwargs, tracker=self.source.tracker, model=self.source.model)

        def interrupt(**kwargs):
            self.assertEqual(apply(**kwargs)['status'], 'confirmed')
            raise InterruptedError('stopped after native confirmation')

        with patch.object(benny_triage, 'triage', side_effect=active) as model_stage:
            with patch.object(benny_automation.benny_handoff, 'apply', side_effect=interrupt) as first:
                with self.assertRaisesRegex(InterruptedError, 'after native confirmation'):
                    self.work(event, writer=writer)
            with patch.object(benny_automation.benny_handoff, 'apply', wraps=apply) as resumed:
                result = self.work(event, writer=writer)
            self.assertEqual(self.work(event, writer=writer), result)
            model_stage.assert_called_once()
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(first.call_args.kwargs['sha256'], resumed.call_args.kwargs['sha256'])
        self.assertLessEqual(resumed.call_args.kwargs['seconds'], first.call_args.kwargs['seconds'])
        self.assertEqual(len(self.source.fixture.sent), 1)

    def test_claim_wait_reuses_original_reproduction_before_draft(self):
        marker = reply(1, '[benny:bug]', user=TRIAGE)
        self.source.fixture.pages[0].append(marker)
        event = 'Ev' + 'A' * 62
        self.receive(self.payload(event, marker))
        self.opts['reproduce']['repair'] = {'fix_search': ['rename label'], 'draft': True}
        cli.write(self.policy, self.opts)
        now = time.time()

        def reproduced(*, out, **options):
            out.mkdir()
            proof = {'schema': cli.SCHEMA, 'status': 'reproduced', 'finished_at':
                     datetime.fromtimestamp(now, timezone.utc).isoformat(), 'claim_seconds': 30}
            cli.write(out/'result.json', proof)
            return proof

        def approved(path, sha256):
            self.assertEqual(cli.digest(path), sha256)
            return Path(path), cli.read(path), {}, set()

        with patch.object(benny_automation.benny_reproduce, 'reproduce', side_effect=reproduced) as reproduce, \
             patch.object(benny_automation.benny_reproduce, '_approved', side_effect=approved), \
             patch.object(benny_automation.benny_reproduce, 'prepare_repair',
                          return_value={'task': str(self.root/'task.json')}) as prepare, \
             patch.object(benny_automation.benny_draft, 'deliver', return_value={'status': 'draft-confirmed'}) as draft:
            waiting = self.work(event)
            self.assertEqual(waiting['status'], 'waiting-claim')
            self.assertEqual(self.work(event), waiting)
            draft.assert_not_called()
            with patch.object(benny_automation.time, 'time', return_value=now + 31):
                result = self.work(event)
                self.assertEqual(self.work(event), result)
            self.assertEqual(result['status'], 'draft-confirmed')
            self.assertEqual(result['reproduction'], waiting['reproduction'])
            self.assertLessEqual(len(prepare.call_args.kwargs['task_id']), 64)
            reproduce.assert_called_once()
            draft.assert_called_once()
