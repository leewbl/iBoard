from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from workbench import benny, cli
from workbench.benny_slack import Slack, SlackError, SlackWriter


CHANNEL = 'C123REPORT'
WORKSPACE = 'T123WORKSPACE'
TRIAGE = 'U123TRIAGE'
ROOT = '1765221000.000010'
PR = 'https://github.com/leewbl/myopenclaw/pull/5'
LINK = 'https://workspace.slack.com/archives/' + CHANNEL + '/p' + ROOT.replace('.', '')
TOKEN = 'xoxb-local-benny-fixture'
RULES = {'schema': 1, 'workspace': WORKSPACE, 'channel': CHANNEL, 'triage_identity': TRIAGE,
         'repository': 'leewbl/myopenclaw', 'markers': benny.DEFAULT_MARKERS,
         'verdict_wait_seconds': 20, 'poll_seconds': 1, 'verification_seconds': 900}


def reply(i, text, *, user='U123PERSON', parent=ROOT, **extra):
    return {'type': 'message', 'ts': f'1765221000.{10+i:06d}', 'thread_ts': parent, 'user': user,
            'text': text, **extra}


class Fixture:
    def __init__(self, root):
        self.calls = []
        self.root = reply(0, 'Original report for the affected feature.')
        self.team = WORKSPACE
        self.link = LINK
        self.pages = [[self.root]]
        self.rate_limit = False
        self.repeat_cursor = False
        self.files = {}
        self.reply_behavior = None
        self.sent = []
        self.user = TRIAGE
        token_file = root / 'fixture-key'
        token_file.write_text(TOKEN)
        token_file.chmod(0o600)
        self.token_file = token_file

    def attach(self, file_id, data, media, **fields):
        item = {'id': file_id, 'mode': 'hosted', 'is_external': False, 'size': len(data), 'mimetype': media,
                'url_private_download': f'https://files.slack.com/files-pri/{WORKSPACE}-{file_id}/download/evidence', **fields}
        self.files[file_id] = (item, data)

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def serve(self):
                url = urlsplit(self.path)
                endpoint = url.path.removeprefix('/api/')
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                fixture.calls.append((self.command, endpoint, query))
                if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
                    self.send_error(401)
                    return
                if url.path.startswith(f'/files-pri/{WORKSPACE}-') and self.command == 'GET':
                    file_id = url.path.split('/')[2].removeprefix(WORKSPACE + '-')
                    if file_id not in fixture.files:
                        self.send_error(404)
                        return
                    item, content = fixture.files[file_id]
                    if item.get('redirect'):
                        self.send_response(302)
                        self.send_header('Location', item['redirect'])
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', item['mimetype'])
                    self.send_header('Content-Length', str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                if endpoint == 'auth.test' and self.command == 'POST':
                    result = {'team_id': fixture.team, 'user_id': fixture.user}
                elif endpoint == 'chat.postMessage' and self.command == 'POST' and fixture.reply_behavior:
                    payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    fixture.sent.append(payload)
                    if payload.get('channel') != CHANNEL or payload.get('thread_ts') != ROOT:
                        self.send_error(400)
                        return
                    behavior = fixture.reply_behavior
                    if behavior == 'reject':
                        result = {'ok': False, 'error': 'not_in_channel'}
                    elif behavior == 'uncertain':
                        self.send_error(503)
                        return
                    else:
                        highest = max(int(row['ts'].split('.')[-1]) for page in fixture.pages for row in page)
                        row = reply(max(80 + len(fixture.sent), highest - 9), payload['text'], user=fixture.user)
                        fixture.pages[-1].append(row)
                        if 'reply_count' in fixture.root:
                            fixture.root['reply_count'] += 1
                        if behavior == 'lost':
                            self.send_error(503)
                            return
                        result = {'channel': CHANNEL, 'ts': row['ts'], 'message': row}
                elif endpoint == 'files.info' and self.command == 'GET':
                    item = fixture.files.get(query.get('file'))
                    result = {'file': item[0]} if item else {'ok': False, 'error': 'file_not_found'}
                elif endpoint == 'chat.getPermalink' and self.command == 'GET':
                    if query != {'conversation': CHANNEL, 'message_ts': ROOT}:
                        self.send_error(400)
                        return
                    result = {'channel': CHANNEL, 'permalink': fixture.link}
                elif endpoint == 'conversations.replies' and self.command == 'GET':
                    if query.get('channel') != CHANNEL or query.get('ts') != ROOT:
                        self.send_error(400)
                        return
                    if fixture.rate_limit:
                        fixture.rate_limit = False
                        self.send_response(429)
                        self.send_header('Retry-After', '2')
                        self.end_headers()
                        return
                    page = int(query.get('cursor', '0'))
                    next_page = '1' if fixture.repeat_cursor else (str(page + 1) if page + 1 < len(fixture.pages) else '')
                    result = {'messages': fixture.pages[page], 'has_more': bool(next_page),
                              'response_metadata': {'next_cursor': next_page}}
                else:
                    self.send_error(404)
                    return
                data = json.dumps({'ok': True, **result}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = serve

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.worker = Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02})
        self.worker.start()

        def connect(host, *, timeout):
            assert host in ('slack.com', 'files.slack.com')
            return HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=timeout)

        self.slack = Slack(self.token_file, connection=connect)
        self.writer = SlackWriter(self.token_file, connection=connect)
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)


class BennyTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixture = Fixture(self.root)
        self.addCleanup(self.fixture.__exit__)
        self.fixture.__enter__()

    def freeze(self, name, **overrides):
        args = dict(rules=RULES, slack=self.fixture.slack, channel=CHANNEL, message_ts=ROOT, out=self.root / name)
        return benny.freeze(**(args | overrides))

    def test_exact_root_identity_pagination_and_source_coordinates(self):
        self.fixture.pages = [[{**self.fixture.root, 'reply_count': 2}, reply(1, 'read only')], [
            reply(2, 'No competing artifact.\n[benny:bug] tracker=<https://tracker.example/issue/5|Bug>', user=TRIAGE)]]
        result = self.freeze('rooted', message_ts=reply(1, '')['ts'], thread_ts=ROOT)
        self.assertEqual(result['status'], 'eligible-for-reproduction')
        self.assertEqual(result['verdict']['tracker'], 'https://tracker.example/issue/5')
        record = cli.read(Path(result['receipt']))
        self.assertEqual(record['snapshot']['location']['root'], ROOT)
        self.assertEqual(record['snapshot']['permalink'], LINK)
        reads = [query for _, endpoint, query in self.fixture.calls if endpoint == 'conversations.replies']
        self.assertEqual(len(reads), 2)
        self.assertTrue(all(query['ts'] == ROOT for query in reads))
        self.assertNotIn(TOKEN, Path(result['receipt']).read_text())

    def test_workspace_mismatch_and_foreign_source_are_never_followed(self):
        self.fixture.team = 'T999WRONG'
        with self.assertRaisesRegex(SlackError, 'configured workspace'):
            self.freeze('wrong-workspace')
        self.assertEqual(len(self.fixture.calls), 1)
        with self.assertRaisesRegex(SlackError, 'configured source channel'):
            self.freeze('wrong-channel', channel='C999WRONG')
        self.assertEqual(len(self.fixture.calls), 1)

    def test_incomplete_or_changed_source_identity_blocks_selection(self):
        cases = [
            ([{**self.fixture.root, 'reply_count': 1}], LINK, 'reply count'),
            ([self.fixture.root, reply(1, '[benny:bug]', parent='1765220999.000010')], LINK, 'different report'),
            ([self.fixture.root], LINK.replace(CHANNEL, 'C999WRONG'), 'different source coordinates'),
        ]
        for index, (rows, link, detail) in enumerate(cases):
            self.fixture.pages, self.fixture.link = [rows], link
            with self.assertRaisesRegex(SlackError, detail):
                self.freeze(f'broken-{index}')
        self.fixture.pages, self.fixture.link = [[self.fixture.root], [reply(1, 'continue')]], LINK
        self.fixture.repeat_cursor = True
        with self.assertRaisesRegex(SlackError, 'pagination'):
            self.freeze('cursor-loop')

    def test_marker_must_be_one_unquoted_final_line_from_configured_identity(self):
        self.fixture.pages = [[self.fixture.root, reply(1, '[benny:bug]'),
                               reply(2, '> [benny:bug]\n```\n[benny:bug]\n```', user=TRIAGE)]]
        self.assertEqual(self.freeze('untrusted')['status'], 'awaiting-verdict')
        self.fixture.pages[0].append(reply(3, '[benny:bug]\n[benny:performance]', user=TRIAGE))
        self.assertEqual(self.freeze('conflict')['status'], 'verdict-conflict')
        self.fixture.pages[0][-1] = reply(3, '[benny:other]', user=TRIAGE)
        self.assertEqual(self.freeze('other')['status'], 'out-of-scope')

    def test_human_claim_pauses_and_an_existing_fix_takes_the_read_only_route(self):
        self.fixture.pages = [[self.fixture.root, reply(1, '[benny:performance]', user=TRIAGE),
                               reply(2, "I'll fix it", user='U123UTIL', bot_id='B123UTIL'),
                               reply(3, 'Can you diagnose this?')]]
        self.assertEqual(self.freeze('only-diagnosis')['status'], 'eligible-for-reproduction')
        self.fixture.pages[0].append(reply(4, "I'll fix it tomorrow."))
        self.assertEqual(self.freeze('claimed')['status'], 'claimed')
        self.fixture.pages[0].append(reply(5, f'Fix: <{PR}|PR>'))
        self.assertEqual(self.freeze('has-pr')['artifact'], PR)
        self.fixture.pages[0].append(reply(6, f'Fix: <{PR[:-1]}4|older PR>'))
        self.assertEqual(self.freeze('which-pr')['status'], 'artifact-conflict')
        self.assertEqual(self.freeze('explicit', artifact=PR)['status'], 'verify-existing')
        with self.assertRaisesRegex(ValueError, 'configured repository'):
            self.freeze('foreign-pr', artifact='https://github.com/foreign/repo/pull/50')

    def test_rate_limit_respects_retry_after_for_the_same_thread(self):
        self.fixture.pages = [[self.fixture.root, reply(1, '[benny:bug]', user=TRIAGE)]]
        self.fixture.rate_limit = True
        with patch('workbench.benny.time.sleep') as sleep:
            result = self.freeze('rate-limit', wait=True)
        self.assertEqual(result['status'], 'eligible-for-reproduction')
        sleep.assert_called_once_with(2)

    def test_verify_rechecks_source_and_forwards_exact_selected_pr_and_report(self):
        self.fixture.pages = [[self.fixture.root, reply(1, '[benny:bug]', user=TRIAGE), reply(2, f'Fix: {PR}')]]
        frozen = self.freeze('before')
        target = self.root / 'target'
        target.mkdir()
        run_fix = Mock(return_value={'outcome': 'confirmed'})
        args = dict(slack=self.fixture.slack, receipt=frozen['receipt'], sha256=frozen['sha256'], out=self.root/'verify',
                    run_fix=run_fix, repo=target)
        with self.assertRaisesRegex(ValueError, 'approved digest'):
            benny.verify(**(args | {'sha256': '0' * 64}))
        self.fixture.pages[0].append(reply(3, 'Stop; this is a different behavior than I first reported.'))
        with self.assertRaisesRegex(SlackError, 'thread changed'):
            benny.verify(**args)
        run_fix.assert_not_called()
        self.fixture.pages[0].pop()
        result = benny.verify(**args)
        self.assertEqual(result['outcome'], 'confirmed')
        received = run_fix.call_args.kwargs
        self.assertEqual(received['artifact'], PR)
        self.assertGreater(received['timeout'], 0)
        self.assertLess(received['timeout'], RULES['verification_seconds'])
        self.assertEqual(received['report'].read_text(), LINK + '\n\n' + self.fixture.root['text'] + '\n')
        self.assertEqual(result['source_route']['sha256'], frozen['sha256'])
        self.assertFalse(any(endpoint == 'chat.postMessage' for _, endpoint, _ in self.fixture.calls))

    def test_token_file_permissions_and_read_method_allowlist(self):
        self.fixture.token_file.chmod(0o644)
        with self.assertRaisesRegex(SlackError, 'owner-only'):
            Slack(self.fixture.token_file)
        with self.assertRaisesRegex(SlackError, 'unrecognized'):
            self.fixture.slack.call('chat.postMessage', 10**10)
        self.assertEqual(self.fixture.calls, [])

    def test_source_change_during_run_retains_runtime_result_without_confirmation(self):
        self.fixture.pages = [[self.fixture.root, reply(1, '[benny:bug]', user=TRIAGE), reply(2, f'Fix: {PR}')]]
        frozen = self.freeze('source-before-run')
        target = self.root/'target'
        target.mkdir()

        def run_fix(**_):
            self.fixture.pages[0].append(reply(3, 'I changed the reproduction steps.'))
            return {'outcome': 'confirmed'}

        report = benny.verify(slack=self.fixture.slack, receipt=frozen['receipt'], sha256=frozen['sha256'],
                              out=self.root/'source-after-run', run_fix=run_fix, repo=target)
        self.assertEqual((report['outcome'], report['runtime_outcome'], report['source_recheck']),
                         ('source-changed', 'confirmed', 'changed'))
