"""Apply an approved Benny triage handoff to GitHub and the exact Slack thread."""

import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import time

from workbench import benny, benny_media, benny_tracker, benny_triage, cli, delivery, pr_watch, project_verify
from workbench.benny_slack import Slack, SlackError, SlackRejected, SlackWriter
from workbench.task_lock import TaskBusy, task_lock


WRITES = {'draft-new-issue', 'confident-duplicate'}
TERMINAL = {'confirmed', 'compensated', 'source-changed', 'prior-verdict', 'source-already-linked', 'reply-rejected'}


def _load(path, sha256, kind):
    with Path(path).open('rb') as stream:
        content = stream.read(1_500_001)
    if len(content) > 1_500_000 or hashlib.sha256(content).hexdigest() != sha256:
        raise ValueError(f'{kind} receipt differs from the approved digest')
    result = json.loads(content)
    if not isinstance(result, dict) or result.get('schema') != cli.SCHEMA or result.get('kind') != kind:
        raise ValueError(f'expected a {kind} receipt')
    return result


def _text(value):
    # Preserve the local evidence verbatim. Suppress notification markup in outgoing summaries.
    return ' '.join(value.split()).replace('@', '@\u200b').replace('<!', '<\u200b!')


def _issue_body(fields, marker):
    labels = [('Reporter quote', 'reporter_quote'), ('Expected', 'expected'), ('Observed', 'observed'),
              ('Trigger', 'trigger'), ('Frequency', 'frequency'), ('Version', 'version'), ('Environment', 'environment')]
    lines = [f'**{label}**\n{_text(fields[key])}' for label, key in labels]
    trace = fields['trace']
    return '\n\n'.join([*lines, f'**Source report**\n{fields["source"]}',
                        f'**Source tracing ({trace["basis"]})**\n{_text(trace["finding"])}', marker])


def _preview(plan, issue=None):
    recipient = plan['source']
    lines = [plan['summary']]
    if plan['action'] == 'existing-fix':
        lines.append('Fix: ' + plan['link'])
    elif plan['link'] and not issue:
        lines.append(plan['link'])
    if plan['question']:
        lines.append(plan['question'])
    if plan['destination']:
        lines.append('Destination: ' + plan['destination'])
    if plan['unread']:
        lines.append(f'Unread or partial attachments: {plan["unread"]}.')
    mark = plan['marker_line'] + (f' tracker={issue}' if issue else '')
    return {'channel': recipient['channel'], 'root': recipient['root'], 'text': '\n'.join([*lines, mark])}


def prepare(*, triage, sha256, state_dir, labels=(), followup_seconds=0):
    if type(followup_seconds) is not int or not 0 <= followup_seconds <= 3600:
        raise ValueError('choose a follow-up window between 0 and 3600 seconds')
    result_path = Path(triage).expanduser().resolve(strict=True)
    triage_result = _load(result_path, sha256, 'benny-triage')
    if triage_result.get('status') != 'ready' or not isinstance(triage_result.get('proposal'), dict):
        raise ValueError('handoff needs a ready Benny triage proposal')
    source_ref = triage_result['route']
    _, route = benny.load_route(source_ref['path'], source_ref['sha256'])
    snapshot, rules = route['snapshot'], route['rules']
    location = snapshot['location']
    packet_path = result_path.parent/'evidence.json'
    if cli.digest(packet_path) != triage_result['evidence_sha256']:
        raise ValueError('Benny triage evidence changed after review')
    packet = cli.read(packet_path)
    proposal, answer = triage_result['proposal'], triage_result['assessment']
    unread = packet['unread_media_count']
    if (proposal['source'] != location or packet['source'] != snapshot['permalink']
            or triage_result['source'] != snapshot['permalink'] or proposal['unread_media_count'] != unread
            or not benny_media.unchanged(result_path.parent/'attachments', packet.get('attachments', []))):
        raise ValueError('triage proposal is no longer bound to the original evidence')
    destination = benny_triage._validate(answer, packet['evidence'], packet['routes'])
    computed = benny_triage._recommend(answer, packet['evidence'], packet['searches'], packet['tracker_read'],
                                       destination, snapshot['permalink'], unread)
    endorsed = {k: v for k, v in triage_result['recommendation'].items() if k != 'attachment_refs'}
    if computed != endorsed:
        raise ValueError('handoff recommendation differs from the verified evidence')
    action, link, repo = computed['action'], computed.get('url'), packet.get('tracker_repository')
    if action in WRITES and (not isinstance(repo, str) or not pr_watch.REPO.fullmatch(repo)):
        raise ValueError('issue handoff needs the exact tracker repository from triage')
    issue = None
    if action == 'confident-duplicate':
        match = re.fullmatch(fr'https://github\.com/{re.escape(repo)}/issues/([1-9][0-9]{{0,9}})', link or '', re.I)
        if not match:
            raise ValueError('duplicate issue is outside the configured GitHub tracker')
        issue = int(match[1])
    if action == 'existing-fix':
        benny._artifact(link, rules['repository'])
    if (len(labels) > 12 or any(not isinstance(item, str) or not item.isprintable() or not 1 <= len(item) <= 80
                               for item in labels) or len(set(labels)) != len(labels)
            or (labels and action != 'draft-new-issue')):
        raise ValueError('optional issue labels must be distinct and belong to a new issue plan')
    marker_kind = answer['category'] if answer['category'] in ('bug', 'performance') and (
        action in WRITES or action == 'existing-fix') else 'other'
    marker_line = rules['markers'][marker_kind]
    if not isinstance(proposal['marker'], str) or proposal['marker'].partition(' ')[0] != marker_line:
        raise ValueError('proposed verdict marker was changed')
    outbound = [_text(answer['summary']), _text(answer['question'] or ''), _text(computed.get('destination', ''))]
    if any(mark in text for text in outbound for mark in rules['markers'].values()):
        raise ValueError('outgoing summary must not introduce another verdict marker')
    identity = hashlib.sha256(json.dumps([rules['workspace'], location['channel'], location['root']]).encode()).hexdigest()
    marker = f'<!-- iboard-benny:{identity} -->'
    plan = {'schema': cli.SCHEMA, 'kind': 'benny-handoff-plan', 'id': identity,
            'triage': {'path': str(result_path), 'sha256': sha256}, 'route': source_ref,
            'source': location, 'permalink': snapshot['permalink'], 'rules': rules,
            'action': action, 'tracker': repo, 'issue': issue, 'labels': list(labels), 'link': link,
            'marker': marker, 'marker_line': marker_line, 'unread': unread,
            'summary': outbound[0], 'question': outbound[1], 'destination': outbound[2],
            'followup_seconds': followup_seconds}
    if action == 'draft-new-issue':
        plan['title'] = _text(computed['title'])
        plan['body'] = _issue_body(computed['fields'], marker)
    elif action == 'confident-duplicate':
        plan['comment'] = 'Another report of this behavior: ' + snapshot['permalink'] + '\n\n' + marker
    directory = Path(state_dir).expanduser().resolve()/identity
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    guard = directory/'lock-target'
    guard.touch(mode=0o600, exist_ok=True)
    path = directory/'plan.json'
    with task_lock(guard, reentrant=False):
        if path.exists():
            if cli.read(path) != plan:
                raise ValueError('this source thread already has a different handoff plan in the journal')
        else:
            cli.write(path, plan)
    return {'plan': str(path), 'sha256': cli.digest(path), 'action': action, 'tracker': repo,
            'reply_preview': _preview(plan, link if issue else None)}


def _normalize(text):
    return html.unescape(re.sub(r'<(https://[^>|]+)(?:\|[^>]+)?>', r'\1', text))


class Handoff:
    def __init__(self, path, sha256, plan, source, slack, writer, tracker, deadline):
        self.path = path.parent/'journal.json'
        self.slack, self.writer, self.tracker = slack, writer, tracker
        self.plan, self.source, self.deadline = plan, source, deadline
        self.repo, self.marker = plan['tracker'], plan['marker']
        self.state = cli.read(self.path) if self.path.exists() else {'schema': cli.SCHEMA, 'plan_sha256': sha256,
                                                                   'phase': 'ready', 'status': 'ready', 'events': []}
        if self.state['plan_sha256'] != sha256:
            raise ValueError('handoff journal does not match its approved plan')

    def save(self, phase, status=None, **fields):
        self.state.update(phase=phase, status=status or phase, **fields)
        self.state['events'].append({'phase': phase, 'status': self.state['status'], 'time': cli.now()})
        cli.write(self.path, self.state)
        return self.state

    def thread(self):
        return benny._snapshot(self.slack, self.plan['source'], self.deadline)

    def created_issue(self, row):
        if (not benny_tracker.owns(row, self.state['tracker_actor'], self.marker)
                or row.get('body') != self.plan['body']):
            raise delivery.DeliveryError('GitHub issue is not the exact workbench-created issue')
        return row['number']

    def checked_comments(self):
        return self.tracker.comments(self.repo, self.plan['issue'])

    def reconcile_comment(self):
        expected = self.plan['comment']
        found = [row for row in self.checked_comments() if row.get('body') == expected
                 and benny_tracker.owns(row, self.state['tracker_actor'], self.marker)]
        if len(found) == 1:
            self.save('tracker-ready', issue=self.plan['issue'], comment_id=found[0]['id'])
        else:
            self.save('commenting', 'comment-unconfirmed')

    def recover_tracker(self):
        if self.state['phase'] == 'creating':
            item = self.tracker.recent(self.repo, self.state['tracker_actor'], self.marker)
            if item is None:
                return self.save('creating', 'issue-unconfirmed')
            self.save('tracker-ready', created=True, issue=self.created_issue(item))
        elif self.state['phase'] == 'commenting':
            self.reconcile_comment()
        return self.state

    def start_tracker(self):
        action = self.plan['action']
        if action not in WRITES:
            return self.save('tracker-ready')
        self.tracker.preflight(self.repo, self.plan['labels'], create=action == 'draft-new-issue')
        if self.tracker.source_linked(self.repo, self.plan['permalink']):
            return self.save('source-already-linked')
        actor = self.tracker.identity()
        if action == 'draft-new-issue':
            existing = self.tracker.recent(self.repo, actor, self.marker)
            if existing is not None:
                return self.save('source-already-linked', issue=existing['number'])
            if self.thread() != self.source:
                return self.save('source-changed')
            self.save('creating', tracker_actor=actor)
            try:
                created = self.tracker.create(self.repo, self.plan['title'], self.plan['body'], self.plan['labels'])
                number = self.created_issue(self.tracker.read(self.repo, self.created_issue(created)))
                return self.save('tracker-ready', created=True, issue=number)
            except (delivery.DeliveryError, OSError):
                return self.recover_tracker()
        number = self.plan['issue']
        issue = self.tracker.read(self.repo, number)
        if issue['state'] != 'open':
            return self.save('source-changed', reason='duplicate tracker issue is no longer open')
        if any(self.plan['permalink'] in str(row.get('body', '')) for row in [issue, *self.checked_comments()]):
            return self.save('source-already-linked', issue=number)
        if self.thread() != self.source:
            return self.save('source-changed')
        self.save('commenting', tracker_actor=actor)
        try:
            self.tracker.comment(self.repo, number, self.plan['comment'])
        except (delivery.DeliveryError, OSError):
            pass
        return self.recover_tracker()

    def compensate(self, reason):
        if not self.state.get('created'):
            return self.save(reason)
        self.save('compensating', compensation_reason=reason)
        number = self.state['issue']
        row = self.tracker.read(self.repo, number)
        self.created_issue(row)
        if row['state'] != 'closed':
            try:
                self.tracker.close(self.repo, number)
            except (delivery.DeliveryError, OSError):
                pass
            row = self.tracker.read(self.repo, number)
            self.created_issue(row)
        if row['state'] != 'closed' or row.get('state_reason') != 'not_planned':
            return self.save('compensating', 'compensation-unconfirmed')
        return self.save('compensated', issue_url=benny_tracker.issue_url(self.repo, number))

    def confirm_reply(self, *, rejected=False):
        current = self.thread()
        expected = _normalize(self.state['outbound']['text'])
        replies = [row for row in current['messages'][1:] if row['author'] == self.plan['rules']['triage_identity']
                   and _normalize(row['text']) == expected]
        if len(replies) == 1 and self.state.get('slack_ts', replies[0]['ts']) == replies[0]['ts']:
            others = [row for row in current['messages'] if row != replies[0]]
            return self.save('confirmed', slack_ts=replies[0]['ts'], confirmed_at=cli.now(),
                             source_changed_after_post=others != self.source['messages'])
        if rejected and not replies:
            return self.compensate('reply-rejected')
        return self.save('rejected' if rejected else 'posting', 'reply-unconfirmed')

    def post(self):
        number = self.state.get('issue')
        url = benny_tracker.issue_url(self.repo, number) if number else None
        if number:
            existing = self.tracker.read(self.repo, number)
            if self.state.get('created'):
                self.created_issue(existing)
            if existing['state'] != 'open':
                return self.save('source-changed', reason='tracker issue is no longer open')
        if self.thread() != self.source:
            return self.compensate('source-changed')
        reply = _preview(self.plan, url)
        self.save('posting', outbound=reply, issue_url=url)
        try:
            stamp = self.writer.reply(**reply, deadline=self.deadline)
            self.save('posting', slack_ts=stamp)
        except SlackRejected:
            self.save('rejected')
            return self.confirm_reply(rejected=True)
        except (SlackError, ValueError, OSError):
            pass
        return self.confirm_reply()

    def run(self):
        phase = self.state['phase']
        if phase in TERMINAL:
            return self.state
        if (phase != 'posting' and self.state.get('tracker_actor')
                and self.tracker.identity() != self.state['tracker_actor']):
            raise delivery.DeliveryError('GitHub tracker account changed during handoff')
        if phase == 'compensating':
            return self.compensate(self.state['compensation_reason'])
        rules = self.plan['rules']
        self.slack.check_team(rules['workspace'], self.deadline)
        self.writer.identity(rules['workspace'], rules['triage_identity'], self.deadline)
        if phase == 'posting':
            return self.confirm_reply()
        if phase == 'rejected':
            return self.confirm_reply(rejected=True)
        if phase == 'ready':
            current = self.thread()
            verdict, status = benny._verdict(current['messages'][1:], rules)
            if verdict is not None or status == 'verdict-conflict':
                return self.save('prior-verdict')
            if current != self.source:
                return self.save('source-changed')
            self.start_tracker()
        elif phase != 'tracker-ready':
            self.recover_tracker()
        if self.state['phase'] == 'tracker-ready':
            return self.post()
        return self.state


def apply(*, plan, sha256, slack, writer, tracker=None, seconds=90):
    if type(seconds) is not int or not 10 <= seconds <= 600:
        raise ValueError('handoff needs a 10–600 second limit')
    path = Path(plan).expanduser().resolve(strict=True)
    updated = _load(path, sha256, 'benny-handoff-plan')
    if path.name != 'plan.json' or path.parent.name != updated['id']:
        raise ValueError('handoff must use its original source-keyed journal directory')
    tracker = tracker or benny_tracker.Tracker()
    deadline = time.monotonic() + seconds
    if isinstance(tracker, benny_tracker.Tracker):
        tracker.set_deadline(deadline)
    with task_lock(path.parent/'lock-target', reentrant=False):
        _load(path, sha256, 'benny-handoff-plan')
        _, record = benny.load_route(updated['route']['path'], updated['route']['sha256'])
        original = record['snapshot']
        if ((updated['source'], updated['permalink'], updated['rules'])
                != (original['location'], original['permalink'], record['rules'])):
            raise ValueError('handoff plan does not match the frozen Slack source')
        handoff = Handoff(path, sha256, updated, original, slack, writer, tracker, deadline)
        try:
            if handoff.state['phase'] == 'ready':
                triage_ref = updated['triage']
                result = _load(triage_ref['path'], triage_ref['sha256'], 'benny-triage')
                root = Path(triage_ref['path']).parent
                evidence = root/'evidence.json'
                if (cli.digest(evidence) != result['evidence_sha256']
                        or not benny_media.unchanged(root/'attachments', cli.read(evidence).get('attachments', []))):
                    return handoff.save('source-changed', reason='approved triage evidence changed')
            return handoff.run()
        except (SlackError, delivery.DeliveryError, ValueError, OSError) as error:
            return handoff.save(handoff.state['phase'], 'pending-review', reason=str(error)[:350])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    ready = commands.add_parser('prepare')
    for name in ('triage', 'sha256', 'state-dir'):
        ready.add_argument('--' + name, required=True)
    ready.add_argument('--label', dest='labels', action='append', default=[])
    ready.add_argument('--followup-seconds', type=int, default=0)
    send = commands.add_parser('apply')
    for name in ('plan', 'sha256', 'read-token-file', 'write-token-file'):
        send.add_argument('--' + name, required=True)
    send.add_argument('--seconds', type=int, default=90)
    args = vars(parser.parse_args(argv))
    action = args.pop('command')
    try:
        if action == 'prepare':
            result = prepare(**args)
        else:
            slack = Slack(args.pop('read_token_file'))
            writer = SlackWriter(args.pop('write_token_file'))
            result = apply(slack=slack, writer=writer, **args)
    except (*project_verify.ERRORS, TaskBusy, KeyError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False))
    return int(action == 'apply' and result['status'] not in ('confirmed', 'prior-verdict', 'source-already-linked'))


if __name__ == '__main__':
    raise SystemExit(main())
