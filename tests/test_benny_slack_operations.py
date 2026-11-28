from contextlib import ExitStack
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from workbench.benny_slack import Slack, SlackError, SlackWriter


OPERATIONS = 'G123OPERATIONS'
ROOT = '1765221000.000010'
LATER = '1765221000.000011'
TOKEN = 'xoxb-local-operations-fixture'


class OperationsTransportFixture:
    def __init__(self, directory):
        self.responses = []
        self.calls = []
        self.token = directory/'operations-fixture-key'
        self.token.write_text(TOKEN)
        self.token.chmod(0o600)

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def serve(self):
                if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
                    self.send_error(401)
                    return
                route = urlsplit(self.path)
                size = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(size)) if size else None
                query = {key: values[0] for key, values in parse_qs(route.query).items()}
                fixture.calls.append((self.command, route.path.removeprefix('/api/'), query, body))
                if not fixture.responses:
                    self.send_error(500)
                    return
                response = fixture.responses.pop(0)
                status, value = response if isinstance(response, tuple) else (200, response)
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = serve

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.worker = Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.worker.start()

        def connect(host, *, timeout):
            if host != 'slack.com':
                raise AssertionError('unexpected operations transport host')
            return HTTPConnection('127.0.0.1', self.server.server_port, timeout=timeout)

        self.reader = Slack(self.token, connection=connect)
        self.writer = SlackWriter(self.token, connection=connect)
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.worker.join(2)
        self.server.server_close()


class BennySlackOperationsTests(unittest.TestCase):
    def setUp(self):
        cleanup = ExitStack()
        self.addCleanup(cleanup.close)
        directory = Path(cleanup.enter_context(TemporaryDirectory()))
        self.fixture = cleanup.enter_context(OperationsTransportFixture(directory))
        self.deadline = time.monotonic() + 10

    def test_operations_root_and_update_use_exact_channel_and_timestamp(self):
        opened = {'ok': True, 'channel': OPERATIONS, 'ts': ROOT, 'message': {'text': 'Reproducing'}}
        updated = {**opened, 'message': {'thread_ts': ROOT, 'text': 'Reproduced'}}
        self.fixture.responses.extend([opened, updated])
        writer = self.fixture.writer

        self.assertEqual(writer.operations_root(channel=OPERATIONS, text='Reproducing', deadline=self.deadline), ROOT)
        self.assertEqual(writer.update_status(channel=OPERATIONS, root=ROOT, text='Reproduced',
                                              deadline=self.deadline), ROOT)

        self.assertEqual(self.fixture.calls, [
            ('POST', 'chat.postMessage', {}, {'channel': OPERATIONS, 'text': 'Reproducing', 'mrkdwn': False,
                                             'parse': 'none', 'unfurl_links': False, 'unfurl_media': False}),
            ('POST', 'chat.update', {}, {'channel': OPERATIONS, 'ts': ROOT, 'text': 'Reproduced',
                                        'parse': 'none', 'as_user': True}),
        ])

    def test_cannot_confirm_a_reply_or_a_different_root(self):
        response = {'ok': True, 'channel': OPERATIONS, 'ts': ROOT, 'message': {'text': 'Reproduced'}}
        self.fixture.responses.extend([{**response, 'channel': 'C123REPORT'},
                                       {**response, 'message': {'thread_ts': LATER}}, {**response, 'ts': LATER}])
        writer = self.fixture.writer

        for _ in range(2):
            with self.assertRaisesRegex(SlackError, 'operations root coordinates'):
                writer.operations_root(channel=OPERATIONS, text='Reproducing', deadline=self.deadline)
        with self.assertRaisesRegex(SlackError, 'operations root coordinates'):
            writer.update_status(channel=OPERATIONS, root=ROOT, text='Reproduced', deadline=self.deadline)

    def test_history_retains_the_scope_when_following_a_cursor(self):
        older = {'type': 'message', 'ts': ROOT, 'text': 'Reproducing'}
        latest = {'type': 'message', 'ts': LATER, 'text': 'Verifying existing fix'}
        self.fixture.responses.extend([
            {'ok': True, 'messages': [latest], 'has_more': True, 'response_metadata': {'next_cursor': 'page+/='}},
            {'ok': True, 'channel': OPERATIONS, 'messages': [older], 'has_more': False},
            {'ok': True, 'messages': []},
        ])

        reader = self.fixture.reader
        self.assertEqual(reader.history(OPERATIONS, oldest=ROOT, deadline=self.deadline), [latest, older])
        self.assertEqual(reader.history(OPERATIONS, oldest=ROOT, deadline=self.deadline), [])
        query = {'channel': OPERATIONS, 'oldest': ROOT, 'limit': '15', 'inclusive': 'true'}
        self.assertEqual(self.fixture.calls, [
            ('GET', 'conversations.history', query, None),
            ('GET', 'conversations.history', {**query, 'cursor': 'page+/='}, None),
            ('GET', 'conversations.history', query, None),
        ])

    def test_history_rejects_incomplete_or_wrong_channel_results(self):
        message = {'type': 'message', 'ts': ROOT, 'text': 'Reproducing'}
        empty = {'ok': True, 'messages': []}
        examples = [{**empty, 'channel': 'C123REPORT'}, {**empty, 'has_more': True}, {**empty, 'is_limited': True},
                    {**empty, 'messages': [{**message, 'channel': 'C123REPORT'}]},
                    {**empty, 'messages': [{**message, 'ts': '1765220999.000099'}]},
                    {**empty, 'messages': [message, message]}]
        self.fixture.responses.extend(examples)
        for example in examples:
            with self.subTest(example=example), self.assertRaises(SlackError):
                self.fixture.reader.history(OPERATIONS, oldest=ROOT, deadline=self.deadline)
