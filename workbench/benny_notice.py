"""Send one confirmed reproduction notice to its original Slack thread."""

import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit

from workbench import benny, benny_handoff, cli
from workbench.benny_slack import STAMP, USER, SlackError
from workbench.task_lock import task_lock


KIND = 'benny-source-notice'
SUMMARY = 'Confirmed reproduction. Two independent checks each reproduced the reported behavior twice.'
ARCHIVE = re.compile(r'/archives/([CGD][A-Z0-9]{1,31})/p([1-9][0-9]{6,18})')
LINEAR_ISSUE = re.compile(r'https://linear\.app/[A-Za-z0-9_-]{1,100}/issue/[A-Za-z0-9_-]{1,50}'
                          r'(?:/[A-Za-z0-9_-]{1,150})?')


def _journal_path(path, reproduction):
    path = Path(path).expanduser()
    parent = path.parent.resolve(strict=True)
    expected = Path(reproduction['path']).resolve(strict=True).parent.parent/'source-notice.json'
    path = parent/path.name
    directory = parent.stat()
    if path != expected or directory.st_uid != os.getuid() or directory.st_mode & 0o022:
        raise ValueError('Benny source notice needs its original event journal')
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Benny source notice journal must be an owner-only regular file')
    return path


def _plan(reproduction, proof, route, identity, operations_link):
    frozen, rules = route['snapshot'], route['rules']
    if not isinstance(identity, str) or not USER.fullmatch(identity):
        raise ValueError('Benny source notice needs an explicit Slack writer identity')
    if (benny.route(frozen, rules, route['selected_artifact']) != route['decision']
            or route['decision']['status'] != 'eligible-for-reproduction'):
        raise ValueError('Benny source notice requires the approved reproducible report route')
    if operations_link is not None:
        link = urlsplit(operations_link) if isinstance(operations_link, str) else None
        archive = ARCHIVE.fullmatch(link.path) if link else None
        if (not archive or link.scheme != 'https' or link.netloc != urlsplit(frozen['permalink']).netloc
                or operations_link != f'https://{link.netloc}{link.path}'
                or archive[1] == frozen['location']['channel']):
            raise ValueError('Benny source notice needs an exact operations-thread permalink in the same workspace')
    tracker = route['decision']['verdict'].get('tracker')
    issue = re.compile(fr'https://github\.com/{re.escape(rules["repository"])}/issues/[1-9][0-9]{{0,9}}', re.I)
    if tracker and not (issue.fullmatch(tracker) or LINEAR_ISSUE.fullmatch(tracker)):
        tracker = None
    text = '\n'.join([SUMMARY, *([f'Operations {operations_link}'] if operations_link else []),
                      *([f'Tracker {tracker}'] if tracker else [])])
    return {'reproduction': reproduction, 'route': proof['route'], 'workspace': rules['workspace'],
            'location': frozen['location'], 'identity': identity, 'operations_link': operations_link,
            'tracker': tracker, 'text': text}


def _match(row, plan, after):
    if (not isinstance(row, dict) or row.keys() != {'ts', 'author', 'text', 'bot', 'files'}
            or row['author'] != plan['identity'] or type(row['bot']) is not bool or row['files'] != []
            or not isinstance(row['ts'], str) or not STAMP.fullmatch(row['ts']) or benny._ticks(row['ts']) <= after
            or not isinstance(row['text'], str)):
        return False
    text = row['text']
    for link in (plan['operations_link'], plan['tracker']):
        if link:
            text = text.replace(f'<{link}>', link)
    return text == plan['text']


def confirmed(reference, *, reproduction, proof):
    """Return the one recorded message that the source ownership gate may exclude."""
    if not isinstance(reference, dict) or reference.keys() != {'path', 'sha256'}:
        raise ValueError('invalid Benny source notice reference')
    path = _journal_path(reference['path'], reproduction)
    if (proof != benny_handoff._load(reproduction['path'], reproduction['sha256'], 'benny-reproduction')
            or proof.get('status') != 'reproduced' or proof.get('runtime_status') != 'reproduced'):
        raise ValueError('Benny source notice requires its approved reproduction proof')
    state = benny_handoff._load(path, reference['sha256'], KIND)
    _, route = benny.load_route(proof['route']['path'], proof['route']['sha256'])
    plan = state.get('plan')
    if (state.get('phase') != 'confirmed' or not isinstance(plan, dict)
            or plan != _plan(reproduction, proof, route, plan.get('identity'), plan.get('operations_link'))):
        raise ValueError('Benny source notice is not bound to the approved reproduction')
    row = state.get('reply')
    after = benny._ticks(route['snapshot']['messages'][-1]['ts'])
    if not _match(row, plan, after) or state.get('reply_ts', row['ts']) != row['ts']:
        raise ValueError('Benny source notice receipt does not match the confirmed reply')
    return row


def publish(*, path, reproduction, slack, writer, identity, operations_link=None):
    """Publish a notice once. A pending write can only be resumed by reading Slack."""
    from workbench import benny_reproduce

    evidence, proof, _, _ = benny_reproduce._approved(reproduction['path'], reproduction['sha256'])
    reproduction = {'path': str(evidence), 'sha256': reproduction['sha256']}
    path = _journal_path(path, reproduction)
    _, route = benny.load_route(proof['route']['path'], proof['route']['sha256'])
    plan = _plan(reproduction, proof, route, identity, operations_link)
    with task_lock(evidence, lock_name='.source-notice.lock'):
        path = _journal_path(path, reproduction)
        state = (benny_handoff._load(path, cli.digest(path), KIND) if path.exists()
                 else {'schema': cli.SCHEMA, 'kind': KIND, 'plan': plan, 'phase': 'ready'})
        if state.get('plan') != plan or state.get('phase') not in ('ready', 'posting', 'confirmed'):
            raise ValueError('Benny source notice journal has a different plan or invalid phase')
        if state['phase'] == 'confirmed':
            reference = {'path': str(path), 'sha256': cli.digest(path)}
            confirmed(reference, reproduction=reproduction, proof=proof)
            return reference
        if writer is None:
            raise ValueError('Benny source notice requires a separate authorized Slack writer')
        deadline = time.monotonic() + 45
        slack.check_team(plan['workspace'], deadline)
        writer.identity(plan['workspace'], identity, deadline)
        frozen = route['snapshot']
        if state['phase'] == 'ready':
            current = benny._snapshot(slack, plan['location'], deadline)
            if current != frozen:
                raise SlackError('Benny source changed before reproduction notice')
            if any(row['author'] == identity and row['text'].split('\n', 1)[0] == SUMMARY for row in current['messages'][1:]):
                raise SlackError('Benny source already contains a reproduction notice outside this journal')
            state = {**state, 'phase': 'posting'}
            cli.write(path, state)
            try:
                stamp = writer.reply(channel=plan['location']['channel'], root=plan['location']['root'],
                                     text=plan['text'], deadline=deadline)
            except (ValueError, OSError):
                pass
            else:
                state = {**state, 'reply_ts': stamp}
                cli.write(path, state)
        current = benny._snapshot(slack, plan['location'], deadline)
        after = benny._ticks(frozen['messages'][-1]['ts'])
        matches = [row for row in current['messages'][1:] if _match(row, plan, after)]
        if len(matches) != 1 or state.get('reply_ts', matches[0]['ts']) != matches[0]['ts']:
            raise SlackError('Benny source notice has no unique reply receipt')
        reply = matches[0]
        without_notice = {**current, 'messages': [row for row in current['messages'] if row != reply]}
        cli.write(path, {**state, 'phase': 'confirmed', 'reply': reply,
                         'source_changed_after_post': without_notice != frozen, 'confirmed_at': cli.now()})
        return {'path': str(path), 'sha256': cli.digest(path)}
