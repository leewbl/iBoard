from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_delivery import FakeGitHub as MergeGitHub
from test_publish import FakeGitHub as PublishGitHub
from workbench import cli, delivery, publish, refactor_delivery


class RefactorPublicationTests(unittest.TestCase):
    repo_name = 'alice/fork'
    actor = 'reviewer'
    base = 'b' * 40
    head = 'c' * 40
    previous = 'd' * 40
    branch = 'workbench/issue-1'

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.task = self.root / 'task.json'
        self.task.touch()
        self.contract = self.record('contract', {'scope': ['example']})
        cli.write(self.root / 'input-stage.json', {'schema': cli.SCHEMA, 'refactor_contract': self.contract})
        self.result = self.record('result', {'steps': [{'commit': self.head}]})
        self.data = {'id': 'issue-1', 'kind': 'refactor', 'repo': str(self.repo), 'surface': 'non-ui',
                     'base_commit': self.base, 'implementer': 'builder', 'required_ci_workflows': ['CI'],
                     'criteria': [{'id': cli.REFACTOR_CRITERION, 'text': cli.REFACTOR_CRITERION_TEXT, 'revision': 1}],
                     'refactor_contract': self.contract, 'refactor_result': self.result, 'evidence': []}
        test = self.record('tests', {'kind': 'test-result', 'actor': self.actor, 'commit': self.head,
                           'source_mode': 'image', 'workspace': str(self.repo), 'source_verified_commit': self.head,
                           'verified_image_id': 'sha256:' + 'a' * 64, 'image': 'sha256:' + 'a' * 64,
                           'container_created': True, 'cleanup_confirmed': True, 'timed_out': False,
                           'exit_code': 0, 'expected_exit': 0, 'stdout': '1 passed', 'stderr': ''})
        self.review = {'verdict': 'PASS', 'candidate_commit': self.head, 'baseline_commit': self.base,
                       'criteria': [cli.REFACTOR_CRITERION], 'criterion_digest': cli.criteria_digest(self.data),
                       'refactor_report_sha256': self.result['sha256'], 'reports': {'REFAC': self.result, 'R1': test},
                       'assessments': [{'id': cli.REFACTOR_CRITERION, 'verdict': 'PASS', 'evidence': ['REFAC'],
                                        'reason': 'Independent replay preserved behavior'}]}
        self.data['evidence'] = [self.evidence('test-result', test), self.evidence('runtime-log', self.result),
                                 self.review_item('review', self.review)]
        self.current = deepcopy(self.data)
        read = cli.read
        for mock in (patch.object(cli, 'read', side_effect=lambda path: deepcopy(self.current)
                                  if Path(path) == self.task else read(path)),
                     patch.object(cli, 'gate', return_value=(self.head, [])),
                     patch.object(refactor_delivery, 'require', side_effect=self.validate_receipt)):
            mock.start()
            self.addCleanup(mock.stop)

    def record(self, name, value):
        path = self.root / (name + '.json')
        cli.write(path, value)
        return {'path': str(path), 'sha256': cli.digest(path)}

    def evidence(self, kind, reference):
        return {'kind': kind, 'actor': self.actor, 'commit': self.head, **reference}

    def review_item(self, name, review):
        return {**self.evidence('independent-review', self.record(name, review)),
                'criteria_digest': cli.criteria_digest(self.data)}

    def validate_receipt(self, data, head):
        # The proof engine's replay of raw steps is tested in test_refactor_delivery.
        # Here the consumer must re-invoke that engine before any remote mutation.
        if head != self.head or data.get('refactor_contract') != self.contract or data.get('refactor_result') != self.result:
            raise refactor_delivery.RefactorGateError('missing or mismatched refactoring evidence')
        if cli.digest(self.result['path']) != self.result['sha256']:
            raise refactor_delivery.RefactorGateError('refactoring result changed')
        return self.result

    def fork(self, remote=None, body=''):
        github = PublishGitHub()
        github.base = self.base
        github.remote = remote
        if remote:
            github.existing = [{'number': 7, 'headRefName': self.branch, 'headRefOid': remote,
                                'baseRefName': 'main', 'baseRefOid': self.base, 'isCrossRepository': False,
                                'headRepository': {'name': 'fork'}, 'headRepositoryOwner': {'login': 'alice'},
                                'body': body}]
        return github

    def replace_review(self):
        review = deepcopy(self.review)
        review['assessments'][0]['reason'] = 'New independent review of the same artifacts'
        self.current['evidence'][-1] = self.review_item('new-review', review)

    def test_publishes_refactor_with_step_count_and_report_hash(self):
        github = self.fork()
        publish.publish(self.task, self.repo_name, github)
        self.assertEqual(github.events, [('push', self.branch, self.head), ('create', self.branch)])
        body = github.existing[0]['body']
        self.assertIn('## Refactoring equivalence', body)
        self.assertIn('Verified 1 ordered commit', body)
        self.assertIn(self.result['sha256'], body)
        self.assertNotIn('## Reproduction', body)
        self.assertNotIn('## Functional documentation', body)

    def test_republish_and_merge_pass_with_unchanged_refactor_proof(self):
        fork = self.fork(self.previous)
        publish.republish(self.task, self.repo_name, 7, self.previous, fork)
        self.assertEqual(fork.events, [('update', self.branch, self.previous, self.head), ('body', 7)])
        github = MergeGitHub(self.base, self.head)
        result = delivery.guarded_merge(self.task, self.repo_name, 1, github)
        self.assertTrue(result['merged'])
        self.assertTrue(github.merged)

    def test_missing_or_mismatched_refactor_review_blocks_initial_publish(self):
        for failure in ('outer-hash', 'report-ref', 'citation', 'evidence', 'superseded'):
            with self.subTest(failure=failure):
                github = self.fork()
                self.current = deepcopy(self.data)
                review = deepcopy(self.review)
                if failure == 'outer-hash':
                    review['refactor_report_sha256'] = '0' * 64
                elif failure == 'report-ref':
                    review['reports']['REFAC'] = review['reports']['R1']
                elif failure == 'citation':
                    review['assessments'][0]['evidence'] = ['R1']
                elif failure == 'evidence':
                    self.current['evidence'].pop(1)
                else:
                    self.current['evidence'][1]['superseded'] = True
                self.current['evidence'][-1] = self.review_item('rejected-review', review)
                with self.assertRaisesRegex(delivery.DeliveryError, 'independent PASS'):
                    publish.publish(self.task, self.repo_name, github)
                self.assertEqual(github.events, [])

    def test_removed_contract_cannot_pass_final_branch_push_preflight(self):
        github = self.fork()

        def check_remote(*_):
            self.current.pop('refactor_contract')
            return None

        github.remote_head = check_remote
        with self.assertRaisesRegex(publish.PublishError, 'contract or result changed'):
            publish.publish(self.task, self.repo_name, github)
        self.assertEqual(github.events, [])

    def test_replaced_review_cannot_pass_pr_creation_preflight(self):
        github = self.fork()
        push = github.push_new

        def push_then_change_review(*args):
            push(*args)
            self.replace_review()

        github.push_new = push_then_change_review
        with self.assertRaisesRegex(publish.PublishError, 'independent review changed'):
            publish.publish(self.task, self.repo_name, github)
        self.assertEqual(github.events, [('push', self.branch, self.head)])

    def test_republish_rejects_tampered_result_before_updating_branch(self):
        github = self.fork(self.previous)
        pulls = github.pulls

        def inspect_then_change_report(*args):
            result = pulls(*args)
            Path(self.result['path']).write_text('{}')
            return result

        github.pulls = inspect_then_change_report
        with self.assertRaisesRegex(publish.PublishError, 'refactoring result changed'):
            publish.republish(self.task, self.repo_name, 7, self.previous, github)
        self.assertEqual(github.events, [])

    def test_recorded_intake_blocks_removing_refactor_kind_and_evidence(self):
        self.current['kind'] = 'feature'
        self.current.pop('refactor_contract')
        self.current.pop('refactor_result')
        github = self.fork()
        with self.assertRaisesRegex(delivery.DeliveryError, 'frozen refactoring intake'):
            publish.publish(self.task, self.repo_name, github)
        self.assertEqual(github.events, [])

    def test_deleted_refactor_intake_blocks_before_github(self):
        (self.root / 'input-stage.json').unlink()
        github = self.fork()
        with self.assertRaisesRegex(delivery.DeliveryError, 'intake is missing'):
            publish.publish(self.task, self.repo_name, github)
        self.assertEqual(github.events, [])

    def test_republish_rejects_new_review_before_description_update(self):
        github = self.fork(self.head)
        pulls = github.pulls
        reads = 0

        def inspect_then_replace_review(*args):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.replace_review()
            return pulls(*args)

        github.pulls = inspect_then_replace_review
        with self.assertRaisesRegex(publish.PublishError, 'independent review changed'):
            publish.republish(self.task, self.repo_name, 7, self.previous, github)
        self.assertEqual(github.events, [])

    def test_merge_rechecks_contract_for_protected_and_unprotected_main(self):
        for protected in (True, False):
            with self.subTest(protected=protected):
                self.current = deepcopy(self.data)
                github = MergeGitHub(self.base, self.head)
                github.protected = protected
                pull = github.pull

                def inspect_then_clear_result(*args):
                    self.current.pop('refactor_result', None)
                    return pull(*args)

                github.pull = inspect_then_clear_result
                with self.assertRaisesRegex(delivery.DeliveryError, 'contract or result changed'):
                    delivery.guarded_merge(self.task, self.repo_name, 1, github)
                self.assertFalse(github.merged)
                self.assertFalse(github.pushed)


if __name__ == '__main__':
    unittest.main()
