"""Publish bounded Benny status to one explicitly configured operations channel."""

import hashlib
import json
from pathlib import Path
import re
import time

from workbench import cli, pr_watch
from workbench.benny_slack import CHANNEL, STAMP, TEAM, USER, SlackError


DRAFT_OPENED = 'Draft pull request opened'
STATUSES = frozenset({'Reproducing', 'Could not reproduce', 'Blocked', 'Reproduced', 'Verifying existing fix',
                      'Existing fix verified', 'Attempting bounded fix', 'Fix verified', DRAFT_OPENED,
                      'Fix did not land'})
SECONDS = 45


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


class Operations:
    """Reconcile durable intents under the dispatcher's event lock."""

    def __init__(self, path, *, workspace, source_channel, source_root, channel, identity, repository, slack, writer):
        for pattern, value in ((TEAM, workspace), (CHANNEL, source_channel), (STAMP, source_root),
                               (CHANNEL, channel), (USER, identity), (pr_watch.REPO, repository)):
            if not _matches(pattern, value):
                raise ValueError('Benny operations needs exact workspace, source, channel, identity and repository')
        if channel == source_channel:
            raise ValueError('Benny operations channel must differ from the source channel')
        self.path = Path(path)
        self.scope = {'workspace': workspace, 'source_channel': source_channel, 'source_root': source_root,
                      'channel': channel, 'identity': identity, 'repository': repository}
        digest = hashlib.sha256(json.dumps(self.scope, sort_keys=True).encode()).hexdigest()
        self.marker = 'Benny run ' + digest[:24]
        self.channel, self.identity = channel, identity
        self.slack, self.writer = slack, writer

    def _load(self):
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ValueError('Benny operations journal must be a regular file')
        if not self.path.exists():
            return {'schema': cli.SCHEMA, 'kind': 'benny-operations', 'scope': self.scope, 'marker': self.marker}
        state = cli.read(self.path)
        if (state.get('kind') != 'benny-operations' or state.get('scope') != self.scope
                or state.get('marker') != self.marker):
            raise ValueError('Benny operations journal belongs to a different run or destination')
        root, status, draft = state.get('root'), state.get('status'), state.get('draft')
        if (('root' in state or 'status' in state) and (not _matches(STAMP, root) or not self._is_status(status))
                or 'draft' in state and (status != DRAFT_OPENED or not isinstance(draft, dict)
                    or not self._is_draft_url(draft.get('url')) or not _matches(STAMP, draft.get('timestamp')))):
            raise ValueError('Benny operations journal has an invalid confirmed receipt')
        return state

    def _save(self, state, **changes):
        record = {**state, **changes}
        cli.write(self.path, record)
        return record

    def _done(self, state, **changes):
        return self._save({key: value for key, value in state.items() if key != 'intent'}, **changes)

    def _authorize(self, deadline):
        if self.writer is None:
            raise SlackError('Benny operations requires an explicitly configured Slack writer')
        workspace = self.scope['workspace']
        self.slack.check_team(workspace, deadline)
        self.writer.identity(workspace, self.identity, deadline)

    def _text(self, status):
        return f'{status}\n{self.marker}'

    def _is_status(self, value):
        return isinstance(value, str) and value in STATUSES

    def _is_draft_url(self, value):
        repository = re.escape(self.scope['repository'])
        return isinstance(value, str) and re.fullmatch(fr'https://github\.com/{repository}/pull/[1-9][0-9]{{0,9}}', value)

    def _draft_text(self, url):
        return f'{DRAFT_OPENED}. {url}'

    def _read_root(self, row, timestamp):
        if (not _matches(STAMP, timestamp) or not isinstance(row, dict) or row.get('type') != 'message'
                or row.get('subtype') in ('message_deleted', 'tombstone')
                or row.get('deleted') or row.get('ts') != timestamp or row.get('thread_ts', timestamp) != timestamp
                or row.get('channel', self.channel) != self.channel or row.get('user') != self.identity
                or not isinstance(row.get('text'), str)):
            raise SlackError('Slack did not confirm the original Benny operations root and author')
        return row['text']

    def _thread(self, timestamp, deadline):
        if not _matches(STAMP, timestamp):
            raise SlackError('Benny operations root has an invalid timestamp')
        rows = self.slack.messages(self.channel, timestamp, deadline)
        if not isinstance(rows, list) or not rows:
            raise SlackError('Slack did not return the Benny operations thread')
        return self._read_root(rows[0], timestamp), rows[1:]

    def _roots(self, oldest, deadline):
        rows = self.slack.history(self.channel, oldest=oldest, deadline=deadline)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise SlackError('Slack did not return the bounded operations history')
        return [row for row in rows if isinstance(row.get('text'), str)
                and row['text'].endswith('\n' + self.marker)]

    def _confirm_root(self, state, deadline):
        intent = state['intent']
        timestamp = intent.get('timestamp')
        if timestamp is None:
            matches = self._roots(intent['oldest'], deadline)
            if len(matches) != 1:
                raise SlackError('Benny operations root has no unique receipt; no second root was sent')
            timestamp = matches[0].get('ts')
            self._read_root(matches[0], timestamp)
        text, _ = self._thread(timestamp, deadline)
        if text != self._text(intent['status']):
            raise SlackError('Benny operations root does not match the original status intent')
        return self._done(state, root=timestamp, status=intent['status'])

    def _update_root(self, state, deadline):
        root, status = state['root'], state['intent']['status']
        target = self._text(status)
        current, _ = self._thread(root, deadline)
        if current != target:
            if current != self._text(state['status']):
                raise SlackError('Benny operations root changed outside this journal')
            try:
                self.writer.update_status(channel=self.channel, root=root, text=target, deadline=deadline)
            except (OSError, ValueError):
                pass
            current, _ = self._thread(root, deadline)
        if current != target:
            raise SlackError('Benny operations status update is unconfirmed')
        return self._done(state, status=status)

    def _draft_replies(self, state, url, deadline):
        root = state['root']
        text, replies = self._thread(root, deadline)
        if text != self._text(DRAFT_OPENED):
            raise SlackError('Benny operations root changed before the draft reply')
        expected = {self._draft_text(url), self._draft_text(f'<{url}>')}
        return [row for row in replies if isinstance(row, dict) and isinstance(row.get('text'), str)
                and row['text'] in expected]

    def _confirm_draft(self, state, deadline):
        intent = state['intent']
        replies = self._draft_replies(state, intent['url'], deadline)
        if len(replies) != 1:
            raise SlackError('Benny operations draft reply has no unique receipt; no second reply was sent')
        row, root = replies[0], state['root']
        timestamp = row.get('ts')
        if (not _matches(STAMP, timestamp) or timestamp == root or timestamp != intent.get('timestamp', timestamp)
                or row.get('type') != 'message' or row.get('deleted') or row.get('thread_ts') != root
                or row.get('channel', self.channel) != self.channel or row.get('user') != self.identity):
            raise SlackError('Slack did not confirm the exact Benny draft reply and author')
        return self._done(state, draft={'url': intent['url'], 'timestamp': timestamp})

    def _reconcile(self, state, deadline):
        intent = state.get('intent')
        if intent is None:
            return state
        handlers = {'root': self._confirm_root, 'update': self._update_root, 'draft': self._confirm_draft}
        if not isinstance(intent, dict) or not isinstance(intent.get('kind'), str) or intent['kind'] not in handlers:
            raise ValueError('Benny operations journal has an invalid pending intent')
        kind = intent['kind']
        if ((kind in ('root', 'update') and not self._is_status(intent.get('status')))
                or (kind == 'root' and not _matches(STAMP, intent.get('oldest')))
                or (kind in ('update', 'draft') and not state.get('root'))
                or (kind == 'draft' and not self._is_draft_url(intent.get('url')))
                or ('timestamp' in intent and not _matches(STAMP, intent.get('timestamp')))):
            raise ValueError('Benny operations journal has an invalid pending receipt')
        return handlers[kind](state, deadline)

    def _set_status(self, state, status, deadline):
        state = self._reconcile(state, deadline)
        if state.get('status') == status:
            return state
        if 'draft' in state:
            raise ValueError('Benny operations already confirmed its draft reply')
        if state.get('root'):
            state = self._save(state, intent={'kind': 'update', 'status': status})
            return self._update_root(state, deadline)
        oldest = f'{max(1, int(time.time()) - 60)}.000000'
        if self._roots(oldest, deadline):
            raise SlackError('Benny operations found an existing root outside this journal')
        intent = {'kind': 'root', 'status': status, 'oldest': oldest}
        state = self._save(state, intent=intent)
        try:
            timestamp = self.writer.operations_root(channel=self.channel, text=self._text(status), deadline=deadline)
        except (OSError, ValueError):
            pass
        else:
            state = self._save(state, intent={**intent, 'timestamp': timestamp})
        return self._confirm_root(state, deadline)

    def _receipt(self, state):
        return {key: state[key] for key in ('root', 'status', 'draft') if key in state} | {'channel': self.channel}

    def status(self, status):
        if not self._is_status(status):
            raise ValueError('Benny operations status must be a registered plain status')
        state = self._load()
        if state.get('status') == status and 'intent' not in state:
            return self._receipt(state)
        deadline = time.monotonic() + SECONDS
        self._authorize(deadline)
        return self._receipt(self._set_status(state, status, deadline))

    def draft(self, url):
        if not self._is_draft_url(url):
            raise ValueError('Benny operations draft requires an exact pull request URL in the bound repository')
        state = self._load()
        intent = state.get('intent')
        known = state.get('draft') or (intent if isinstance(intent, dict) and intent.get('kind') == 'draft' else None)
        if known and known.get('url') != url:
            raise ValueError('Benny operations draft is already bound to a different pull request')
        if 'draft' in state and 'intent' not in state:
            return self._receipt(state)
        deadline = time.monotonic() + SECONDS
        self._authorize(deadline)
        state = self._set_status(state, DRAFT_OPENED, deadline)
        if 'draft' in state:
            return self._receipt(state)
        if self._draft_replies(state, url, deadline):
            raise SlackError('Benny operations found a draft reply outside this journal')
        intent = {'kind': 'draft', 'url': url}
        state = self._save(state, intent=intent)
        try:
            timestamp = self.writer.reply(channel=self.channel, root=state['root'], text=self._draft_text(url),
                                          deadline=deadline)
        except (OSError, ValueError):
            pass
        else:
            state = self._save(state, intent={**intent, 'timestamp': timestamp})
        return self._receipt(self._confirm_draft(state, deadline))
