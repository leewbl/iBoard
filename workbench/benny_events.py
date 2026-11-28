"""Authenticate Slack callbacks and retain one immutable Benny event receipt."""

from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from workbench.benny_slack import CHANNEL, STAMP, TEAM, USER


MAX_BODY = 256_000
MAX_RECEIPT = 16_000
APP = re.compile(r'A[A-Z0-9]{1,31}')
EVENT = re.compile(r'Ev[A-Za-z0-9]{1,62}')
TIME = re.compile(r'[0-9]{1,12}')
SIGNATURE = re.compile(r'v0=[0-9a-f]{64}')
CHALLENGE = re.compile(r'[A-Za-z0-9._-]{1,256}')
SHA = re.compile(r'[a-f0-9]{64}')
REVISION = re.compile(r'[a-f0-9]{40}')
EVENT_FIELDS = {'id', 'role', 'channel', 'message_ts', 'root', 'workspace', 'app_id'}


class EventError(ValueError):
    pass


class AuthenticationError(EventError):
    pass


def _matches(pattern, value):
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise EventError('JSON contains a repeated key')
        value[key] = item
    return value


def _constant(_):
    raise EventError('JSON contains a non-finite value')


def _object(body):
    try:
        data = json.loads(body.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, RecursionError) as error:
        raise EventError('expected a UTF-8 JSON object with unique keys') from error
    if not isinstance(data, dict):
        raise EventError('expected a JSON object')
    return data


def _private(fd, type_check):
    info = os.fstat(fd)
    return type_check(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def _secret(value):
    if isinstance(value, str):
        try:
            value = value.encode('ascii')
        except UnicodeEncodeError as error:
            raise AuthenticationError('invalid Slack signing secret') from error
    if not isinstance(value, bytes) or not re.fullmatch(rb'[!-~]{16,256}', value):
        raise AuthenticationError('invalid Slack signing secret')
    return value


def read_signing_secret(path):
    """Read a signing secret from an owner-only regular file."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    with os.fdopen(os.open(Path(path).expanduser(), flags), 'rb') as stream:
        if not _private(stream.fileno(), stat.S_ISREG):
            raise EventError('Slack signing credentials require an owner-only regular file')
        value = stream.read(259)
    if len(value) > 258:
        raise EventError('Slack signing credentials exceed the byte limit')
    return _secret(value.removesuffix(b'\n').removesuffix(b'\r'))


def _verify(body, headers, signing_secret, now):
    signed_headers = {}
    if not callable(getattr(headers, 'items', None)):
        raise AuthenticationError('Slack request has invalid signing headers')
    for name, value in headers.items():
        if not isinstance(name, str):
            continue
        name = name.lower()
        if name in ('x-slack-request-timestamp', 'x-slack-signature'):
            if name in signed_headers:
                raise AuthenticationError('Slack request has duplicate signing headers')
            signed_headers[name] = value
    stamp = signed_headers.get('x-slack-request-timestamp')
    signature = signed_headers.get('x-slack-signature')
    if not _matches(TIME, stamp) or not _matches(SIGNATURE, signature):
        raise AuthenticationError('Slack request has invalid signing headers')
    if type(now) not in (int, float) or not math.isfinite(now) or abs(now - int(stamp)) > 300:
        raise AuthenticationError('Slack request is outside the signing window')
    message = b'v0:' + stamp.encode('ascii') + b':' + body
    expected = 'v0=' + hmac.new(_secret(signing_secret), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise AuthenticationError('Slack request signature does not match')


def _ticks(ts):
    seconds, fraction = ts.split('.')
    return int(seconds) * 1_000_000 + int(fraction)


def _ignored(reason):
    return {'status': 'ignored', 'reason': reason}


def receive(body, headers, signing_secret, *, app_id, rules, selected_roots, now=None):
    """Authenticate the exact request bytes before interpreting Slack's callback."""
    if not isinstance(body, bytes) or not 1 <= len(body) <= MAX_BODY:
        raise EventError('Slack request body is missing or too large')
    _verify(body, headers, signing_secret, time.time() if now is None else now)
    if (not _matches(APP, app_id) or not isinstance(rules, dict)
            or not _matches(TEAM, rules.get('workspace')) or not _matches(CHANNEL, rules.get('channel'))
            or not _matches(USER, rules.get('triage_identity'))
            or not isinstance(selected_roots, (list, tuple, set, frozenset)) or len(selected_roots) > 128
            or not all(_matches(STAMP, item) for item in selected_roots)):
        raise EventError('configure the Slack app, workspace, channel, and exact selected roots')

    data = _object(body)
    if data.get('type') == 'url_verification':
        challenge = data.get('challenge')
        if not _matches(CHALLENGE, challenge):
            raise EventError('invalid Slack verification challenge')
        return {'status': 'challenge', 'challenge': challenge}
    if data.get('type') != 'event_callback':
        return _ignored('unsupported-callback')
    application, workspace = data.get('api_app_id'), data.get('team_id')
    if not _matches(APP, application) or not _matches(TEAM, workspace):
        raise EventError('Slack callback needs an app and workspace')
    if application != app_id or workspace != rules['workspace']:
        return _ignored('foreign-installation')
    event = data.get('event')
    if not isinstance(event, dict):
        raise EventError('Slack callback needs an inner event')
    if (event.get('type') != 'message' or event.get('hidden') or event.get('deleted')
            or event.get('subtype') not in (None, 'file_share', 'bot_message')):
        return _ignored('unsupported-event')
    channel = event.get('channel')
    if not _matches(CHANNEL, channel):
        raise EventError('Slack message needs a channel')
    if channel != rules['channel'] or event.get('team', workspace) != workspace:
        return _ignored('foreign-channel')
    message_ts, root = event.get('ts'), event.get('thread_ts', event.get('ts'))
    if not _matches(STAMP, message_ts) or not _matches(STAMP, root) or _ticks(root) > _ticks(message_ts):
        raise EventError('Slack message needs valid message and thread timestamps')
    if root not in selected_roots:
        return _ignored('unselected-thread')

    author = event.get('user')
    if root == message_ts:
        if (not _matches(USER, author) or event.get('bot_id') or event.get('app_id')
                or event.get('subtype') == 'bot_message'):
            return _ignored('non-human-root')
        role = 'triage'
    elif author == rules['triage_identity']:
        role = 'reproduce'
    else:
        return _ignored('untrusted-reply')
    event_id = data.get('event_id')
    if not _matches(EVENT, event_id):
        raise EventError('Slack callback needs a valid event ID')
    return {'status': 'accepted', 'event': {'id': event_id, 'role': role, 'channel': channel,
                                             'message_ts': message_ts, 'root': root, 'workspace': workspace,
                                             'app_id': application}}


def _record(event, deployment):
    if (not isinstance(event, dict) or set(event) != EVENT_FIELDS or not _matches(EVENT, event['id'])
            or not _matches(CHANNEL, event['channel']) or not _matches(STAMP, event['message_ts'])
            or not _matches(STAMP, event['root']) or not _matches(TEAM, event['workspace'])
            or not _matches(APP, event['app_id'])):
        raise EventError('invalid Benny event identity')
    if not ((event['role'] == 'triage' and event['message_ts'] == event['root'])
            or (event['role'] == 'reproduce' and _ticks(event['root']) < _ticks(event['message_ts']))):
        raise EventError('invalid Benny event role for the selected thread')
    if (not isinstance(deployment, dict) or set(deployment) != {'revision', 'manifest_sha256', 'rules_sha256'}
            or not _matches(REVISION, deployment['revision']) or not _matches(SHA, deployment['manifest_sha256'])
            or not _matches(SHA, deployment['rules_sha256'])):
        raise EventError('Benny event requires a committed deployment and rule digests')
    return {'schema': 1, 'kind': 'benny-event', 'status': 'pending', 'event': dict(event),
            'deployment': dict(deployment)}


@contextmanager
def _directory(store, *, create=False):
    path = Path(store).expanduser()
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(path, flags)
    try:
        if not _private(parent, stat.S_ISDIR):
            raise EventError('Benny event store requires an owner-only directory')
        if create:
            try:
                os.mkdir('events', mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
        directory = os.open('events', flags, dir_fd=parent)
        try:
            if not _private(directory, stat.S_ISDIR):
                raise EventError('Benny event directory must be owner-only')
            if create:
                os.fsync(parent)
            yield directory
        finally:
            os.close(directory)
    finally:
        os.close(parent)


def _load(directory, event_id):
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    with os.fdopen(os.open(f'{event_id}.json', flags, dir_fd=directory), 'rb') as stream:
        if not _private(stream.fileno(), stat.S_ISREG):
            raise EventError('Benny event receipt requires an owner-only regular file')
        body = stream.read(MAX_RECEIPT + 1)
    if not 1 <= len(body) <= MAX_RECEIPT:
        raise EventError('invalid Benny event receipt size')
    receipt = _object(body)
    expected = _record(receipt.get('event'), receipt.get('deployment'))
    if type(receipt.get('schema')) is not int or receipt != expected or receipt['event']['id'] != event_id:
        raise EventError('Benny event receipt identity or schema changed')
    return receipt


def load(store, event_id):
    if not _matches(EVENT, event_id):
        raise EventError('invalid Slack event ID')
    with _directory(store) as directory:
        return _load(directory, event_id)


def enqueue(store, event, deployment):
    """Durably publish once and return the same receipt for Slack retries."""
    receipt = _record(event, deployment)
    event_id = event['id']
    encoded = (json.dumps(receipt, sort_keys=True, separators=(',', ':')) + '\n').encode()
    with _directory(store, create=True) as directory:
        staging = f'.pending-{uuid.uuid4().hex}'
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(staging, flags, 0o600, dir_fd=directory), 'wb') as stream:
            try:
                stream.write(encoded)
                stream.flush()
                os.fchmod(stream.fileno(), 0o400)
                os.fsync(stream.fileno())
                os.link(staging, f'{event_id}.json', src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                if _load(directory, event_id) != receipt:
                    raise EventError('Slack event ID is already bound to a different receipt')
            finally:
                os.unlink(staging, dir_fd=directory)
            os.fsync(directory)
    return receipt
