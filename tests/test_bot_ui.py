import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from workbench import bot_ui, cli
from workbench.bot_ui_store import Store, encode


class BotUITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.directory = self.root / 'routine'
        self.task = self.root / 'task.json'
        cli.write(self.task, {'schema': cli.SCHEMA})
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'title': 'Task panel', 'locale': 'en', 'actions': [
            {'id': 'inspect', 'label': 'Inspect', 'description': 'See the selected task.',
             'operation': {'kind': 'inspect-task', 'task': str(self.task)}}]}))
        bot_ui.create(manifest=manifest, directory=self.directory)
        self.operator = bot_ui._secret(self.directory / 'operator.key')
        self.sender = bot_ui._secret(self.directory / 'receiver.key')
        self.calls = []

    def start(self):
        def dispatch(operation):
            self.calls.append(operation)
            return {'recorded_status': 'running'}
        self.server, urls = bot_ui.bind(self.directory, 0, dispatch=dispatch)
        self.url = urls[0]
        worker = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02})
        worker.start()
        def cleanup():
            self.server.shutdown()
            self.server.server_close()
            self.server.routine.stop()
            worker.join()
        self.addCleanup(cleanup)

    def post(self, path, payload, *, key=None, extra=None):
        headers = {'Content-Type': 'application/json'}
        if key is not None:
            headers['Authorization'] = 'Bearer ' + key
        headers.update(extra or {})
        return bot_ui._http(self.url + path, 'POST', body=json.dumps(payload).encode(), headers=headers)

    def await_wake(self, identity):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            item = self.server.routine.store.status(identity)
            if item and item['wake'] in ('complete', 'failed'):
                return item
            time.sleep(.01)
        self.fail('wake did not finish')

    def test_page_does_not_contain_keys_and_probe_does_not_run_an_action(self):
        self.start()
        for path in ('/', '/style.css', '/app.js', '/api/options'):
            status, body = bot_ui._http(self.url + path, 'GET')
            self.assertEqual(status, 200)
            for key in (self.operator, self.sender, str(self.task)):
                self.assertNotIn(key.encode(), body)
        self.assertEqual(bot_ui.doctor(directory=self.directory, url=self.url)['status'], 'ready')
        self.assertEqual(self.calls, [])

    def test_received_action_runs_once_and_rejects_unknown_fields_or_wrong_origin(self):
        self.start()
        event = {'action': 'inspect', 'request_id': '1' * 32, 'values': {}}
        self.assertEqual(self.post('/api/actions', event, key=None)[0], 401)
        self.assertEqual(self.post('/api/actions', event, key=self.sender)[0], 401)
        self.assertEqual(self.post('/api/actions', event, key=self.operator, extra={'Origin': 'http://wrong.local'})[0], 403)
        self.assertEqual(self.post('/api/actions', {**event, 'values': {'prompt': 'other'}}, key=self.operator)[0], 400)
        self.assertEqual(self.post('/api/actions', {**event, 'action': 'run-other'}, key=self.operator)[0], 400)
        self.assertEqual(self.post('/api/actions', event, key=self.operator)[0], 200)
        self.assertEqual(self.await_wake(event['request_id'])['result'], {'recorded_status': 'running'})
        self.assertEqual(self.post('/api/actions', event, key=self.operator)[0], 200)
        self.assertEqual(len(self.calls), 1)

    def test_retained_payload_is_processed_by_a_separate_wake(self):
        self.start()
        event = {'action': 'inspect', 'request_id': '2' * 32, 'values': {}}
        with patch.object(self.server.routine, '_send', return_value=False) as send:
            status, body = self.post('/api/actions', event, key=self.operator)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['state'], 'retained')
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.calls, [])
        status, body = self.post('/api/drain', {}, key=self.operator)
        self.assertEqual(status, 200)
        drained = self.await_wake(json.loads(body)['id'])
        self.assertEqual(drained['result'], {'accepted': 1, 'retained': 0})
        event_state = self.await_wake(event['request_id'])
        self.assertEqual(event_state['attempts'], 2)
        self.assertEqual(len(self.calls), 1)

    def test_sender_header_and_body_must_both_match_the_approved_receiver(self):
        self.start()
        event = {'action': 'inspect', 'request_id': '3' * 32, 'values': {}}
        self.assertEqual(self.post('/webhook', event, key=self.sender)[0], 401)
        good = {'X-Automation-Key': self.sender}
        self.assertEqual(self.post('/webhook', event, key=self.sender, extra=good)[0], 200)
        self.assertEqual(self.post('/webhook', {**event, 'action': '_probe'}, key=self.sender, extra=good)[0], 409)
        self.assertEqual(self.await_wake(event['request_id'])['wake'], 'complete')

    def test_drain_reports_the_entire_backlog_after_one_batch(self):
        routine = bot_ui.Routine(self.directory)
        with patch.object(routine, '_send', return_value=False):
            for number in range(65):
                event = {'action': 'inspect', 'request_id': f'{number:032x}', 'values': {}}
                self.assertEqual(routine.forward(event)['state'], 'retained')
        with patch.object(routine, '_send', return_value=True):
            self.assertEqual(routine.drain(), {'accepted': 32, 'retained': 33})

    def test_cursor_configuration_uses_one_https_attempt_and_both_sender_headers(self):
        key = self.root / 'fixture-provider.key'
        bot_ui._new_secret(key, self.sender)
        url = 'https://api2.cursor.sh/automations/webhook/test-fixture'
        bot_ui.configure(directory=self.directory, webhook=url, sender_file=key)
        routine = bot_ui.Routine(self.directory)
        routine.local_url = 'http://127.0.0.1:9'
        event = {'action': 'inspect', 'request_id': '4' * 32, 'values': {}}
        with patch.object(bot_ui, '_http', return_value=(500, b'fixture error')) as send:
            self.assertEqual(routine.forward(event)['state'], 'retained')
            self.assertEqual(routine.forward(event)['state'], 'retained')
        send.assert_called_once_with(url, 'POST', body=encode(event).encode(), headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.sender,
            'X-Automation-Key': self.sender, 'User-Agent': 'iBoard-routine/1'})

    def test_recovery_preserves_uncertain_effects_and_requeues_unstarted_wakes(self):
        audit = Store(self.directory)
        for number in ('5', '6'):
            event = {'action': 'inspect', 'request_id': number * 32, 'values': {}}
            audit.intake(event, encode(event), {}, int(number))
        self.assertIsNotNone(audit.claim('5' * 32))
        self.assertEqual(audit.recover(), ['6' * 32])
        self.assertEqual(audit.status('5' * 32)['wake'], 'interrupted')

    def test_tailnet_urls_are_discovered_without_creating_another_node(self):
        profile = {'BackendState': 'Running', 'Self': {'Online': True, 'DNSName': 'desk.example.ts.net.'}}
        calls = []
        def invoke(argv, **kwargs):
            calls.append(argv)
            answer = json.dumps(profile) if argv[1] == 'status' else '100.72.1.4'
            return subprocess.CompletedProcess(argv, 0, stdout=answer)
        result = bot_ui.tailnet(executable='fixture-tailscale', invoke=invoke)
        self.assertEqual(result, {'ipv4': '100.72.1.4', 'dns': 'desk.example.ts.net'})
        self.assertEqual([row[1:] for row in calls], [['status', '--json'], ['ip', '-4']])
