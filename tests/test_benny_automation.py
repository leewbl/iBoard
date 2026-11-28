from datetime import datetime
import hashlib
import hmac
from http.client import HTTPConnection
import json
from pathlib import Path
import shutil
import subprocess
from threading import Thread
import time
import unittest
from unittest.mock import patch

from test_benny import CHANNEL, PR, ROOT, RULES, TRIAGE, WORKSPACE, reply
import test_benny_triage
import test_project_triage
from workbench import benny, benny_automation, benny_deploy, benny_draft, benny_events, benny_reproduce, benny_triage, cli, start


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

    def test_operations_followup_resumes_without_overwriting_a_settled_event(self):
        event = 'Ev087FOLLOWUP'
        marker = reply(1, '[benny:bug]', user=TRIAGE)
        self.source.fixture.pages[0].append(marker)
        self.assertEqual(self.receive(self.payload(event, marker))[0], 200)
        settings = {'repo': self.source.repo, 'rules': RULES, 'state': self.store, 'policy': self.policy,
                    'slack': self.source.fixture.slack, 'operations_writer': self.source.fixture.writer,
                    'seconds': 3, 'max_events': 1, 'poll_seconds': 1}
        plan = {'path': str(self.root/'followup.json'), 'sha256': 'a'*64}
        outcome = {'event': event, 'status': 'draft', 'operations_followup': plan}
        processed = self.store/'processed'/f'{event}.json'
        with patch.object(benny_automation, 'work', return_value=outcome), \
             patch.object(benny_automation.benny_ops_followup, 'step', side_effect=[
                 {'status': 'reply-unconfirmed', 'retry_after': 30},
                 benny_automation.TaskBusy('another watcher is reconciling the reply'), {'status': 'stopped'}]) as observer:
            first = benny_automation.watch(**settings)
            self.assertEqual(first['followups'], {event: 'reply-unconfirmed'})
            self.assertEqual(cli.read(processed)['status'], 'draft')
            resumed = benny_automation.watch(**settings)
            self.assertEqual(resumed['events'], {event: 'draft'})
            self.assertEqual(resumed['followups'], {event: 'stopped'})
            self.assertEqual(cli.read(processed)['followup_status'], 'stopped')
            self.assertEqual(benny_automation.watch(**(settings | {'seconds': 1}))['events'], {})
        self.assertEqual(observer.call_count, 3)
        self.assertTrue(all(call.args == (plan,) for call in observer.call_args_list))

    def test_missing_settled_event_blocks_followup_without_restarting_the_work(self):
        event = 'Ev088FOLLOWUP'
        marker = reply(1, '[benny:bug]', user=TRIAGE)
        self.source.fixture.pages[0].append(marker)
        self.assertEqual(self.receive(self.payload(event, marker))[0], 200)
        ack = {'event': event, 'status': 'draft', 'operations_followup': {'path': '/missing', 'sha256': 'a'*64}}
        with self.assertRaisesRegex(ValueError, 'lost the original settled event'):
            self.work(event, settled_followup=ack)
        self.assertEqual(self.source.fixture.calls, [])

    def queue_repair(self, event, *, claim_seconds=0):
        marker = reply(1, '[benny:bug]', user=TRIAGE)
        self.source.fixture.pages[0].append(marker)
        self.assertEqual(self.receive(self.payload(event, marker))[0], 200)
        runtime = test_project_triage.ProjectTriageTests()
        self.addCleanup(runtime.doCleanups)
        runtime.setUp()
        runtime.repo = self.source.repo
        runtime.base = cli.sha(runtime.repo, 'HEAD')
        runtime.states.update({(test_project_triage.BASE_IMAGE, probe): 'observed' for probe in ('MAIN', 'ORACLE')})
        self.opts['reproduce'].update(manifest=str(runtime.manifest), helpers=str(runtime.helpers),
                                      oracle=str(runtime.oracle), journey='main', oracle_marker='ORACLE_PASS',
                                      oracle_failure_marker='ORACLE_FAIL', claim_seconds=claim_seconds,
                                      repair={'fix_search': ['rename label'], 'draft': True})
        cli.write(self.policy, self.opts)
        real_reproduce, real_prepare = benny_reproduce.reproduce, benny_reproduce.prepare_repair

        def reproduce(**options):
            return real_reproduce(**options, sandbox=runtime.sandbox, attester=runtime.attester)

        def prepare(**options):
            real_run, real_which = subprocess.run, shutil.which

            def inspect(command, *args, **kwargs):
                if command[:3] == ['docker', 'image', 'inspect']:
                    self.assertEqual(command[3], test_project_triage.BASE_IMAGE)
                    image = json.dumps([{'Id': test_project_triage.BASE_IMAGE}]).encode()
                    return subprocess.CompletedProcess(command, 0, image, b'')
                return real_run(command, *args, **kwargs)

            def which(program, *args, **kwargs):
                return program if program in ('claude', 'docker') else real_which(program, *args, **kwargs)

            with patch.object(start.subprocess, 'run', side_effect=inspect), patch.object(start.shutil, 'which', side_effect=which):
                return real_prepare(**options, tracker=self.source.tracker)

        return runtime, reproduce, prepare

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
        event = 'Ev' + 'A' * 62
        _, reproduce, prepare = self.queue_repair(event, claim_seconds=30)
        with patch.object(benny_reproduce, 'reproduce', side_effect=reproduce) as run, \
             patch.object(benny_reproduce, 'prepare_repair', side_effect=prepare) as prepared, \
             patch.object(benny_draft, 'deliver', return_value={'status': 'draft'}) as draft:
            waiting = self.work(event)
            self.assertEqual(waiting['status'], 'waiting-claim')
            self.assertEqual(self.work(event), waiting)
            draft.assert_not_called()
            ready = datetime.fromisoformat(waiting['ready_at']).timestamp()
            with patch.object(benny_automation.time, 'time', return_value=ready + 1):
                result = self.work(event)
                self.assertEqual(self.work(event), result)
            self.assertEqual(result['status'], 'draft')
            self.assertEqual(result['reproduction'], waiting['reproduction'])
            self.assertLessEqual(len(prepared.call_args.kwargs['task_id']), 64)
            run.assert_called_once()
            draft.assert_called_once()

    def test_completed_reproduction_survives_worker_loss(self):
        event = 'Ev456BASELINE'
        runtime, reproduce, _ = self.queue_repair(event, claim_seconds=30)

        def completed_then_interrupted(**options):
            self.assertEqual(reproduce(**options)['status'], 'reproduced')
            raise InterruptedError('stopped after baseline receipt')

        with patch.object(benny_reproduce, 'reproduce', side_effect=completed_then_interrupted) as run, \
             patch.object(benny_reproduce, 'prepare_repair') as prepare:
            with self.assertRaises(InterruptedError):
                self.work(event)
            report = self.work(event)
            run.assert_called_once()
        self.assertEqual(report['status'], 'waiting-claim')
        self.assertEqual(runtime.calls.count((test_project_triage.BASE_IMAGE, 'MAIN')), 2)
        self.assertEqual(runtime.calls.count((test_project_triage.BASE_IMAGE, 'ORACLE')), 2)
        prepare.assert_not_called()

    def test_interrupted_reproduction_with_no_result_blocks_replay(self):
        event = 'Ev456PARTIAL'
        self.queue_repair(event)
        with patch.object(benny_reproduce, 'reproduce', side_effect=InterruptedError('stopped before baseline')) as run, \
             patch.object(benny_reproduce, 'prepare_repair') as prepare:
            with self.assertRaises(InterruptedError):
                self.work(event)
            with self.assertRaises((OSError, ValueError)):
                self.work(event)
        run.assert_called_once()
        prepare.assert_not_called()

    def test_changed_probe_after_interruption_blocks_repair(self):
        event = 'Ev456ALTERED'
        _, reproduce, _ = self.queue_repair(event)

        def changed_then_interrupted(**options):
            self.assertEqual(reproduce(**options)['status'], 'reproduced')
            observed = options['out']/'baseline/main.primary-1.json'
            observed.write_text(observed.read_text() + '\n')
            raise InterruptedError('stopped with changed probe receipt')

        with patch.object(benny_reproduce, 'reproduce', side_effect=changed_then_interrupted) as run, \
             patch.object(benny_reproduce, 'prepare_repair') as prepare:
            with self.assertRaises(InterruptedError):
                self.work(event)
            with self.assertRaisesRegex(ValueError, 'frozen sandbox receipt'):
                self.work(event)
        run.assert_called_once()
        prepare.assert_not_called()

    def test_dispatch_adopts_completed_task_after_preparation_interruption(self):
        event = 'Ev456PREPARED'
        _, reproduce, prepare = self.queue_repair(event)
        expected = {}

        def prepared_then_interrupted(**options):
            expected.update(prepare(**options))
            start._read_input_stage(Path(expected['task']))
            raise InterruptedError('stopped after native task preparation')

        with patch.object(benny_reproduce, 'reproduce', side_effect=reproduce) as run, \
             patch.object(benny_reproduce, 'prepare_repair', side_effect=prepared_then_interrupted) as prepared, \
             patch.object(benny_draft, 'deliver', return_value={'status': 'draft'}) as draft:
            with self.assertRaises(InterruptedError):
                self.work(event)
            draft.assert_not_called()
            result = self.work(event)
        self.assertEqual(result['task'], expected['task'])
        self.assertEqual(result['status'], 'draft')
        self.assertEqual(Path(draft.call_args.kwargs['task']), Path(expected['task']))
        run.assert_called_once()
        prepared.assert_called_once()

    def test_dispatch_reconciles_same_draft_after_interruption_before_settling_event(self):
        event = 'Ev456DRAFT'
        _, reproduce, prepare = self.queue_repair(event)
        deliveries = []
        watch_options = dict(repo=self.source.repo, rules=RULES, state=self.store, policy=self.policy,
                             slack=self.source.fixture.slack, seconds=3, max_events=1, poll_seconds=1)
        processed = self.store/'processed'/f'{event}.json'

        def draft_return(*, task, **_options):
            task = Path(task)
            accepted = {'schema': cli.SCHEMA, 'status': 'draft', 'pr': 27}
            receipt = task.with_name('fixture-draft.json')
            deliveries.append(task)
            if receipt.exists():
                return cli.read(receipt)
            cli.write(receipt, accepted)
            raise KeyboardInterrupt('stopped after draft confirmation')

        with patch.object(benny_reproduce, 'reproduce', side_effect=reproduce) as run, \
             patch.object(benny_reproduce, 'prepare_repair', side_effect=prepare) as prepared, \
             patch.object(benny_draft, 'deliver', side_effect=draft_return):
            with self.assertRaises(KeyboardInterrupt):
                benny_automation.watch(**watch_options)
            self.assertFalse(processed.exists())
            summary = benny_automation.watch(**watch_options)
            result = self.work(event)
            self.assertEqual(self.work(event), result)
        self.assertEqual(summary['events'], {event: 'draft'})
        self.assertEqual(cli.read(processed), {'schema': cli.SCHEMA, 'event': event, 'status': 'draft'})
        self.assertEqual(list(processed.parent.glob('*.json')), [processed])
        self.assertEqual((result['status'], result['repair']['pr']), ('draft', 27))
        self.assertEqual(deliveries, [Path(result['task'])] * 2)
        run.assert_called_once()
        prepared.assert_called_once()

    def test_operations_notification_recovers_without_reentering_native_delivery(self):
        event = 'Ev789OPS'
        _, reproduce, prepare = self.queue_repair(event)
        channel = 'G012IBOARD'
        self.opts['reproduce']['operations'] = {'channel': channel, 'identity': TRIAGE}
        cli.write(self.policy, self.opts)
        slack, writer = self.source.fixture.slack, self.source.fixture.writer
        source_messages = slack.messages
        second = int(time.time())
        root = f'{second}.000050'
        reply_timestamp = f'{second}.000051'
        url = f'https://github.com/{RULES["repository"]}/pull/27'
        rows, writes = [], []

        def thread(conversation, timestamp, deadline):
            if conversation != channel:
                return source_messages(conversation, timestamp, deadline)
            self.assertEqual(timestamp, root)
            return rows[:]

        def post_root(*, channel, text, deadline):
            self.assertEqual(channel, self.opts['reproduce']['operations']['channel'])
            rows.append({'channel': channel, 'type': 'message', 'ts': root, 'user': TRIAGE, 'text': text})
            writes.append(('root', text.splitlines()[0]))
            return root

        def edit_root(*, channel, root, text, deadline):
            self.assertEqual((channel, root), (rows[0]['channel'], rows[0]['ts']))
            rows[0]['text'] = text
            writes.append(('edit', text.splitlines()[0]))
            return root

        def post_reply(*, channel, root, text, deadline):
            self.assertEqual((channel, root), (rows[0]['channel'], rows[0]['ts']))
            rows.append({'channel': channel, 'type': 'message', 'ts': reply_timestamp, 'user': TRIAGE,
                         'text': text, 'thread_ts': root})
            writes.append(('reply', text))
            raise KeyboardInterrupt('stopped after Slack accepted the draft reply')

        watch_options = dict(repo=self.source.repo, rules=RULES, state=self.store, policy=self.policy,
                             slack=slack, operations_writer=writer, seconds=3, max_events=1, poll_seconds=1)
        with patch.object(slack, 'messages', side_effect=thread), patch.object(slack, 'history', return_value=[]), \
             patch.object(writer, 'operations_root', side_effect=post_root), \
             patch.object(writer, 'update_status', side_effect=edit_root), patch.object(writer, 'reply', side_effect=post_reply), \
             patch.object(benny_reproduce, 'reproduce', side_effect=reproduce) as native_reproduce, \
             patch.object(benny_reproduce, 'prepare_repair', side_effect=prepare) as native_prepare, \
             patch.object(benny_draft, 'deliver', return_value={'status': 'draft', 'url': url}) as native_draft:
            with self.assertRaises(KeyboardInterrupt):
                benny_automation.watch(**watch_options)
            self.assertFalse((self.store/'processed'/f'{event}.json').exists())
            self.assertEqual(benny_automation.watch(**watch_options)['events'], {event: 'draft'})
            result = self.work(event, operations_writer=writer, slack=slack)
        native_reproduce.assert_called_once()
        native_prepare.assert_called_once()
        native_draft.assert_called_once()
        self.assertEqual(writes, [('root', 'Reproducing'), ('edit', 'Reproduced'), ('edit', 'Attempting bounded fix'),
                                  ('edit', 'Draft pull request opened'), ('reply', f'Draft pull request opened. {url}')])
        receipt = result['operations']
        self.assertEqual(cli.digest(receipt['path']), receipt['sha256'])
        self.assertEqual(cli.read(receipt['path'])['draft']['url'], url)
        self.assertFalse(self.source.fixture.sent)

    def test_confirmed_reproduction_replies_once_then_repair_keeps_the_notice_receipt(self):
        event = 'Ev987NOTICED'
        _, reproduce, prepare = self.queue_repair(event)
        self.opts['reproduce']['source_notice_identity'] = TRIAGE
        cli.write(self.policy, self.opts)
        writer = self.source.fixture.writer
        self.source.fixture.reply_behavior = 'lost'
        with patch.object(benny_reproduce, 'reproduce', side_effect=reproduce) as run, \
             patch.object(benny_reproduce, 'prepare_repair', side_effect=prepare), \
             patch.object(benny_draft, 'deliver', return_value={'status': 'draft'}) as draft:
            result = self.work(event, source_notice_writer=writer)
            self.assertEqual(self.work(event, source_notice_writer=writer), result)
        run.assert_called_once()
        draft.assert_called_once()
        self.assertEqual(result['status'], 'draft')
        self.assertEqual(cli.read(result['task'])['benny_reproduction']['source_notice'], result['source_notice'])
        self.assertEqual(len(self.source.fixture.sent), 1)
        sent = self.source.fixture.sent[0]
        self.assertEqual((sent['channel'], sent['thread_ts']), (CHANNEL, ROOT))
        self.assertIn('reproduced', sent['text'].lower())

    def test_claim_after_source_notice_preserves_evidence_and_blocks_draft(self):
        event = 'Ev987CLAIMED'
        _, reproduce, prepare = self.queue_repair(event, claim_seconds=30)
        self.opts['reproduce']['source_notice_identity'] = TRIAGE
        cli.write(self.policy, self.opts)
        writer = self.source.fixture.writer
        self.source.fixture.reply_behavior = 'accepted'
        with patch.object(benny_reproduce, 'reproduce', side_effect=reproduce) as run, \
             patch.object(benny_reproduce, 'prepare_repair', side_effect=prepare), \
             patch.object(benny_draft, 'deliver') as draft:
            waiting = self.work(event, source_notice_writer=writer)
            self.assertEqual(waiting['status'], 'waiting-claim')
            self.source.fixture.pages[-1].append(reply(120, "I'll fix the label."))
            ready = datetime.fromisoformat(waiting['ready_at']).timestamp()
            with patch.object(benny_automation.time, 'time', return_value=ready + 1):
                with self.assertRaisesRegex(ValueError, 'source changed|no longer available'):
                    self.work(event, source_notice_writer=writer)
        self.assertTrue(Path(waiting['source_notice']['path']).is_file())
        self.assertEqual(len(self.source.fixture.sent), 1)
        run.assert_called_once()
        draft.assert_not_called()

    def queue_correctable_repair(self, event):
        runtime, reproduce, prepare = self.queue_repair(event, claim_seconds=30)
        variant = self.root/'clean-state'
        variant.mkdir()
        manifest = json.loads(runtime.manifest.read_text())
        manifest['features'][0]['drive'] = 'Clear the fixture cache, then read the named item.'
        (variant/'manifest.json').write_text(json.dumps(manifest))
        shutil.copytree(runtime.helpers, variant/'helpers')
        main = variant/'helpers/main.sh'
        main.write_text('# Clear the fixture cache before reading.\n' + main.read_text())
        (variant/'oracle.sh').write_text('# Read the fixture without cached data.\n' + runtime.oracle.read_text())
        self.opts['reproduce'].update(
            source_notice_identity=TRIAGE,
            corrections={'clean-state': {'manifest': 'clean-state/manifest.json',
                                         'helpers': 'clean-state/helpers', 'oracle': 'clean-state/oracle.sh'}})
        cli.write(self.policy, self.opts)
        self.source.fixture.reply_behavior = 'accepted'
        return runtime, reproduce, prepare

    def correct_setup(self, waiting):
        self.assertEqual(waiting['status'], 'waiting-claim')
        deadline = datetime.fromisoformat(waiting['ready_at']).timestamp()
        correction = reply(120, 'Correction: setup: clean-state', user=self.source.fixture.root['user'],
                           ts=f'{int(deadline) - 1}.000000')
        self.source.fixture.pages[-1].append(correction)
        return deadline

    def test_correction_reuses_a_completed_rerun_after_worker_interruption(self):
        event = 'Ev987CORRECTED'
        _, reproduce, prepare = self.queue_correctable_repair(event)
        writer = self.source.fixture.writer

        def interrupted_reproduction(**options):
            report = reproduce(**options)
            if Path(options['out']).parent.name == 'correction':
                raise KeyboardInterrupt('stopped after corrected reproduction')
            return report

        with patch.object(benny_reproduce, 'reproduce', side_effect=interrupted_reproduction) as run, \
             patch.object(benny_reproduce, 'prepare_repair', side_effect=prepare) as native_prepare, \
             patch.object(benny_draft, 'deliver', return_value={'status': 'draft'}) as draft:
            waiting = self.work(event, source_notice_writer=writer)
            deadline = self.correct_setup(waiting)
            draft.assert_not_called()
            with patch.object(benny_automation.time, 'time', return_value=deadline + 1):
                with self.assertRaises(KeyboardInterrupt):
                    self.work(event, source_notice_writer=writer)
                native_prepare.assert_not_called()
                result = self.work(event, source_notice_writer=writer)
                self.assertEqual(self.work(event, source_notice_writer=writer), result)
        self.assertEqual(result['status'], 'draft')
        self.assertEqual(run.call_count, 2)
        native_prepare.assert_called_once()
        draft.assert_called_once()
        original = waiting['reproduction']
        self.assertEqual(cli.digest(original['path']), original['sha256'])
        notice = waiting['source_notice']
        self.assertEqual(cli.digest(notice['path']), notice['sha256'])
        rerun = Path(run.call_args.kwargs['out'])/'result.json'
        self.assertNotEqual(str(rerun), original['path'])
        corrected = cli.read(rerun)
        self.assertEqual(corrected['status'], 'reproduced')
        self.assertEqual(corrected['claim_seconds'], 0)
        _, route = benny.load_route(corrected['route']['path'], corrected['route']['sha256'])
        self.assertEqual(route['snapshot']['messages'][-1]['text'], 'Correction: setup: clean-state')
        self.assertEqual(cli.read(rerun.with_name('contract.json'))['journey']['drive'],
                         'Clear the fixture cache, then read the named item.')
        self.assertEqual(Path(native_prepare.call_args.kwargs['receipt']), rerun)
        self.assertEqual(native_prepare.call_args.kwargs['sha256'], cli.digest(rerun))
        self.assertEqual(len(self.source.fixture.sent), 1)

    def test_healthy_corrected_setup_keeps_both_reports_and_does_not_prepare_repair(self):
        event = 'Ev987CORRHEALTHY'
        runtime, reproduce, _ = self.queue_correctable_repair(event)
        writer = self.source.fixture.writer

        def healthy_reproduction(**options):
            if Path(options['out']).parent.name == 'correction':
                for probe in ('MAIN', 'ORACLE'):
                    runtime.states[(test_project_triage.BASE_IMAGE, probe)] = 'pass'
            return reproduce(**options)

        with patch.object(benny_reproduce, 'reproduce', side_effect=healthy_reproduction) as run, \
             patch.object(benny_reproduce, 'prepare_repair') as prepare, \
             patch.object(benny_draft, 'deliver') as draft:
            waiting = self.work(event, source_notice_writer=writer)
            deadline = self.correct_setup(waiting)
            with patch.object(benny_automation.time, 'time', return_value=deadline + 1):
                result = self.work(event, source_notice_writer=writer)
                self.assertEqual(self.work(event, source_notice_writer=writer), result)
        self.assertEqual(result['status'], 'not-reproduced')
        self.assertEqual(run.call_count, 2)
        prepare.assert_not_called()
        draft.assert_not_called()
        original = waiting['reproduction']
        self.assertEqual(cli.digest(original['path']), original['sha256'])
        corrected = cli.read(Path(run.call_args.kwargs['out'])/'result.json')
        self.assertEqual(corrected['status'], 'not-reproduced')
        self.assertEqual(len(self.source.fixture.sent), 1)
