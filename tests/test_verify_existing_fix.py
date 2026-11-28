from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from workbench import cli, delivery, verify_existing_fix


class ExistingFixTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        path = self.repo / 'app.txt'
        path.write_text('old\n')
        cli.git(self.repo, 'add', 'app.txt')
        cli.git(self.repo, 'commit', '-qm', 'before')
        self.base = cli.sha(self.repo, 'HEAD')
        path.write_text('new\n')
        cli.git(self.repo, 'commit', '-qam', 'after')
        self.head = cli.sha(self.repo, 'HEAD')
        self.report = self.root / 'report.txt'
        self.report.write_text('Original expected behavior.')
        self.url = 'https://github.com/owner/repo/pull/3'
        self.pull = {'number':3, 'html_url':self.url, 'state':'open', 'merged':False,
                     'head':{'sha':self.head}, 'base':{'sha':self.base}}
        self.reader = Mock()
        self.reader.artifact.return_value = self.pull
        self.compare = Mock(side_effect=lambda **_: {'outcome':'artifact-unconfirmed', 'runtime_outcome':'confirmed'})

    def verify(self, **overrides):
        args = dict(repo=self.repo, artifact=self.url, report=self.report, out=self.root/'output', image='local:runtime',
                    manifest='manifest.json', helpers='helpers', journey='feature', oracle='doctor.mjs',
                    oracle_marker='ORACLE_PASS', oracle_failure_marker='ORACLE_FAIL', reader=self.reader,
                    compare=self.compare)
        return verify_existing_fix.verify(**(args | overrides))

    def test_open_pr_is_rechecked_and_the_exact_pair_is_passed_for_comparison(self):
        result = self.verify(expected_head=self.head, expected_base=self.base)
        kwargs = self.compare.call_args.kwargs
        self.assertEqual((kwargs['base'], kwargs['revision']), (self.base, self.head))
        self.assertEqual(kwargs['existing_fix'][1], self.report.read_bytes())
        self.assertEqual(result['outcome'], 'confirmed')
        self.assertEqual(result['artifact_recheck'], kwargs['existing_fix'][0])
        self.assertEqual(self.reader.artifact.call_count, 2)
        self.assertEqual(cli.git(self.repo, 'status', '--porcelain'), '')

    def test_moving_or_unavailable_pr_withholds_runtime_pass(self):
        self.reader.artifact.side_effect = [self.pull, {**self.pull, 'head':{'sha':'a'*40}}]
        self.assertEqual(self.verify()['outcome'], 'stale')
        self.reader.artifact.side_effect = [self.pull, delivery.DeliveryError('provider unavailable')]
        report = self.verify()
        self.assertEqual((report['outcome'], report['runtime_outcome']), ('artifact-unconfirmed', 'confirmed'))

    def test_merged_fix_uses_actual_landed_tree_and_first_parent_for_squash(self):
        self.reader.artifact.return_value = {**self.pull, 'state':'closed', 'merged':True,
                                             'head':{'sha':'b'*40}, 'merge_commit_sha':self.head}
        result = self.verify()
        self.assertEqual(result['artifact_recheck']['state'], 'merged')
        self.assertEqual(self.compare.call_args.kwargs['base'], self.base)
        self.assertEqual(self.compare.call_args.kwargs['revision'], self.head)

    def test_commit_requires_remote_default_branch_ancestry(self):
        url = f'https://github.com/owner/repo/commit/{self.head}'
        self.reader.artifact.return_value = {'status':'diverged', 'merge_base_commit':{'sha':self.base}}
        with self.assertRaisesRegex(ValueError, 'default branch'):
            self.verify(artifact=url)
        self.compare.assert_not_called()
        self.reader.artifact.return_value = {'status':'ahead', 'merge_base_commit':{'sha':self.head}}
        self.assertEqual(self.verify(artifact=url)['outcome'], 'confirmed')

    def test_wrong_pin_closed_pr_and_same_tree_stop_before_execution(self):
        with self.assertRaisesRegex(ValueError, 'exact selected GitHub pair'):
            self.verify(expected_head='c' * 40)
        self.reader.artifact.return_value = {**self.pull, 'state':'closed'}
        with self.assertRaisesRegex(ValueError, 'neither open nor merged'):
            self.verify()
        cli.git(self.repo, 'commit', '--allow-empty', '-qm', 'equivalent')
        self.reader.artifact.return_value = {**self.pull, 'head':{'sha':cli.sha(self.repo, 'HEAD')},
                                             'base':{'sha':self.head}}
        with self.assertRaisesRegex(ValueError, 'changed source tree'):
            self.verify()
        self.compare.assert_not_called()
