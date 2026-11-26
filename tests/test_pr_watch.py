from copy import deepcopy
import unittest

from workbench import delivery, pr_watch


REPO = 'user/fork'


def pull(number=1, **changes):
    return {'number':number, 'url':f'https://github.com/{REPO}/pull/{number}', 'isDraft':False,
            'state':'OPEN', 'isCrossRepository':False, 'headRefOid':str(number)*40, 'baseRefOid':'0'*40,
            'headRefName':f'work-{number}', 'baseRefName':'main', 'mergeable':'MERGEABLE',
            'mergeStateStatus':'CLEAN', 'reviewDecision':'', 'statusCheckRollup':[], **changes}


def thread(ident, *, resolved=False, outdated=False):
    return {'id':ident, 'isResolved':resolved, 'isOutdated':outdated, 'path':'src/main.py', 'line':10,
            'comments':{'nodes':[{'author':{'login':'reviewer'}, 'body':'Check the changed caller.',
                                  'url':'https://github.com/user/fork/pull/1#discussion_r1'}]}}


class Reader:
    def __init__(self, *rows):
        self.rows = {row['number']:row for row in rows}
        self.pages = {}
        self.thread_calls = []
        self.on_thread = lambda _:None

    def pull(self, repo, number):
        if repo != REPO:
            raise AssertionError('wrong repository')
        return deepcopy(self.rows[number])

    def thread_page(self, repo, number, cursor):
        self.thread_calls.append((number, cursor))
        self.on_thread(number)
        nodes, more = self.pages.get((number, cursor), ([], None))
        return {**self.pull(repo, number),
                'reviewThreads':{'pageInfo':{'hasNextPage':more is not None, 'endCursor':more}, 'nodes':nodes}}

    def open_pulls(self, repo):
        return [row for row in self.rows.values() if row['state'] == 'OPEN']


class Clock:
    elapsed = 0

    def now(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


class PrWatchTests(unittest.TestCase):
    def test_conflict_precedes_threads_and_checks_in_a_real_stack_shape(self):
        root = pull()
        child = pull(2, baseRefName=root['headRefName'], baseRefOid=root['headRefOid'],
                     mergeable='CONFLICTING', mergeStateStatus='DIRTY')
        reader = Reader(root, child)
        reader.on_thread = lambda _:self.fail('queried threads behind a higher-priority conflict')
        result = pr_watch.watch(REPO, [1, 2], reader=reader)
        self.assertEqual((result['frontier'], result['reason']), (2, 'merge-conflict'))
        self.assertEqual(result['members'][1]['needs_rebase'], 'work-2')
        self.assertIsNone(result['members'][0]['checks'])

    def test_paginates_threads_and_preserves_outdated_open_discussions(self):
        reader = Reader(pull(statusCheckRollup=[{'status':'COMPLETED', 'conclusion':'FAILURE'}]))
        reader.pages = {(1, None):([thread('closed', resolved=True)], 'next'),
                        (1, 'next'):([thread('open', outdated=True)], None)}
        result = pr_watch.watch(REPO, 1, reader=reader)
        self.assertEqual(result['reason'], 'unresolved-review-threads')
        self.assertEqual(reader.thread_calls, [(1, None), (1, 'next')])
        self.assertIsNone(result['checks'])
        self.assertEqual([entry['id'] for entry in result['threads']], ['open'])
        self.assertTrue(result['threads'][0]['outdated'])
        reader.pages[1, 'next'] = ([thread('closed')], None)
        with self.assertRaisesRegex(pr_watch.WatchError, 'duplicate'):
            pr_watch.watch(REPO, 1, reader=reader)

    def test_changed_head_never_reuses_earlier_thread_or_check_observations(self):
        reader = Reader(pull())
        reader.on_thread = lambda _:reader.rows[1].update(headRefOid='a'*40)
        result = pr_watch.watch(REPO, 1, reader=reader)
        self.assertEqual(result['reason'], 'inconsistent-snapshot')
        self.assertNotIn('checks', result)
        result = pr_watch.watch(REPO, 1, expected_head='1'*40, reader=reader)
        self.assertEqual(result['reason'], 'head-changed')
        self.assertEqual(result['head'], 'a'*40)
        self.assertEqual(reader.thread_calls, [(1, None)])

    def test_drive_waits_for_checks_and_keeps_first_head_and_base_pinned(self):
        root = pull(statusCheckRollup=[{'name':'unit','status':'IN_PROGRESS'}])
        reader, clock, updates = Reader(root), Clock(), []

        def sleep(seconds):
            clock.sleep(seconds)
            root['statusCheckRollup'][0].update(status='COMPLETED', conclusion='SUCCESS')

        result = pr_watch.watch(REPO, 1, mode='drive', interval=2, timeout=15, reader=reader,
                                clock=clock.now, sleep=sleep, emit=updates.append)
        self.assertEqual(result['status'], 'github-ready')
        self.assertEqual(result['sequence'], 2)
        self.assertEqual(updates[0]['reason'], 'pending-checks')
        root['reviewDecision'] = 'REVIEW_REQUIRED'
        clock.elapsed = 0

        def move_base(seconds):
            clock.sleep(seconds)
            root['baseRefOid'] = 'b'*40

        result = pr_watch.watch(REPO, 1, mode='drive', interval=2, timeout=15, reader=reader,
                                clock=clock.now, sleep=move_base)
        self.assertEqual(result['reason'], 'base-drift')
        self.assertEqual(result['expected_base'], '0'*40)

    def test_drive_bounds_retries_and_wait_seconds(self):
        clock = Clock()
        reader = Reader(pull())

        def offline(*_):
            raise delivery.DeliveryError('GitHub command timed out')

        reader.pull = offline
        timed = pr_watch.watch(REPO, 1, mode='drive', timeout=15, interval=5, reader=reader,
                               clock=clock.now, sleep=clock.sleep)
        self.assertEqual(clock.elapsed, 15)
        self.assertEqual((timed['status'], timed['last_reason']), ('timed-out', 'github-temporarily-unavailable'))
        clock.elapsed = 0
        blocked = pr_watch.watch(REPO, 1, mode='drive', timeout=180, interval=5, reader=reader,
                                  clock=clock.now, sleep=clock.sleep)
        self.assertEqual(clock.elapsed, 90)
        self.assertEqual((blocked['sequence'], blocked['reason']), (3, 'status-query-retries-exhausted'))

    def test_discovery_includes_siblings_and_validates_the_dependency_commits(self):
        root = pull()
        first = pull(2, baseRefName='work-1', baseRefOid='1'*40)
        second = pull(3, baseRefName='work-1', baseRefOid='1'*40, isDraft=True)
        unrelated = pull(4)
        reader = Reader(root, first, second, unrelated)
        result = pr_watch.watch(REPO, 2, discover=True, reader=reader)
        self.assertEqual(result['prs'], [1, 2, 3])
        self.assertEqual(result['dependencies'], {'2':1, '3':1})
        self.assertEqual((result['frontier'], result['reason']), (3, 'draft-pr'))
        second['baseRefOid'] = 'e'*40
        result = pr_watch.watch(REPO, 2, discover=True, reader=reader)
        self.assertEqual((result['frontier'], result['reason']), (3, 'stack-base-drift'))
        self.assertEqual(result['expected_base'], '1'*40)

    def test_discovery_does_not_use_a_fork_head_as_a_parent(self):
        forked = pull(isCrossRepository=True)
        other = pull(2, baseRefName='work-1')
        result = pr_watch.watch(REPO, 2, discover=True, reader=Reader(forked, other))
        self.assertEqual(result['number'], 2)
        self.assertNotIn('members', result)

    def test_statuses_distinguish_failed_unknown_and_missing_checks(self):
        root = pull()
        reader = Reader(root)
        empty = pr_watch.watch(REPO, 1, reader=reader)
        self.assertEqual((empty['reason'], empty['checks']), ('no-checks-observed', []))
        root['statusCheckRollup'] = [{'state':'FAILURE','context':'CI'}]
        self.assertEqual(pr_watch.watch(REPO, 1, reader=reader)['reason'], 'failing-checks')
        root['statusCheckRollup'] = [{'status':'COMPLETED','conclusion':None}]
        self.assertEqual(pr_watch.watch(REPO, 1, reader=reader)['reason'], 'unknown-check-state')
        thread_result = pr_watch.watch(REPO, 1, mode='threads-only', reader=reader)
        self.assertEqual(thread_result['status'], 'threads-clear')
        self.assertIsNone(thread_result['checks'])
