"""Observe the GitHub merge frontier without modifying the pull request."""

import argparse
import json
import re
import sys
import time

from workbench import cli, delivery


PSTACK_SOURCE = 'skills/poteto-mode/playbooks/babysit.md'
REPO = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*')
SHA = re.compile(r'[a-f0-9]{40}')
THREAD_QUERY = '''
query($owner:String!, $repo:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      number headRefOid baseRefOid state isDraft
      reviewThreads(first:50, after:$cursor) {
        pageInfo { endCursor hasNextPage }
        nodes {
          id isResolved isOutdated path line
          comments(first:1) {
            nodes { author { login } url body outdated createdAt }
          }
        }
      }
    }
  }
}
'''
PENDING = {'QUEUED', 'IN_PROGRESS', 'WAITING', 'REQUESTED', 'PENDING', 'EXPECTED'}
PASSING = {'SUCCESS', 'NEUTRAL', 'SKIPPED'}
FAILING = {'FAILURE', 'ERROR', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STALE', 'STARTUP_FAILURE'}
MODES = ('check', 'drive', 'threads-only')
TRANSIENT = re.compile(
    r'timed?\s*out|connection reset|connection refused|network is unreachable|unexpected EOF|HTTP\s+50[0234]|'
    r'temporary failure|deadline exceeded', re.IGNORECASE,
)


class WatchError(Exception):
    pass


class SnapshotChanged(WatchError):
    pass


class GitHubReader(delivery.GitHub):
    def set_deadline(self, deadline):
        self._deadline = deadline

    def pull(self, repo, number):
        fields = ('number,url,state,isDraft,isCrossRepository,headRefName,headRefOid,'
                  'baseRefName,baseRefOid,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup')
        return self._call('pr', 'view', str(number), '--repo', repo, '--json', fields)

    def open_pulls(self, repo):
        return self._call('pr', 'list', '--repo', repo, '--state', 'open', '--limit', '201',
                          '--json', 'number,headRefName,baseRefName,isCrossRepository')

    def thread_page(self, repo, number, cursor):
        owner, name = repo.split('/')
        args = ['api', 'graphql', '--method', 'POST', '-f', 'query=' + THREAD_QUERY,
                '-f', 'owner=' + owner, '-f', 'repo=' + name, '-F', f'number={number}']
        if cursor is not None:
            args.extend(('-f', 'cursor=' + cursor))
        data = self._call(*args)
        try:
            if data.get('errors'):
                raise WatchError('GitHub reported incomplete GraphQL data')
            return data['data']['repository']['pullRequest']
        except (AttributeError, KeyError, TypeError) as error:
            raise WatchError('GitHub did not return the requested review threads') from error


def _identity(pull):
    return tuple(pull.get(key) for key in ('number', 'headRefOid', 'baseRefOid', 'state', 'isDraft'))


def _facts(reader, repo, number):
    pull = reader.pull(repo, number)
    canonical = f'https://github.com/{repo}/pull/{number}'
    if (not isinstance(pull, dict) or type(pull.get('number')) is not int or pull['number'] != number
            or not isinstance(pull.get('url'), str) or pull['url'].casefold() != canonical.casefold()
            or pull.get('state') not in ('OPEN', 'MERGED', 'CLOSED') or type(pull.get('isDraft')) is not bool
            or type(pull.get('isCrossRepository')) is not bool):
        raise WatchError('GitHub PR identity or lifecycle is unavailable')
    if any(not isinstance(pull.get(k), str) or not SHA.fullmatch(pull[k]) for k in ('baseRefOid', 'headRefOid')):
        raise WatchError('GitHub did not identify the PR base and head commits')
    if any(not isinstance(pull.get(k), str) or not pull[k] for k in ('baseRefName', 'headRefName')):
        raise WatchError('GitHub did not identify the PR branches')
    return pull


def _threads(reader, repo, pull):
    number = pull['number']
    cursor, seen, unresolved = None, set(), []
    for _ in range(10):
        current = reader.thread_page(repo, number, cursor)
        if not isinstance(current, dict):
            raise WatchError('GitHub review thread page is unavailable')
        if _identity(current) != _identity(pull):
            raise SnapshotChanged('PR identity moved while reading review threads')
        connection = current.get('reviewThreads')
        if not isinstance(connection, dict):
            raise WatchError('GitHub review thread connection is unavailable')
        page, nodes = connection.get('pageInfo'), connection.get('nodes')
        if (not isinstance(page, dict) or type(page.get('hasNextPage')) is not bool
                or not isinstance(nodes, list) or len(nodes) > 50):
            raise WatchError('GitHub returned an incomplete review thread page')
        for thread in nodes:
            if (not isinstance(thread, dict) or not isinstance(thread.get('id'), str) or not thread['id']
                    or thread['id'] in seen or type(thread.get('isResolved')) is not bool
                    or type(thread.get('isOutdated')) is not bool):
                raise WatchError('GitHub returned an invalid or duplicate review thread')
            seen.add(thread['id'])
            if thread['isResolved']:
                continue
            comments = thread.get('comments')
            messages = comments.get('nodes') if isinstance(comments, dict) else None
            message = messages[0] if isinstance(messages, list) and messages and isinstance(messages[0], dict) else {}
            author = message.get('author')
            body = message.get('body')
            unresolved.append({
                'id':thread['id'], 'path':thread.get('path'), 'line':thread.get('line'),
                'outdated':thread['isOutdated'], 'url':message.get('url'),
                'author':author.get('login') if isinstance(author, dict) else None,
                'body_excerpt':body[:1600] if isinstance(body, str) else None,
                'excerpt_truncated':isinstance(body, str) and len(body) > 1600,
                'created_at':message.get('createdAt'),
            })
        if not page['hasNextPage']:
            return unresolved
        next_cursor = page.get('endCursor')
        if not isinstance(next_cursor, str) or not next_cursor or cursor == next_cursor or not nodes:
            raise WatchError('GitHub review thread pagination did not advance')
        cursor = next_cursor
    raise WatchError('review thread count exceeds the inspection bound')


def _checks(rollup):
    if not isinstance(rollup, list) or len(rollup) > 1000:
        raise WatchError('GitHub check results are unavailable or exceed the inspection bound')
    observed = []
    for check in rollup:
        if not isinstance(check, dict):
            raise WatchError('GitHub returned an invalid check result')
        name = check.get('name') or check.get('context') or 'unnamed check'
        status = check.get('status') or check.get('state')
        outcome = check.get('conclusion') if status == 'COMPLETED' else status
        if not isinstance(status, str) or (outcome is not None and not isinstance(outcome, str)):
            state = 'unknown'
        elif status in PENDING:
            state = 'pending'
        elif outcome in PASSING:
            state = 'passed'
        elif outcome in FAILING:
            state = 'failed'
        else:
            state = 'unknown'
        observed.append({'name':name, 'workflow':check.get('workflowName'), 'state':state,
                         'status':status, 'outcome':outcome,
                         'url':check.get('detailsUrl') or check.get('targetUrl')})
    return observed


def _describe(pull, repo, mode):
    return {
        'schema':1, 'mode':mode, 'repo':repo, 'number':pull['number'], 'url':pull['url'],
        'state':pull['state'], 'draft':pull['isDraft'], 'cross_repository':pull['isCrossRepository'],
        'head':pull['headRefOid'], 'head_branch':pull['headRefName'],
        'base':pull['baseRefOid'], 'base_branch':pull['baseRefName'],
        'observed_at':cli.now(), 'checks':None, 'threads':None,
        'mergeability':pull.get('mergeable'), 'merge_state':pull.get('mergeStateStatus'),
        'review_decision':pull.get('reviewDecision'),
    }


def _initial(pull, repo, mode, expected_head, expected_base):
    report = _describe(pull, repo, mode)
    if expected_head and report['head'] != expected_head:
        report.update(status='blocked', reason='head-changed', expected_head=expected_head)
    elif pull['state'] == 'MERGED':
        report.update(status='merged', reason='github-merged')
    elif pull['state'] == 'CLOSED':
        report.update(status='blocked', reason='closed-without-merge')
    elif report['mergeability'] == 'CONFLICTING' or report['merge_state'] in ('DIRTY', 'CONFLICTING'):
        report.update(status='blocked', reason='merge-conflict', needs_rebase=report['head_branch'],
                      next='inspect new base callers and reconcile them with the branch')
    elif (expected_base and report['base'] != expected_base) or report['merge_state'] == 'BEHIND':
        report.update(status='blocked', reason='base-drift', expected_base=expected_base,
                      needs_rebase=report['head_branch'],
                      next='sweep the new base for affected callers and revalidate the candidate')
    else:
        report.update(status='observing', reason='review-pending')
    return report


def _fresh_pull(reader, repo, pull):
    # Re-read the check rollup after paging and discard observations of a moved PR.
    latest = _facts(reader, repo, pull['number'])
    fields = ('headRefName', 'baseRefName', 'mergeable', 'mergeStateStatus', 'reviewDecision', 'isCrossRepository')
    if _identity(latest) != _identity(pull) or any(latest.get(key) != pull.get(key) for key in fields):
        raise SnapshotChanged('PR identity or mergeability moved while reading its merge frontier')
    return latest


def _classify(report, pull):
    def outcome(status, reason):
        return {**report, 'status':status, 'reason':reason}

    if report['threads']:
        return outcome('blocked', 'unresolved-review-threads')
    if report['mode'] == 'threads-only':
        return outcome('threads-clear', 'no-unresolved-review-threads')
    report['checks'] = _checks(pull.get('statusCheckRollup'))
    if any(check['state'] == 'failed' for check in report['checks']):
        return outcome('blocked', 'failing-checks')
    if any(check['state'] == 'unknown' for check in report['checks']):
        return outcome('blocked', 'unknown-check-state')
    if report['draft']:
        return outcome('blocked', 'draft-pr')
    if report['review_decision'] == 'CHANGES_REQUESTED':
        return outcome('blocked', 'changes-requested')
    if report['review_decision'] == 'REVIEW_REQUIRED':
        return outcome('waiting', 'review-required')
    if any(check['state'] == 'pending' for check in report['checks']):
        return outcome('waiting', 'pending-checks')
    if report['mergeability'] == 'UNKNOWN' or report['merge_state'] == 'UNKNOWN':
        return outcome('waiting', 'github-calculating-mergeability')
    if report['mergeability'] != 'MERGEABLE' or report['merge_state'] != 'CLEAN':
        return outcome('blocked', 'github-merge-gate')
    return outcome('github-ready', 'checks-observed' if report['checks'] else 'no-checks-observed')


def snapshot(reader, repo, number, mode, expected_head, expected_base):
    pull = _facts(reader, repo, number)
    report = _initial(pull, repo, mode, expected_head, expected_base)
    if report['status'] != 'observing':
        return report
    report['threads'] = _threads(reader, repo, pull)
    return _classify(report, _fresh_pull(reader, repo, pull))


def _stack(reader, repo, numbers, mode, pins):
    pulls = [_facts(reader, repo, number) for number in numbers]
    members = [_initial(pull, repo, mode, *pins.get(pull['number'], (None, None))) for pull in pulls]
    dependencies = {}
    report = {'members':members, 'dependencies':dependencies}

    def frontier(member, status, reason, **details):
        return {**report, 'status':status, 'reason':reason, 'frontier':member['number'], **details}

    for member in members:
        if member['status'] == 'blocked':
            return frontier(member, 'blocked', member['reason'])
    parents = {members[0]['head_branch']:members[0]} if not members[0]['cross_repository'] else {}
    for child in members[1:]:
        parent = parents.get(child['base_branch'])
        if parent is None:
            return frontier(child, 'blocked', 'disconnected-stack', expected_base_branch=child['base_branch'])
        if child['base'] != parent['head']:
            return frontier(child, 'blocked', 'stack-base-drift', expected_parent=parent['number'],
                            expected_base=parent['head'])
        dependencies[str(child['number'])] = parent['number']
        if not child['cross_repository']:
            if child['head_branch'] in parents:
                return frontier(child, 'blocked', 'ambiguous-stack-branch')
            parents[child['head_branch']] = child
    for member, pull in zip(members, pulls):
        if member['status'] == 'observing':
            member['threads'] = _threads(reader, repo, pull)
            if member['threads']:
                member.update(status='blocked', reason='unresolved-review-threads')
                _fresh_pull(reader, repo, pull)
                return frontier(member, 'blocked', member['reason'])
    # Earlier members can move while a later member's threads are being read.
    latest = [_fresh_pull(reader, repo, pull) for pull in pulls]
    for index, member in enumerate(members):
        if member['status'] == 'observing':
            members[index] = _classify(member, latest[index])
    for member in members:
        if member['reason'] in ('failing-checks', 'unknown-check-state'):
            return frontier(member, 'blocked', member['reason'])
    for status in ('blocked', 'waiting'):
        for member in members:
            if member['status'] == status:
                return frontier(member, status, member['reason'])
    if all(member['status'] == 'merged' for member in members):
        return {**report, 'status':'merged', 'reason':'all-observed-merged', 'frontier':None}
    status = 'threads-clear' if mode == 'threads-only' else 'github-ready'
    return {**report, 'status':status, 'reason':'stack-frontier-clear', 'frontier':None}


def _discover(reader, repo, selected):
    items = reader.open_pulls(repo)
    if not isinstance(items, list) or len(items) > 200:
        raise WatchError('stack discovery requires a complete list of at most 200 open pull requests')
    by_number, by_head, children = {}, {}, {}
    for item in items:
        if (not isinstance(item, dict) or type(item.get('number')) is not int or item['number'] < 1
                or item['number'] in by_number or type(item.get('isCrossRepository')) is not bool
                or any(not isinstance(item.get(key), str) or not item[key] for key in ('headRefName', 'baseRefName'))):
            raise WatchError('GitHub returned incomplete or duplicate stack metadata')
        by_number[item['number']] = item
        children.setdefault(item['baseRefName'], []).append(item)
        if not item['isCrossRepository']:
            by_head.setdefault(item['headRefName'], []).append(item)
    root = by_number.get(selected)
    if root is None:
        return (selected,)
    ancestors = {selected}
    while parents := by_head.get(root['baseRefName']):
        if len(parents) != 1 or parents[0]['number'] in ancestors:
            raise WatchError('PR stack discovery encountered an ambiguous dependency or cycle')
        root = parents[0]
        ancestors.add(root['number'])
    ordered, visited = [], set()
    pending = [root]
    while pending:
        item = pending.pop()
        if item['number'] in visited:
            raise WatchError('PR stack discovery encountered a dependency cycle')
        ordered.append(item['number'])
        visited.add(item['number'])
        if len(ordered) > 15:
            raise WatchError('discovered PR stack exceeds 15 members')
        if not item['isCrossRepository']:
            pending.extend(sorted(children.get(item['headRefName'], []), key=lambda node:node['number'], reverse=True))
    return tuple(ordered)


def watch(repo, number, *, mode='check', expected_head=None, expected_base=None, interval=30,
          timeout=600, discover=False, reader=None, clock=time.monotonic, sleep=time.sleep, emit=lambda _:None):
    numbers = (number,) if type(number) is int else tuple(number) if isinstance(number, (list, tuple)) else ()
    if (not isinstance(repo, str) or not REPO.fullmatch(repo) or not 1 <= len(numbers) <= 15
            or any(type(n) is not int or not 1 <= n <= 2_147_483_647 for n in numbers)
            or len(set(numbers)) != len(numbers) or mode not in MODES
            or (discover and len(numbers) != 1)
            or type(interval) is not int or not 1 <= interval <= 1800
            or type(timeout) is not int or not 1 <= timeout <= 86400
            or any(value is not None and (not isinstance(value, str) or not SHA.fullmatch(value))
                   for value in (expected_head, expected_base))):
        raise WatchError('provide a GitHub repository, distinct PR numbers, valid mode, timeout and pinned commits')
    if reader is None:
        reader = GitHubReader()
    deadline = clock() + timeout
    if isinstance(reader, GitHubReader):
        reader.set_deadline(time.monotonic() + timeout)
    selected = numbers[0]
    if discover:
        try:
            numbers = _discover(reader, repo, selected)
        except delivery.DeliveryError as error:
            raise WatchError('GitHub PR stack could not be discovered: ' + str(error)) from error
    stack = len(numbers) > 1
    scope = {'prs':list(numbers)} if stack else {'number':numbers[0]}
    pins = {selected:(expected_head, expected_base)}
    sequence, errors = 0, 0
    last = None
    while clock() < deadline:
        sequence += 1
        try:
            report = (_stack(reader, repo, numbers, mode, pins) if stack else
                      snapshot(reader, repo, numbers[0], mode, *pins[numbers[0]]))
        except SnapshotChanged as error:
            report = {'status':'waiting', 'reason':'inconsistent-snapshot', 'detail':str(error)}
            errors += 1
        except delivery.DeliveryError as error:
            errors += 1
            if mode != 'drive' or not TRANSIENT.search(str(error)):
                raise WatchError('GitHub PR status could not be retrieved: ' + str(error)) from error
            report = {'status':'waiting', 'reason':'github-temporarily-unavailable'}
        else:
            errors = 0
            for member in (report['members'] if stack else [report]):
                previous_head, previous_base = pins.get(member['number'], (None, None))
                pins[member['number']] = (previous_head or member['head'], previous_base or member['base'])
        last = {'schema':1, 'repo':repo, **scope, 'mode':mode, 'observed_at':cli.now(),
                **report, 'sequence':sequence}
        if clock() >= deadline:
            break
        if mode != 'drive' or last['status'] != 'waiting':
            return {**last, 'terminal':True}
        if errors == 3:
            return {**last, 'status':'blocked', 'reason':'status-query-retries-exhausted', 'terminal':True}
        emit({**last, 'terminal':False})
        delay = min(300, max(interval, 30) * 2 ** (errors - 1)) if errors else interval
        sleep(min(delay, max(0, deadline - clock())))
    return {**(last or {}), 'schema':1, 'repo':repo, **scope, 'mode':mode,
            'sequence':sequence, 'observed_at':cli.now(), 'status':'timed-out', 'reason':'watch-deadline',
            'last_reason':last['reason'] if last else None, 'terminal':True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, help='GitHub owner/repo')
    parser.add_argument('--pr', required=True, type=int, action='append', help='PR number, repeat root-to-tip for a stack')
    parser.add_argument('--discover-stack', action='store_true', help='Find the open dependency stack of one selected PR')
    parser.add_argument('--mode', choices=MODES, default='check')
    parser.add_argument('--head', help='Expected head commit SHA of the first PR')
    parser.add_argument('--base', help='Expected base commit SHA of the first PR')
    parser.add_argument('--interval', type=int, default=30, help='Seconds between drive-mode polls')
    parser.add_argument('--timeout', type=int, default=600, help='Total watch limit in seconds')
    args = parser.parse_args(argv)
    try:
        report = watch(args.repo, args.pr, mode=args.mode, expected_head=args.head, expected_base=args.base,
                       interval=args.interval, timeout=args.timeout, discover=args.discover_stack,
                       emit=lambda update:print(json.dumps(update, ensure_ascii=False), file=sys.stderr, flush=True))
    except (OSError, WatchError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] in ('github-ready', 'merged', 'threads-clear') else 1


if __name__ == '__main__':
    raise SystemExit(main())
