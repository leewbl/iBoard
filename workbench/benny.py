"""Bind Benny decisions and existing-fix verification to one Slack report."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from workbench import cli, delivery, pr_watch, project_verify, verify_existing_fix
from workbench.benny_slack import CHANNEL, FILE, MAX_MESSAGES, STAMP, TEAM, USER, RetryAfter, Slack, SlackError


DEFAULT_MARKERS = {'bug': '[benny:bug]', 'performance': '[benny:performance]', 'other': '[benny:other]'}
FIX_LINK = re.compile(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:pull/[1-9][0-9]*|commit/[0-9a-f]{40})(?![A-Za-z0-9_/?#-])')
FIX_LABEL = re.compile(r"^(?:\[benny:fix\]|fix\s*:|(?:the |a )?fix (?:is here|is in|is at)|fixed (?:in|by)|修复(?:在|见|提交))", re.I)
CLAIM = re.compile(
    r"^(?:\[benny:claim\]|i(?:['’]ll| will| am|['’]m) (?:fix|fixing|patch|patching|implement|implementing|take (?:this|the fix)|working on (?:this fix|the fix)|open (?:a|the) PR)\b"
    r"|i (?:can take|have started fixing) (?:this|the fix)\b|(?:please\s+)?<@[UW][A-Z0-9]+>\s+(?:please\s+)?(?:fix|implement|patch|open (?:a|the) PR)\b"
    r"|(?:这个我来修|我来修(?:复)?|我正在修(?:复)?|我负责修(?:复)?))", re.I)


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def config(path):
    raw = project_verify._bounded_json(path, 16_000)
    required = {'schema', 'workspace', 'channel', 'triage_identity', 'repository', 'verdict_wait_seconds',
                'poll_seconds', 'verification_seconds'}
    if not isinstance(raw, dict) or not required <= raw.keys() or raw.keys() - required - {'markers'}:
        raise ValueError('Benny needs workspace, channel, triage identity, repository and bounded budgets')
    rules = {**raw, 'markers': raw.get('markers', DEFAULT_MARKERS)}
    return _rules(rules)


def _rules(rules):
    fields = {'schema', 'workspace', 'channel', 'triage_identity', 'repository', 'verdict_wait_seconds',
              'poll_seconds', 'verification_seconds', 'markers'}
    if not isinstance(rules, dict) or set(rules) != fields or type(rules['schema']) is not int or rules['schema'] != 1:
        raise ValueError('invalid Benny configuration schema')
    for key, pattern in (('workspace', TEAM), ('channel', CHANNEL), ('triage_identity', USER),
                         ('repository', pr_watch.REPO)):
        if not _matches(pattern, rules[key]):
            raise ValueError(f'invalid Benny {key}')
    for key, low, high in (('verdict_wait_seconds', 0, 3600), ('poll_seconds', 1, 600),
                           ('verification_seconds', 1, 3600)):
        if type(rules[key]) is not int or not low <= rules[key] <= high:
            raise ValueError(f'invalid Benny {key}')
    markers = rules['markers']
    marker_pattern = re.compile(r'\[[a-z][a-z0-9_-]{0,29}:[a-z][a-z0-9_-]{0,29}\]')
    if (not isinstance(markers, dict) or markers.keys() != DEFAULT_MARKERS.keys()
            or not all(_matches(marker_pattern, item) for item in markers.values())
            or len(set(markers.values())) != len(markers)):
        raise ValueError('Benny requires separate bug, performance and other markers')
    return rules


def _trigger(rules, channel, message_ts, thread_ts):
    if (channel != rules['channel'] or not _matches(STAMP, message_ts)
            or (thread_ts is not None and (not _matches(STAMP, thread_ts) or _ticks(thread_ts) > _ticks(message_ts)))):
        raise SlackError('trigger does not identify a message in the configured source channel')
    return {'channel': channel, 'message_ts': message_ts, 'root': thread_ts or message_ts}


def _ticks(timestamp):
    seconds, fractional = timestamp.split('.')
    return int(seconds) * 1_000_000 + int(fractional)


def _snapshot(slack, location, deadline, *, permalink=None):
    channel, root = location['channel'], location['root']
    rows = slack.messages(channel, root, deadline)
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_MESSAGES:
        raise SlackError('source thread is missing or exceeds the configured read limit')
    seen, result, budget = set(), [], 256_000
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get('type') != 'message' or row.get('deleted'):
            raise SlackError('source thread contains a missing or malformed message')
        ts, user, text = row.get('ts'), row.get('user'), row.get('text')
        provider_bot = bool(row.get('bot_id') or row.get('app_id') or row.get('subtype') == 'bot_message')
        if (not _matches(STAMP, ts) or ts in seen or row.get('channel', channel) != channel
                or row.get('subtype') in ('message_deleted', 'tombstone', 'channel_leave', 'channel_join')
                or (not _matches(USER, user) and not (provider_bot and user is None))
                or not isinstance(text, str) or '\0' in text):
            raise SlackError('invalid Slack source message identity or text')
        if index == 0:
            if ts != root or row.get('thread_ts', root) != root:
                raise SlackError('Slack did not return the frozen root message first')
        elif row.get('thread_ts') != root or _ticks(ts) <= _ticks(root):
            raise SlackError('Slack returned a reply belonging to a different report')
        seen.add(ts)
        budget -= len(text.encode('utf-8'))
        if budget < 0:
            raise SlackError('source thread exceeds the text budget')
        files = row.get('files') or []
        if (not isinstance(files, list) or len(files) > 30 or any(not isinstance(file, dict)
                or not _matches(FILE, file.get('id')) for file in files)):
            raise SlackError('source thread attachment references are malformed')
        result.append({'ts': ts, 'author': user, 'text': text, 'bot': provider_bot,
                       'files': [file['id'] for file in files]})
    if location['message_ts'] not in seen:
        raise SlackError('the triggering message does not belong to the frozen thread')
    link = permalink if permalink is not None else slack.permalink(channel, root, deadline)
    return {'location': location, 'permalink': link, 'messages': sorted(result, key=lambda row: _ticks(row['ts']))}


def _lines(text):
    fenced = False
    for line in text.splitlines():
        if '```' in line:
            if line.count('```') % 2:
                fenced = not fenced
            continue
        line = line.strip()
        if not fenced and line and not line.startswith(('>', '&gt;', '`')):
            yield line


def _tracker(raw):
    if raw.startswith('<') and raw.endswith('>'):
        raw = raw[1:-1].split('|', 1)[0]
    url = urlsplit(raw)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment
            or not url.path or any(char.isspace() for char in raw) or len(raw) > 1000):
        raise ValueError('trusted verdict has an invalid tracker link')
    return raw


def _verdict(replies, rules):
    kinds = {value: key for key, value in rules['markers'].items()}
    check = re.compile(r'(' + '|'.join(map(re.escape, kinds)) + r')(?:\s+tracker=(.+))?')
    verdicts = []
    for row in replies:
        if row['author'] != rules['triage_identity']:
            continue
        lines = list(_lines(row['text']))
        marker_count = sum(line.count(marker) for marker in kinds for line in lines)
        if not marker_count:
            continue
        match = check.fullmatch(lines[-1])
        if marker_count != 1 or not match:
            return None, 'verdict-conflict'
        try:
            tracker = _tracker(match[2]) if match[2] else None
        except ValueError:
            return None, 'verdict-conflict'
        verdicts.append({'kind': kinds[match[1]], 'tracker': tracker, 'ts': row['ts'], 'author': row['author']})
    if not verdicts:
        return None, 'awaiting-verdict'
    if len({(row['kind'], row['tracker']) for row in verdicts}) != 1:
        return None, 'verdict-conflict'
    return verdicts[0], None


def _artifact(value, repository):
    found = FIX_LINK.fullmatch(value) if isinstance(value, str) else None
    if not found or not value.casefold().startswith(f'https://github.com/{repository.casefold()}/'):
        raise ValueError('existing fix must be a GitHub PR or full commit in the configured repository')
    return value


def route(snapshot, rules, selected_artifact=None):
    if selected_artifact is not None:
        _artifact(selected_artifact, rules['repository'])
    replies = snapshot['messages'][1:]
    verdict, pending = _verdict(replies, rules)
    result = {'verdict': verdict, 'artifact': None, 'claims': [], 'candidates': []}
    if pending or verdict['kind'] == 'other':
        return {**result, 'status': pending or 'out-of-scope'}
    if not snapshot['messages'][0]['text'].strip():
        return {**result, 'status': 'report-text-missing'}
    candidates = {}
    for row in replies:
        for line in _lines(row['text']):
            if not row['bot'] and CLAIM.match(line):
                if not any(item['ts'] == row['ts'] for item in result['claims']):
                    result['claims'].append({'author': row['author'], 'ts': row['ts']})
            if (not row['bot'] or row['author'] == rules['triage_identity']) and FIX_LABEL.match(line):
                for link in FIX_LINK.findall(line):
                    if link.casefold().startswith(f'https://github.com/{rules["repository"].casefold()}/'):
                        candidates.setdefault(link, row['ts'])
    result['candidates'] = [{'url': url, 'ts': ts} for url, ts in sorted(candidates.items())]
    chosen = selected_artifact or (next(iter(candidates)) if len(candidates) == 1 else None)
    if chosen:
        return {**result, 'status': 'verify-existing', 'artifact': chosen}
    if len(candidates) > 1:
        return {**result, 'status': 'artifact-conflict'}
    return {**result, 'status': 'claimed' if result['claims'] else 'eligible-for-reproduction'}


def freeze(*, rules, slack, channel, message_ts, thread_ts=None, artifact=None, out, wait=False):
    rules = _rules(rules)
    location = _trigger(rules, channel, message_ts, thread_ts)
    if artifact is not None:
        _artifact(artifact, rules['repository'])
    wait = wait and rules['verdict_wait_seconds'] > 0
    deadline = time.monotonic() + (rules['verdict_wait_seconds'] if wait else 30)
    slack.check_team(rules['workspace'], deadline)
    link = None
    while True:
        try:
            snapshot = _snapshot(slack, location, deadline, permalink=link)
        except RetryAfter as error:
            if not wait or deadline - time.monotonic() <= error.seconds:
                raise
            time.sleep(error.seconds)
            continue
        link = snapshot['permalink']
        decision = route(snapshot, rules, artifact)
        if not wait or decision['status'] != 'awaiting-verdict':
            break
        if deadline - time.monotonic() <= rules['poll_seconds']:
            decision['status'] = 'verdict-timeout'
            break
        time.sleep(rules['poll_seconds'])
    root = Path(out).expanduser().resolve()
    root.mkdir(mode=0o700, parents=True)
    path = root / 'route.json'
    cli.write(path, {'schema': cli.SCHEMA, 'kind': 'benny-source-route', 'rules': rules, 'snapshot': snapshot,
                     'selected_artifact': artifact, 'decision': decision})
    return {**decision, 'receipt': str(path), 'sha256': cli.digest(path)}


def load_route(receipt, sha256):
    route_file = Path(receipt).expanduser().resolve(strict=True)
    with route_file.open('rb') as stream:
        content = stream.read(768_001)
    if len(content) > 768_000 or hashlib.sha256(content).hexdigest() != sha256:
        raise ValueError('Benny route receipt does not match the approved digest')
    record = json.loads(content)
    if record.get('schema') != cli.SCHEMA or record.get('kind') != 'benny-source-route':
        raise ValueError('expected a frozen Benny route')
    rules = _rules(record['rules'])
    snapshot = record['snapshot']
    location = snapshot['location']
    if _trigger(rules, location['channel'], location['message_ts'], location['root']) != location:
        raise ValueError('invalid frozen Benny source coordinates')
    return route_file, record


def verify(*, slack, receipt, sha256, out, run_fix=verify_existing_fix.verify, **inputs):
    route_file, record = load_route(receipt, sha256)
    rules, snapshot = record['rules'], record['snapshot']
    location = snapshot['location']
    if (record['decision']['status'] != 'verify-existing'
            or route(snapshot, rules, record['selected_artifact']) != record['decision']):
        raise ValueError('Benny route has not selected an existing fix')
    started = time.monotonic()
    deadline = started + rules['verification_seconds']
    preflight = min(deadline, started + 30)
    slack.check_team(rules['workspace'], preflight)
    current = _snapshot(slack, location, preflight)
    if current != snapshot:
        raise SlackError('source thread changed since selection, freeze the current route again')
    remaining = int(deadline - time.monotonic())
    if remaining < 1:
        raise ValueError('Benny verification budget expired during source preflight')
    root = Path(out).expanduser().resolve()
    repo = Path(inputs['repo']).expanduser().resolve(strict=True)
    if root.is_relative_to(repo):
        raise ValueError('store Benny evidence outside the target checkout')
    root.mkdir(mode=0o700, parents=True)
    request = root / 'report.txt'
    request.write_text(snapshot['permalink'] + '\n\n' + current['messages'][0]['text'] + '\n', encoding='utf-8')
    result = run_fix(artifact=record['decision']['artifact'], report=request, out=root / 'verification',
                     timeout=remaining, **inputs)
    try:
        final_snapshot = _snapshot(slack, location, min(deadline, time.monotonic() + 30))
    except SlackError:
        final_snapshot = None
    source_status = 'unconfirmed' if final_snapshot is None else ('unchanged' if final_snapshot == snapshot else 'changed')
    outcome = {'unconfirmed': 'source-unconfirmed', 'changed': 'source-changed'}.get(source_status, result['outcome'])
    output = {'schema': cli.SCHEMA, 'kind': 'benny-reproduction-result', 'source': snapshot['permalink'],
              'source_route': {'receipt': str(route_file), 'sha256': sha256}, 'artifact': record['decision']['artifact'],
              'outcome': outcome, 'runtime_outcome': result['outcome'], 'source_recheck': source_status,
              'verification': 'verification/result.json'}
    cli.write(root / 'result.json', output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for verb in ('freeze', 'verify'):
        command = commands.add_parser(verb)
        command.add_argument('--token-file', required=True)
        command.add_argument('--out', required=True)
    freeze_cmd = commands.choices['freeze']
    for name in ('config', 'channel', 'message-ts'):
        freeze_cmd.add_argument('--' + name, required=True)
    freeze_cmd.add_argument('--thread-ts')
    freeze_cmd.add_argument('--artifact')
    freeze_cmd.add_argument('--wait', action='store_true')
    verify_cmd = commands.choices['verify']
    for name in ('receipt', 'sha256', 'repo', 'image', 'manifest', 'helpers', 'journey', 'oracle', 'oracle-marker',
                 'oracle-failure-marker'):
        verify_cmd.add_argument('--' + name, required=True)
    verify_cmd.add_argument('--expected-base')
    verify_cmd.add_argument('--expected-head')
    verify_cmd.add_argument('--memory-mb', type=int, default=3328)
    args = vars(parser.parse_args(argv))
    action = args.pop('command')
    try:
        slack = Slack(args.pop('token_file'))
        if action == 'freeze':
            result = freeze(rules=config(args.pop('config')), slack=slack, **args)
            visible = {key: result[key] for key in ('status', 'receipt', 'sha256')}
            exit_code = 0
        else:
            result = verify(slack=slack, **args)
            visible = {'outcome': result['outcome'], 'result': str(Path(args['out']).resolve()/'result.json')}
            exit_code = 0 if result['outcome'] == 'confirmed' else 1
    except (*project_verify.ERRORS, delivery.DeliveryError, KeyError, UnicodeError) as error:
        parser.error(str(error))
    print(json.dumps(visible, ensure_ascii=False))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
