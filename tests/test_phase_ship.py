from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, delivery, phase_dispatch, phase_ship, publish


REPO = 'alice/project'


class LocalFork:
    """Real bare Git remote with local receipts standing in for GitHub metadata."""

    def __init__(self, checkout, remote, trunk):
        self.checkout = checkout
        self.remote = remote
        self.records = {}
        self.writes = []
        self.pending = None
        self.squash = False
        self.lose_publication = False
        self.lose_merge = False
        self.extra_advance = False
        remote.mkdir()
        cli.git(remote, 'init', '--bare', '-q')
        cli.git(checkout, 'push', str(remote), trunk + ':refs/heads/main')

    def login(self):
        return 'alice'

    def repository(self, repo):
        assert repo == REPO
        return {'fork': True, 'default_branch': 'main', 'owner': {'login': 'alice'}}

    def branch(self, repo, name):
        return cli.sha(self.remote, f'refs/heads/{name}')

    def remote_head(self, repo, name):
        if any(item['headRefName'] == name for item in self.records.values()):
            return self.branch(repo, name)
        return None

    def pulls(self, repo, branch):
        return [dict(item) for item in self.records.values()
                if item['headRefName'] == branch and item['state'] == 'OPEN']

    def pull(self, repo, number):
        return dict(self.records[number])

    def thread_page(self, repo, number, cursor):
        return {**self.pull(repo, number), 'reviewThreads': {'nodes': [], 'pageInfo': {'hasNextPage': False}}}

    def create(self, task, repo, *, github):
        assert github is self
        data = cli.read(task)
        assert data['status'] == 'running'
        head, base = cli.sha(data['repo'], 'HEAD'), data['base_commit']
        assert self.branch(repo, 'main') == base
        branch = f"workbench/{data['id']}"
        number = len(self.records) + 1
        cli.git(data['repo'], 'push', str(self.remote), f'{head}:refs/heads/{branch}')
        self.records[number] = {'number': number, 'url': f'https://github.com/{repo}/pull/{number}',
                                'state': 'OPEN', 'isDraft': False, 'isCrossRepository': False,
                                'headRefName': branch, 'headRefOid': head, 'baseRefName': 'main',
                                'baseRefOid': base, 'mergeStateStatus': 'CLEAN', 'mergeable': 'MERGEABLE',
                                'statusCheckRollup': [{'name': 'CI', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}
        self.writes.append(('publish', number))
        if self.lose_publication:
            self.lose_publication = False
            raise publish.PublishError('publication response was lost')
        return {'repo': repo, 'head': head, 'pr': number}

    def _advance(self, number, member):
        head, base = member['head'], member['base']
        landing = head
        if self.squash and number == 1:
            tree = cli.git(self.checkout, 'rev-parse', head + '^{tree}')
            landing = cli.git(self.checkout, 'commit-tree', tree, '-p', base, '-m', 'Squash first verified phase')
        self.records[number].update(state='MERGED', landed=landing)
        remote_head = landing
        if self.extra_advance:
            tree = cli.git(self.checkout, 'rev-parse', head + '^{tree}')
            remote_head = cli.git(self.checkout, 'commit-tree', tree, '-p', landing, '-m', 'Other unrelated change')
        delivery._fast_forward_push(self.checkout, str(self.remote), base, remote_head)

    def merge(self, task, repo, number, github, *, stack_binding, revalidate, deadline):
        assert github is self
        assert stack_binding['ci_workflows'] == ['CI']
        assert cli.read(task)['status'] == 'running'
        revalidate()
        self.writes.append(('merge', number))
        if self.lose_merge:
            self.lose_merge = False
            self.pending = (number, stack_binding)
            raise delivery.DeliveryError('merge response was lost')
        self._advance(number, stack_binding)
        return {'merged': True, 'verified_head': stack_binding['head']}

    def exact_landing(self, repo, member, observed):
        return self.records[member['number']].get('landed') == observed


class PhaseShipTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.checkout = self.root / 'checkout'
        self.checkout.mkdir()
        cli.git(self.checkout, 'init', '-q')
        cli.git(self.checkout, 'config', 'user.name', 'Fixture')
        cli.git(self.checkout, 'config', 'user.email', 'fixture@example.invalid')
        self.trunk = self.commit('trunk.txt', 'trunk')
        self.first = self.commit('label.txt', 'label')
        self.second = self.commit('notes.txt', 'notes')
        self.fork = LocalFork(self.checkout, self.root / 'fork.git', self.trunk)
        self.dispatch = self.root / 'dispatch'
        self.dispatch.mkdir()
        (self.dispatch / '.dispatch').touch()
        self.manifest, self.plan = self.root / 'approved.json', self.root / 'plan.json'
        cli.write(self.manifest, {'approved': True})
        cli.write(self.plan, {'summary': 'ordered fixture'})
        self.digest = cli.digest(self.manifest)
        self.entries = [dict(id='Label', base='trunk', depends_on=[], interaction=False),
                        dict(id='Notes', base='Label', depends_on=['Label'], interaction=True)]
        self.contract = {'destination': REPO, 'repo': str(self.checkout), 'manifest_path': str(self.manifest),
                         'manifest_sha256': self.digest, 'plan_path': str(self.plan),
                         'plan_sha256': cli.digest(self.plan), 'plan': {'baseline': self.trunk, 'phases': self.entries},
                         'seconds': 600}
        self.perf = {'path': str(self.root / 'benchmark.json'), 'sha256': 'a' * 64}
        self.tasks = {}
        self.approved = {self.second}
        self.replays = []
        self.substitute_replay = False
        self.proofs = {}
        self.started = cli.now()
        phases = {}
        for entry, parent, head in zip(self.entries, [self.trunk, self.first], [self.first, self.second]):
            key = entry['id']
            directory = self.dispatch / 'tasks' / phase_dispatch._task_id(self.dispatch, key)
            task = self.task(directory, key, parent, head)
            self.tasks[key] = task
            phases[key] = {'baseline_perf': self.perf, 'result': self.proofs[str(task)]}
        self.index = self.dispatch / 'run.json'
        cli.write(self.index, {'schema': cli.SCHEMA, 'manifest_sha256': self.digest,
                               'started_at': self.started, 'phases': phases})

        for module, name, callback in ((phase_ship.phase_contract, 'load', lambda *_: self.contract),
                                       (phase_ship.phase_contract, 'read', lambda data: data.get('replay_context')),
                                       (phase_dispatch, '_bound_task', self.bound),
                                       (phase_dispatch, '_verified', self.verified),
                                       (delivery, '_independent_verification', self.independent)):
            stub = patch.object(module, name, side_effect=callback)
            self.addCleanup(stub.stop)
            stub.start()

    def commit(self, filename, content):
        (self.checkout / filename).write_text(content)
        cli.git(self.checkout, 'add', filename)
        cli.git(self.checkout, 'commit', '-qm', content)
        return cli.sha(self.checkout, 'HEAD')

    def task(self, directory, phase, parent, head):
        workspace = directory / 'workspace'
        directory.mkdir(parents=True)
        cli.git(self.checkout, 'worktree', 'add', '--detach', str(workspace), head)
        task = directory / 'task.json'
        data = {'schema': cli.SCHEMA, 'id': directory.name, 'phase': phase, 'status': 'paused',
                'head': head, 'base_commit': parent, 'repo': str(workspace), 'iterations': 1,
                'limits': {'seconds': 600, 'iterations': 2}, 'started_at': self.started}
        cli.write(task, data)
        result = directory / 'result.json'
        review = directory / 'independent.json'
        cli.write(result, {'status': 'verified', 'commit': head})
        cli.write(review, {'status': 'PASS', 'commit': head})
        self.proofs[str(task)] = {'task': str(task), 'commit': head, 'status': 'verified',
                                 'result': {'path': str(result), 'sha256': cli.digest(result)},
                                 'independent_review': {'path': str(review), 'sha256': cli.digest(review)}}
        return task

    def bound(self, task, contract, entry, parent, baseline_perf):
        data = cli.read(task)
        self.assertEqual(contract, self.contract)
        self.assertEqual(data['base_commit'], parent)
        self.assertEqual(data['phase'], entry['id'])
        self.assertEqual(baseline_perf, self.perf)
        return data

    def verified(self, task, data, entry):
        proof = dict(self.proofs[str(task)])
        self.assertEqual(proof['commit'], cli.sha(data['repo'], 'HEAD'))
        for field in ('result', 'independent_review'):
            if cli.digest(proof[field]['path']) != proof[field]['sha256']:
                raise ValueError('retained proof changed')
        if entry['interaction'] and proof['commit'] not in self.approved:
            proof['status'] = 'awaiting-operator-review'
        return proof

    def independent(self, data, candidate, *, destination=None):
        self.assertEqual(data['head'], candidate)
        self.assertEqual(destination, REPO)
        if data['phase'] == 'Notes' and candidate not in self.approved:
            raise ValueError('operator review is required')
        return {}

    def replay(self, original, base, output, *, seconds):
        self.assertEqual(original, self.tasks['Notes'])
        self.assertGreater(seconds, 0)
        path = output / 'replayed' / 'task.json'
        if not path.exists():
            self.replays.append(base)
            merged_tree = cli.git(self.checkout, 'rev-parse', self.second + '^{tree}')
            candidate = cli.git(self.checkout, 'commit-tree', merged_tree, '-p', base, '-m', 'Replay notes')
            self.assertEqual(path, self.task(path.parent, 'Notes', base, candidate))
            data = cli.read(path)
            origin = self.proofs[str(original)]
            data['replay_context'] = {'replayed_candidate': candidate, 'replay': {
                'source_task': str(original), 'source_result': origin['result'],
                'source_review': origin['independent_review'] if not self.substitute_replay else {}}}
            cli.write(path, data)
        else:
            candidate = cli.read(path)['head']
        return {'status': 'verified' if candidate in self.approved else 'awaiting-operator-review',
                'task': str(path), 'candidate': candidate}

    def ship(self, workflows=('CI',)):
        return phase_ship.run(self.manifest, self.digest, self.dispatch, ci_workflows=list(workflows),
                              github=self.fork, replay=self.replay, publisher=self.fork.create, merger=self.fork.merge)

    def test_exact_chain_lands_and_reuses_remote_confirmations(self):
        result = self.ship()
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual([item['head'] for item in result['landed']], [self.first, self.second])
        self.assertEqual(self.fork.branch(REPO, 'main'), self.second)
        self.assertEqual(self.ship()['status'], 'complete')
        self.assertEqual(self.fork.writes, [('publish', 1), ('merge', 1), ('publish', 2), ('merge', 2)])
        self.assertEqual(self.replays, [])
        self.assertEqual(cli.read(self.tasks['Label'])['status'], 'paused')

    def test_squashed_landing_replays_child_and_requires_fresh_approval(self):
        self.fork.squash = True
        result = self.ship()
        self.assertEqual(result['status'], 'awaiting-operator-review', result)
        self.assertEqual(result['phase'], 'Notes')
        squash = self.fork.branch(REPO, 'main')
        self.assertNotEqual(squash, self.first)
        self.assertEqual(self.fork.writes, [('publish', 1), ('merge', 1)])
        replay_proof = next(value for value in self.proofs.values() if value['commit'] not in (self.first, self.second))
        candidate = replay_proof['commit']
        self.approved.add(candidate)
        final = self.ship()
        self.assertEqual(final['status'], 'complete', final)
        self.assertEqual(final['landed'][-1]['head'], candidate)
        self.assertEqual(cli.sha(self.checkout, candidate + '^'), squash)
        self.assertEqual(self.replays, [squash])

    def test_uncertain_publication_and_merge_recover_without_repeating_writes(self):
        self.fork.lose_publication = True
        self.assertEqual(self.ship()['status'], 'awaiting-confirmation')
        self.assertEqual(self.fork.writes, [('publish', 1)])
        self.fork.lose_merge = True
        self.assertEqual(self.ship()['status'], 'awaiting-confirmation')
        self.assertEqual(self.ship()['status'], 'awaiting-confirmation')
        self.assertEqual(self.fork.writes, [('publish', 1), ('merge', 1)])
        self.fork._advance(*self.fork.pending)
        self.assertEqual(self.ship()['status'], 'complete')
        self.assertEqual(self.fork.writes, [('publish', 1), ('merge', 1), ('publish', 2), ('merge', 2)])

    def test_external_main_change_is_not_treated_as_a_replay_base(self):
        self.fork.extra_advance = True
        result = self.ship()
        self.assertEqual(result['status'], 'awaiting-confirmation', result)
        self.assertIn('not the exact', result['reason'])
        self.assertEqual(self.fork.writes, [('publish', 1), ('merge', 1)])
        self.assertEqual(self.replays, [])

    def test_changed_local_proof_prevents_any_remote_write(self):
        Path(self.proofs[str(self.tasks['Label'])]['result']['path']).write_text('{}')
        result = self.ship()
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('retained proof changed', result['reason'])
        self.assertEqual(self.fork.writes, [])

    def test_landing_confirmation_rechecks_replayed_proof(self):
        self.fork.squash = True
        self.assertEqual(self.ship()['status'], 'awaiting-operator-review')
        replay_proof = next(value for value in self.proofs.values() if value['commit'] not in (self.first, self.second))
        self.approved.add(replay_proof['commit'])
        self.assertEqual(self.ship()['status'], 'complete')
        Path(replay_proof['result']['path']).write_text('{}')
        result = self.ship()
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(result['phase'], 'Notes')
        self.assertEqual(len(self.fork.writes), 4)

    def test_reverified_candidate_cannot_substitute_for_the_bound_replay(self):
        self.fork.squash = True
        self.substitute_replay = True
        self.assertEqual(self.ship()['status'], 'awaiting-operator-review')
        replayed = next(value for value in self.proofs.values() if value['commit'] not in (self.first, self.second))
        self.approved.add(replayed['commit'])
        result = self.ship()
        self.assertEqual(result['status'], 'blocked', result)
        self.assertIn('exact replay', result['reason'])
        self.assertEqual(self.fork.writes, [('publish', 1), ('merge', 1)])

    def test_exact_landing_uses_authoritative_tip_and_tree(self):
        member = {'number': 42, 'head': self.first, 'base': self.trunk}
        tree = 'a' * 40
        landing = 'b' * 40
        github = phase_ship.GitHub()
        pr = {'merged': True, 'merge_commit_sha': landing, 'head': {'sha': self.first}, 'base': {'ref': 'main'}}
        head = {'sha': self.first, 'tree': {'sha': tree}}
        commit = {'sha': landing, 'tree': {'sha': tree}, 'parents': [{'sha': self.trunk}, {'sha': self.first}]}
        with patch.object(github, '_call', side_effect=[pr, head, commit]):
            self.assertTrue(github.exact_landing(REPO, member, landing))
        with patch.object(github, '_call', return_value=pr) as api:
            self.assertFalse(github.exact_landing(REPO, member, self.second))
            api.assert_called_once()
        changed = {**commit, 'tree': {'sha': 'f' * 40}}
        with patch.object(github, '_call', side_effect=[pr, head, changed]):
            self.assertFalse(github.exact_landing(REPO, member, landing))
