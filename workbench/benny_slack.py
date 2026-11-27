"""Read a single approved Slack report and its replies using the public Web API."""

import http.client
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlencode, urlsplit


CHANNEL = re.compile(r'[CGD][A-Z0-9]{1,31}')
TEAM = re.compile(r'T[A-Z0-9]{1,31}')
USER = re.compile(r'[UW][A-Z0-9]{1,31}')
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
    METHODS = {'auth.test': 'POST', 'conversations.replies': 'GET', 'chat.getPermalink': 'GET'}

    def __init__(self, token_file, *, connection=http.client.HTTPSConnection):
        self.token = _token(token_file)
        self.connection = connection

    def call(self, endpoint, deadline, **query):
        method = self.METHODS.get(endpoint)
        if not method:
            raise SlackError('unrecognized Slack read method')
        left = deadline - time.monotonic()
        if left <= 0:
            raise SlackError('Benny report read exceeded its deadline')
        client = self.connection('slack.com', timeout=min(left, 12))
        suffix = '?' + urlencode(query) if query else ''
        try:
            client.request(method, '/api/' + endpoint + suffix, headers={'Authorization': 'Bearer ' + self.token})
            response = client.getresponse()
            if response.status == 429:
                retry = response.getheader('Retry-After', '60')
                delay = int(retry) if re.fullmatch(r'[0-9]{1,5}', retry) else 60
                raise RetryAfter(max(1, delay))
            if response.status != 200:
                raise SlackError(f'Slack {endpoint} returned HTTP {response.status}')
            raw = response.read(MAX_PAGE + 1)
        except (OSError, http.client.HTTPException) as error:
            raise SlackError(f'Slack {endpoint} could not be read') from error
        finally:
            client.close()
        try:
            result = json.loads(raw) if len(raw) <= MAX_PAGE else None
        except (UnicodeError, ValueError):
            result = None
        if not isinstance(result, dict) or result.get('ok') is not True:
            code = result.get('error') if isinstance(result, dict) else None
            detail = code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,80}', code) else 'invalid_response'
            raise SlackError(f'Slack {endpoint} failed ({detail})')
        return result

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
