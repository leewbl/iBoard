import json
from pathlib import Path
import tempfile
import unittest

from workbench import cli, pickup_history, recall, workflow


class Forge:
    def __init__(self, head, base, *, branch='topic', state='OPEN'):
        self.head, self.base, self.branch_name, self.state = head, base, branch, state
        self.reads = []

    def pull(self, repo, number):
        self.reads.append(('pr', repo, number))
        return {'number': number, 'headRefOid': self.head, 'baseRefOid': self.base,
                'state': self.state, 'headRefName': self.branch_name, 'isDraft': self.state == 'OPEN'}

    def branch(self, repo, name):
        self.reads.append(('branch', repo, name))
        return self.head


class SessionPickupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q', '-b', 'main')
        self.base = self.commit('base.txt', 'Define the original behavior')
        cli.git(self.repo, 'checkout', '-qb', 'topic')
        self.head = self.commit('candidate.txt', 'Keep the verified behavior')
        self.github = Forge(self.head, self.base)

    def commit(self, file, subject):
        (self.repo / file).write_text(subject, encoding='utf-8')
        cli.git(self.repo, 'add', file)
        cli.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', subject)
        return cli.sha(self.repo, 'HEAD')

    def test_local_branch_diff_excludes_new_commits_on_the_base(self):
        cli.git(self.repo, 'checkout', '-q', 'main')
        current = self.commit('base-new.txt', 'Unrelated new main change')
        before = cli.git(self.repo, 'show-ref')
        result = pickup_history.inspect(self.repo, branch='topic', base='main', goal='Keep behavior stable')
        self.assertEqual(result['goal'], 'Keep behavior stable')
        self.assertEqual(result['delta']['base_only'], 1)
        self.assertEqual(result['delta']['merge_base'], self.base)
        self.assertEqual(result['delta']['changed_paths'], ['A\tcandidate.txt'])
        self.assertEqual(result['delta']['commits'][0]['sha'], self.head)
        self.assertEqual(result['playbook'], 'interrogate')
        remote = pickup_history.inspect(self.repo, forge='alice/project', pr=7, github=Forge(self.head, current))
        self.assertEqual(remote['playbook'], 'shipping')
        self.assertEqual(remote['next'], 'reconcile-the-candidate-with-the-current-pr-base')
        self.assertEqual(result['checks_rerun'], [])
        self.assertEqual(before, cli.git(self.repo, 'show-ref'))

    def test_prior_recall_and_live_pr_are_preserved_as_distinct_evidence(self):
        transcript = self.root / 'chat.jsonl'
        transcript.write_text(json.dumps({'cwd': str(self.repo), 'type': 'user',
            'message': {'role': 'user', 'content': 'Keep the runtime receipts for PR #7.'},
            'sessionId': 'fixture', 'timestamp': '2026-11-26T12:00:00Z'}) + '\n', encoding='utf-8')
        packet = recall.collect(self.repo, self.root / 'evidence.json', [transcript], forge='alice/project',
                                until='2026-11-27T00:00:00Z')
        past = Forge(self.head, self.base, state='CLOSED')
        report = recall.summarize(packet['path'], packet['sha256'], self.root / 'recall.json',
                                  model='gpt-test', github=past, reviewer=lambda *_: {
                                      'capsule': [], 'threads': [], 'problems': [],
                                      'next': {'text': 'Preserve the prior receipts.', 'citations': ['T1']}})
        out = self.root / 'pickup.json'
        result = pickup_history.inspect(self.repo, github=self.github,
                                        recall_report=report['path'], recall_sha256=report['sha256'], out=out)
        self.assertEqual(result['verdict'], 'continue')
        self.assertEqual(result['playbook'], 'verify-pr')
        self.assertEqual(result['worktree']['head'], self.head)
        self.assertEqual(result['history']['prior_artifacts']['L1']['tag'], 'closed PR #7')
        self.assertEqual(result['pr']['state'], 'OPEN')
        self.assertEqual(result['history']['citations']['T1']['session'], 'fixture')
        self.assertTrue(result['history']['selected_artifact'])
        self.assertEqual(result['receipt']['sha256'], cli.digest(out))
        self.assertEqual(len(self.github.reads), 2)
        with self.assertRaisesRegex(ValueError, 'another GitHub repository'):
            pickup_history.inspect(self.repo, forge='elsewhere/project', pr=7, github=self.github,
                                   recall_report=report['path'], recall_sha256=report['sha256'])
        Path(packet['path']).write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'evidence digest changed'):
            pickup_history.inspect(self.repo, forge='alice/project', pr=7, github=self.github,
                                   recall_report=report['path'], recall_sha256=report['sha256'])

    def test_routes_dirty_closed_merged_and_outdated_base(self):
        (self.repo / 'uncommitted.txt').write_text('pending', encoding='utf-8')
        dirty = pickup_history.inspect(self.repo, branch='topic', base='main')
        self.assertEqual(dirty['verdict'], 'inspect')
        self.assertTrue(dirty['worktree']['dirty'])
        (self.repo / 'uncommitted.txt').unlink()
        for state, verdict, playbook in (('MERGED', 'ratify', 'interrogate'), ('CLOSED', 'postmortem', 'investigation')):
            with self.subTest(state=state):
                forge = Forge(self.head, self.base, state=state)
                result = pickup_history.inspect(self.repo, forge='alice/project', pr=7, github=forge)
                self.assertEqual((result['verdict'], result['playbook']), (verdict, playbook))
        forge = Forge(self.head, self.head)
        result = pickup_history.inspect(self.repo, forge='alice/project', pr=7, github=forge, base=self.base)
        self.assertEqual(result['next'], 'reconcile-the-retained-base-with-the-current-pr-base')
        self.assertTrue(result['pr_base_changed'])

    def test_rejects_a_remote_branch_update_during_inspection(self):
        class MovingForge(Forge):
            def branch(self, repo, name):
                previous = super().branch(repo, name)
                return previous if len(self.reads) == 1 else self.base

        github = MovingForge(self.head, self.base)
        with self.assertRaisesRegex(ValueError, 'selected state changed'):
            pickup_history.inspect(self.repo, forge='alice/project', remote_branch='topic', base='main', github=github)
        self.assertEqual(cli.sha(self.repo, 'HEAD'), self.head)

    def test_retained_task_still_owns_an_untouched_checkpoint(self):
        prepared = workflow.prepare(task_id='pickup-history', repo=self.repo, tasks_root=self.root / 'tasks',
                                    workspaces_root=self.root / 'workspaces', source='Keep the original feature',
                                    kind='feature', surface='non-ui', iterations=2, seconds=600,
                                    implementer='engineer')
        task = prepared['task']
        data = cli.read(task)
        data['status'] = 'paused'
        cli.write(Path(task), data)
        task_dir = Path(task).parent
        for file in ('issue.txt', 'input-base.txt'):
            (task_dir / file).write_text('Keep the original feature', encoding='utf-8')
        digest = cli.digest(task_dir / 'issue.txt')
        cli.write(task_dir / 'input-stage.json', {
            'schema': cli.SCHEMA, 'phase': 'ready', 'input_digest': None,
            'base_digest': digest, 'request_digest': digest,
            'run_settings': {'image': 'test', 'pr_repo': None, 'source_mode': 'workspace',
                             'candidate_image': None, 'build_source_image': False, 'memory_mb': 2048}})
        workspace = Path(prepared['workspace'])
        branch = cli.git(workspace, 'branch', '--show-current')
        before = Path(task).read_bytes()
        result = pickup_history.inspect(self.repo, branch=branch, task=task)
        self.assertEqual(result['verdict'], 'continue', result['task'])
        self.assertEqual(result['playbook'], 'feature')
        self.assertEqual(result['next'], 'resume-from-plan')
        self.assertEqual(result['goal'], 'Keep the original feature')
        self.assertEqual(result['task']['path'], task)
        self.assertEqual(result['checks_rerun'], [])
        self.assertEqual(before, Path(task).read_bytes())


if __name__ == '__main__':
    unittest.main()
