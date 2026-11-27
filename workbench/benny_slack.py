"""Read a single approved Slack report and its replies using the public Web API."""

import http.client
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import unquote, urlencode, urlsplit


CHANNEL = re.compile(r'[CGD][A-Z0-9]{1,31}')
TEAM = re.compile(r'T[A-Z0-9]{1,31}')
USER = re.compile(r'[UW][A-Z0-9]{1,31}')
FILE = re.compile(r'F[A-Z0-9]{1,31}')
STAMP = re.compile(r'[1-9][0-9]{0,12}\.[0-9]{6}')
TOKEN = re.compile(r'xox[abp]-[A-Za-z0-9-]{8,300}')
MAX_PAGE = 2_000_000
MAX_MESSAGES = 200


class SlackError(ValueError):
    pass


class RetryAfter(SlackError):
    def __init__(self, seconds):
        super().__init__('Slack rate limited the report read')
        self.seconds = seconds


class SlackRejected(SlackError):
    """Slack replied that the particular write was rejected."""


def _token(path):
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(Path(path).expanduser(), flags), 'r', encoding='ascii') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SlackError('Slack credentials require an owner-only regular file')
        value = stream.read(400).strip()
    if not TOKEN.fullmatch(value):
        raise SlackError('invalid Slack credential file')
    return value


class Slack:
    METHODS = {'auth.test': 'POST', 'conversations.replies': 'GET', 'chat.getPermalink': 'GET', 'files.info': 'GET'}

    def __init__(self, token_file, *, connection=http.client.HTTPSConnection):
        self.token = _token(token_file)
        self.connection = connection

    def _request(self, host, method, path, deadline, limit, endpoint, *, payload=None):
        left = deadline - time.monotonic()
        if left <= 0:
            raise SlackError('Benny report read exceeded its deadline')
        client = self.connection(host, timeout=min(left, 12))
        try:
            headers = {'Authorization': 'Bearer ' + self.token, 'Accept-Encoding': 'identity'}
            if payload is not None:
                headers['Content-Type'] = 'application/json; charset=utf-8'
            client.request(method, path, body=payload, headers=headers)
            response = client.getresponse()
            if response.status == 429:
                retry = response.getheader('Retry-After', '60')
                delay = int(retry) if re.fullmatch(r'[0-9]{1,5}', retry) else 60
                raise RetryAfter(max(1, delay))
            if response.status != 200:
                raise SlackError(f'Slack {endpoint} returned HTTP {response.status}')
            advertised = response.getheader('Content-Length')
            if advertised and (not re.fullmatch(r'[0-9]{1,10}', advertised) or int(advertised) > limit):
                raise SlackError(f'Slack {endpoint} exceeded the byte limit')
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise SlackError(f'Slack {endpoint} exceeded the byte limit')
            return raw, response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
        except (OSError, http.client.HTTPException) as error:
            raise SlackError(f'Slack {endpoint} could not be read') from error
        finally:
            client.close()

    def call(self, endpoint, deadline, **query):
        method = self.METHODS.get(endpoint)
        if not method:
            raise SlackError('unrecognized Slack read method')
        suffix = '?' + urlencode(query) if query else ''
        raw, _ = self._request('slack.com', method, '/api/' + endpoint + suffix, deadline, MAX_PAGE, endpoint)
        try:
            result = json.loads(raw)
        except (UnicodeError, ValueError):
            result = None
        if not isinstance(result, dict) or result.get('ok') is not True:
            code = result.get('error') if isinstance(result, dict) else None
            detail = code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,80}', code) else 'invalid_response'
            raise SlackError(f'Slack {endpoint} failed ({detail})')
        return result

    def file_info(self, file_id, deadline):
        if not isinstance(file_id, str) or not FILE.fullmatch(file_id):
            raise SlackError('invalid Slack file identity')
        item = self.call('files.info', deadline, file=file_id).get('file')
        if (not isinstance(item, dict) or item.get('id') != file_id or item.get('mode') != 'hosted'
                or item.get('is_external') is not False or not isinstance(item.get('mimetype'), str)
                or type(item.get('size')) is not int or item['size'] < 1):
            raise SlackError('Slack did not confirm a hosted attachment with the requested identity')
        return item

    def download(self, item, team, deadline, limit):
        link = item.get('url_private_download') or item.get('url_private')
        url = urlsplit(link) if isinstance(link, str) else None
        prefix = f'/files-pri/{team}-{item["id"]}/'
        if (url is None or url.scheme != 'https' or url.netloc != 'files.slack.com' or url.fragment
                or not url.path.startswith(prefix) or not url.path.isascii() or not url.query.isascii()
                or any(part in ('', '.', '..') for part in unquote(url.path[len(prefix):]).split('/'))):
            raise SlackError('Slack attachment URL did not match the configured workspace and file')
        size = item['size']
        if not 1 <= size <= limit:
            raise SlackError('Slack attachment exceeded the permitted byte limit')
        path = url.path + ('?' + url.query if url.query else '')
        data, media = self._request('files.slack.com', 'GET', path, deadline, size, 'attachment download')
        if len(data) != size or media not in (item['mimetype'].lower(), 'application/octet-stream'):
            raise SlackError('Slack attachment length or media type changed')
        return data

    def check_team(self, team, deadline):
        info = self.call('auth.test', deadline)
        if info.get('team_id') != team:
            raise SlackError('Slack credential is not for the configured workspace')

    def messages(self, channel, root, deadline):
        cursor, seen, messages = None, set(), []
        while len(seen) < 16:
            query = {'cursor': cursor} if cursor else {}
            data = self.call('conversations.replies', deadline, channel=channel, ts=root, **query)
            rows = data.get('messages')
            if (not isinstance(rows, list) or not rows or len(messages) + len(rows) > MAX_MESSAGES
                    or data.get('channel', channel) != channel):
                raise SlackError('Slack did not provide a complete bounded source thread')
            messages.extend(rows)
            metadata = data.get('response_metadata') or {}
            cursor = metadata.get('next_cursor', '') if isinstance(metadata, dict) else None
            if not isinstance(cursor, str) or len(cursor) > 1000 or cursor in seen:
                raise SlackError('Slack thread pagination is invalid')
            if not cursor:
                if data.get('has_more') is True:
                    raise SlackError('Slack has more replies without a cursor')
                total = messages[0].get('reply_count') if isinstance(messages[0], dict) else None
                if total is not None and (type(total) is not int or total != len(messages) - 1):
                    raise SlackError('Slack replies do not match the source thread reply count')
                return messages
            seen.add(cursor)
        raise SlackError('Slack source thread exceeded the page limit')

    def permalink(self, channel, root, deadline):
        result = self.call('chat.getPermalink', deadline, conversation=channel, message_ts=root)
        link = result.get('permalink')
        url = urlsplit(link) if isinstance(link, str) else None
        if (result.get('channel') != channel or url is None or url.scheme != 'https'
                or not re.fullmatch(r'[a-z0-9-]+\.slack\.com', url.netloc)
                or url.path != f'/archives/{channel}/p{root.replace(".", "")}' or url.query or url.fragment):
            raise SlackError('Slack returned a permalink for different source coordinates')
        return link


class SlackWriter(Slack):
    """A separate credential can reply in one exact source thread."""

    def identity(self, team, user, deadline):
        info = self.call('auth.test', deadline)
        if (info.get('team_id'), info.get('user_id')) != (team, user):
            raise SlackError('Slack writer is not the configured triage identity')

    def reply(self, *, channel, root, text, deadline):
        if (not CHANNEL.fullmatch(channel) or not STAMP.fullmatch(root) or not isinstance(text, str)
                or not 1 <= len(text) <= 3500):
            raise ValueError('Slack reply needs a nonempty exact source thread and bounded text')
        payload = json.dumps({'channel': channel, 'thread_ts': root, 'text': text, 'mrkdwn': False, 'parse': 'none',
                              'unfurl_links': False, 'unfurl_media': False}, ensure_ascii=False).encode()
        raw, _ = self._request('slack.com', 'POST', '/api/chat.postMessage', deadline, MAX_PAGE,
                               'chat.postMessage', payload=payload)
        data = json.loads(raw)
        if not isinstance(data, dict) or type(data.get('ok')) is not bool:
            raise SlackError('Slack did not confirm the reply')
        if data['ok'] is False:
            raise SlackRejected('Slack rejected the source-thread reply')
        message, timestamp = data.get('message'), data.get('ts')
        if (data.get('channel') != channel or not isinstance(timestamp, str) or not STAMP.fullmatch(timestamp)
                or timestamp == root or not isinstance(message, dict) or message.get('thread_ts') != root):
            raise SlackError('Slack did not confirm reply coordinates')
        return timestamp
