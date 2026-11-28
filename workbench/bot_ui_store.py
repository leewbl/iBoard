"""Keep webhook attempts, wake receipts, and replay state in one local store."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from workbench import cli
from workbench.automate_me import _digest


class Conflict(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class Store:
    def __init__(self, directory):
        self.path = Path(directory) / 'events.sqlite3'
        if self.path.is_symlink():
            raise ValueError('routine state cannot be a symbolic link')
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL,
                    state TEXT NOT NULL, stamp TEXT NOT NULL, attempts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wakes (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL,
                    headers TEXT NOT NULL, stamp_ms INTEGER NOT NULL,
                    state TEXT NOT NULL, result TEXT
                );
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=8)
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE wakes SET state='interrupted' WHERE state='running'")
            db.execute("UPDATE outbox SET state='retained' WHERE state='forwarding'")
            return [row[0] for row in db.execute("SELECT id FROM wakes WHERE state='queued' ORDER BY stamp_ms")]

    def begin(self, event):
        identity, body = event['request_id'], encode(event)
        digest = _digest(body.encode())
        with self.connect() as db:
            cursor = db.execute('INSERT OR IGNORE INTO outbox VALUES (?, ?, ?, ?, ?, ?)',
                                (identity, body, digest, 'forwarding', cli.now(), 1))
            if cursor.rowcount:
                return 'send'
            previous = db.execute('SELECT digest,state FROM outbox WHERE id=?', (identity,)).fetchone()
            if previous[0] != digest:
                raise Conflict('This request ID is already bound to different JSON.')
            return previous[1]

    def send_result(self, identity, sent):
        state = 'accepted' if sent else 'retained'
        with self.connect() as db:
            db.execute("UPDATE outbox SET state=?,stamp=? WHERE id=? AND state='forwarding'",
                       (state, cli.now(), identity))
        return state

    def retained(self):
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT id FROM outbox WHERE state='retained' ORDER BY stamp LIMIT 32")]

    def retained_count(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM outbox WHERE state='retained'").fetchone()[0]

    def claim_retained(self, identity):
        with self.connect() as db:
            changed = db.execute("UPDATE outbox SET state='forwarding',attempts=attempts+1 WHERE id=? AND state='retained'",
                                 (identity,))
            if changed.rowcount:
                return json.loads(db.execute('SELECT body FROM outbox WHERE id=?', (identity,)).fetchone()[0])
        return None

    def intake(self, event, body, headers, stamp_ms):
        identity = event['request_id']
        digest = _digest(body.encode())
        with self.connect() as db:
            state = 'ignored' if event['action'] == '_probe' else 'queued'
            added = db.execute('INSERT OR IGNORE INTO wakes VALUES (?, ?, ?, ?, ?, ?, NULL)',
                               (identity, body, digest, encode(headers), stamp_ms, state))
            if added.rowcount:
                return state, state == 'queued'
            previous = db.execute('SELECT digest,state FROM wakes WHERE id=?', (identity,)).fetchone()
            if previous[0] != digest:
                raise Conflict('This request ID is already bound to different JSON.')
            return previous[1], False

    def claim(self, identity):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body,digest,headers,stamp_ms FROM wakes WHERE id=? AND state='queued'",
                             (identity,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE wakes SET state='running' WHERE id=?", (identity,))
        body, digest, headers, stamp = row
        return {'body': body, 'body_digest': digest, 'headers': json.loads(headers), 'timestamp_ms': stamp}

    def finish(self, identity, state, result):
        with self.connect() as db:
            db.execute("UPDATE wakes SET state=?,result=? WHERE id=? AND state='running'",
                       (state, encode(result), identity))

    def status(self, identity):
        with self.connect() as db:
            forwarded = db.execute('SELECT state,attempts FROM outbox WHERE id=?', (identity,)).fetchone()
            wake = db.execute('SELECT state,result,body FROM wakes WHERE id=?', (identity,)).fetchone()
        if not forwarded and not wake:
            return None
        return {'id': identity, 'forwarding': forwarded[0] if forwarded else None,
                'attempts': forwarded[1] if forwarded else 0, 'wake': wake[0] if wake else None,
                'result': json.loads(wake[1]) if wake and wake[1] else None,
                'action': json.loads(wake[2])['action'] if wake else None}
