from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from email.message import Message
import hashlib
import hmac
import json
from pathlib import Path
import stat
import tempfile
import unittest

from workbench import benny_events as events


NOW = 1765221000
ROOT = f'{NOW}.000010'
REPLY = f'{NOW}.000011'
OTHER = f'{NOW}.000012'
SECRET = b'local-signing-secret-fixture'
APP = 'A123BENNY'
RULES = {'workspace': 'T123WORKSPACE', 'channel': 'C123REPORT', 'triage_identity': 'U123TRIAGE'}
DEPLOYMENT = {'revision': 'a' * 40, 'manifest_sha256': 'b' * 64, 'rules_sha256': 'c' * 64}
CALLBACK = {
    'type': 'event_callback', 'api_app_id': APP, 'team_id': RULES['workspace'], 'event_id': 'EvFirst',
    'event': {'type': 'message', 'channel': RULES['channel'], 'ts': ROOT, 'user': 'U123REPORTER',
              'text': 'Synthetic text stays out of the receipt.'},
}


def headers(raw, *, now=NOW, key=SECRET):
    stamp = str(now)
    mac = hmac.new(key, f'v0:{stamp}:'.encode() + raw, hashlib.sha256).hexdigest()
    return {'X-slack-Request-Timestamp': stamp, 'x-Slack-Signature': 'v0=' + mac}


class CallbackTests(unittest.TestCase):
    def receive(self, packet, *, signed=None, roots=(ROOT,)):
        raw = packet if isinstance(packet, bytes) else json.dumps(packet, separators=(',', ':')).encode()
        return events.receive(raw, signed if signed is not None else headers(raw), SECRET, app_id=APP,
                              rules=RULES, selected_roots=roots, now=NOW)

    def test_signed_challenge_and_exact_bytes(self):
        raw = b'{ "type": "url_verification", "challenge": "fixture-challenge" }'
        self.assertEqual(self.receive(raw), {'status': 'challenge', 'challenge': 'fixture-challenge'})
        with self.assertRaises(events.AuthenticationError):
            self.receive(raw.replace(b' ', b''), signed=headers(raw))
        with self.assertRaises(events.AuthenticationError):
            self.receive(raw, signed=headers(raw, key=b'other-signing-fixture'))

    def test_stale_future_and_duplicate_signing_headers(self):
        raw = b'{"type":"url_verification","challenge":"cosmos"}'
        for stamp in (NOW - 301, NOW + 301):
            with self.subTest(stamp=stamp), self.assertRaises(events.AuthenticationError):
                self.receive(raw, signed=headers(raw, now=stamp))
        wire = Message()
        for name, value in headers(raw).items():
            wire[name] = value
        wire['x-slack-signature'] = headers(raw)['x-Slack-Signature']
        with self.assertRaisesRegex(events.AuthenticationError, 'duplicate'):
            self.receive(raw, signed=wire)

    def test_authenticated_selected_human_and_trusted_triage_reply(self):
        ready = self.receive(CALLBACK)
        self.assertEqual(ready, {'status': 'accepted', 'event': {'id': 'EvFirst', 'role': 'triage',
                         'channel': RULES['channel'], 'message_ts': ROOT, 'root': ROOT,
                         'workspace': RULES['workspace'], 'app_id': APP}})
        reply = deepcopy(CALLBACK)
        reply['event_id'] = 'EvSecond'
        reply['event'].update(ts=REPLY, thread_ts=ROOT, user=RULES['triage_identity'],
                              subtype='bot_message', bot_id='B123TEST', text='Untrusted payload.')
        attempt = self.receive(reply)
        self.assertEqual(attempt['event']['role'], 'reproduce')
        self.assertEqual(attempt['event']['root'], ROOT)
        self.assertEqual(attempt['event']['message_ts'], REPLY)
        self.assertNotIn('text', attempt['event'])

    def test_other_installations_roots_and_untrusted_messages_are_ignored(self):
        cases = (
            {'api_app_id': 'A999OTHER'},
            {'team_id': 'T999OTHER'},
            {'event': {**CALLBACK['event'], 'channel': 'C999OTHER'}},
            {'event': {**CALLBACK['event'], 'team': 'T999OTHER'}},
            {'event': {**CALLBACK['event'], 'bot_id': 'B123TEST'}},
            {'event': {**CALLBACK['event'], 'subtype': 'message_changed'}},
            {'event': {**CALLBACK['event'], 'ts': REPLY, 'thread_ts': ROOT}},
        )
        for fields in cases:
            with self.subTest(fields=list(fields)):
                self.assertEqual(self.receive({**CALLBACK, **fields})['status'], 'ignored')
        self.assertEqual(self.receive(CALLBACK, roots=(OTHER,))['reason'], 'unselected-thread')
        self.assertEqual(self.receive(CALLBACK, roots=())['reason'], 'unselected-thread')

    def test_rejects_ambiguous_oversized_and_malformed_requests(self):
        for raw in (b'{"type":"a","type":"b"}', b'{"a":NaN}', b'[]', b'{\xff', b'x' * (events.MAX_BODY + 1)):
            with self.subTest(size=len(raw)), self.assertRaises(events.EventError):
                self.receive(raw)
        broken = deepcopy(CALLBACK)
        broken['event']['thread_ts'] = OTHER
        with self.assertRaisesRegex(events.EventError, 'timestamps'):
            self.receive(broken)
        with self.assertRaisesRegex(events.EventError, 'valid event ID'):
            self.receive({**CALLBACK, 'event_id': '../EvFirst'})


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = self.root / 'store'
        raw = json.dumps(CALLBACK).encode()
        self.event = events.receive(raw, headers(raw), SECRET, app_id=APP, rules=RULES,
                                    selected_roots=(ROOT,), now=NOW)['event']

    def test_retry_preserves_original_receipt_and_requires_all_deployment_digests(self):
        first = events.enqueue(self.store, self.event, DEPLOYMENT)
        file = self.store / 'events' / 'EvFirst.json'
        info = file.stat()
        self.assertEqual(first, events.load(self.store, self.event['id']))
        self.assertEqual(first, events.enqueue(self.store, self.event, DEPLOYMENT))
        self.assertEqual(info.st_mtime_ns, file.stat().st_mtime_ns)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o400)
        self.assertEqual(first['status'], 'pending')
        self.assertNotIn('Synthetic text', file.read_text())
        with self.assertRaisesRegex(events.EventError, 'different receipt'):
            events.enqueue(self.store, self.event, {**DEPLOYMENT, 'rules_sha256': 'd' * 64})
        with self.assertRaisesRegex(events.EventError, 'different receipt'):
            events.enqueue(self.store, {**self.event, 'role': 'reproduce', 'message_ts': REPLY}, DEPLOYMENT)
        with self.assertRaisesRegex(events.EventError, 'committed deployment'):
            events.enqueue(self.store, self.event, {'revision': 'a' * 40, 'manifest_sha256': 'b' * 64})
        self.assertEqual(first, events.load(self.store, self.event['id']))

    def test_concurrent_retries_create_one_complete_file(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            receipts = list(pool.map(lambda _: events.enqueue(self.store, self.event, DEPLOYMENT), range(12)))
        expected = events.load(self.store, self.event['id'])
        self.assertTrue(all(row == expected for row in receipts))
        self.assertEqual([path.name for path in (self.store/'events').iterdir()], ['EvFirst.json'])

    def test_repository_directory_and_receipts_reject_links_and_public_modes(self):
        target = self.root / 'other'
        target.mkdir(mode=0o700)
        self.store.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            events.enqueue(self.store, self.event, DEPLOYMENT)
        self.store.unlink()
        self.store.mkdir(mode=0o755)
        self.store.chmod(0o755)
        with self.assertRaisesRegex(events.EventError, 'owner-only'):
            events.enqueue(self.store, self.event, DEPLOYMENT)
        self.store.chmod(0o700)
        events.enqueue(self.store, self.event, DEPLOYMENT)
        original = self.store / 'events' / 'EvFirst.json'
        copy = target / original.name
        original.rename(copy)
        original.symlink_to(copy)
        with self.assertRaises(OSError):
            events.load(self.store, self.event['id'])
        with self.assertRaises(events.EventError):
            events.load(self.store, '../EvFirst')

    def test_owner_only_signing_secret_is_never_followed_or_echoed(self):
        key = self.root / 'key'
        key.write_bytes(SECRET + b'\r\n')
        key.chmod(0o600)
        self.assertEqual(events.read_signing_secret(key), SECRET)
        key.chmod(0o644)
        with self.assertRaisesRegex(events.EventError, 'owner-only') as error:
            events.read_signing_secret(key)
        self.assertNotIn(SECRET.decode(), str(error.exception))
        key.chmod(0o600)
        link = self.root / 'key-link'
        link.symlink_to(key)
        with self.assertRaises(OSError):
            events.read_signing_secret(link)


if __name__ == '__main__':
    unittest.main()
