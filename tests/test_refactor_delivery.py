import itertools
import json
from pathlib import Path
import tempfile
import unittest

from workbench import cli, refactor, refactor_delivery


class RefactorDeliveryTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        cli.git(self.repo, 'config', 'commit.gpgsign', 'false')
        self.base = self.commit('original')
        self.serials = itertools.count(1)
        self.baseline_image = 'sha256:' + '1' * 64
        self.manifest = self.root / 'manifest.json'
        cli.write(self.manifest, {
            'schema': 1, 'intent': 'Group conditions', 'target_shape': 'One optional branch',
            'scope': ['app.js'], 'cases': [{
                'id': 'public-output', 'helper': 'observe.mjs', 'args': [], 'exit_code': 0,
                'behavior': 'Print the public line',
            }],
        })
        drivers = self.root / 'drivers'
        drivers.mkdir()
        (drivers / 'observe.mjs').write_text('console.log("public line")')
        self.frozen = self.root / 'baseline'
        plan = refactor.freeze(repo=self.repo, base=self.base, image=self.baseline_image,
                               manifest=self.manifest, helpers=drivers, out=self.frozen,
                               sandbox=self.run_container, attest=self.attest)
        self.assertEqual(plan['status'], 'frozen')
        self.contract_path = self.frozen / 'contract.json'
        self.first = self.commit('structure part one')
        self.head = self.commit('structure part two')
        self.data = {'repo': str(self.repo), 'base_commit': self.base,
                     'refactor_contract': refactor_delivery.bind(self.contract_path, cli.digest(self.contract_path))}
        self.out = self.root / 'result'
        report = refactor.verify(repo=self.repo, contract=self.contract_path,
                                 contract_sha256=self.data['refactor_contract']['sha256'],
                                 candidate=self.head, out=self.out, sandbox=self.run_container,
                                 attest=self.attest, derive=self.candidate_image)
        self.assertEqual(report['status'], 'equivalent')
        self.report_path = self.out / 'report.json'
        self.data['refactor_result'] = {'path': str(self.report_path), 'sha256': cli.digest(self.report_path)}

    def commit(self, value):
        (self.repo / 'app.js').write_text(value)
        cli.git(self.repo, 'add', 'app.js')
        cli.git(self.repo, 'commit', '-qm', 'fixture step')
        return cli.sha(self.repo, 'HEAD')

    @staticmethod
    def attest(repo, image, timeout, *, commit):
        return image

    @staticmethod
    def candidate_image(repo, base, revision, image, timeout):
        return 'sha256:' + revision + '0' * 24

    def run_container(self, workspace, image, command, timeout, **settings):
        return {'command': command, 'image': image, **settings,
                'create_stdout': f'{next(self.serials):064x}', 'create_exit_code': 0,
                'container_created': True, 'cleanup_confirmed': True, 'timed_out': False,
                'exit_code': 0, 'stdout_sha256': refactor._digest(b'public line\n'),
                'stderr_sha256': refactor._digest(b''),
                'stdout_truncated': False, 'stderr_truncated': False}

    def save_report(self, report):
        cli.write(self.report_path, report)
        self.data['refactor_result']['sha256'] = cli.digest(self.report_path)

    def test_revalidates_every_step_for_the_exact_current_head(self):
        binding = refactor_delivery.bind(self.contract_path, cli.digest(self.contract_path))
        self.assertEqual(binding, self.data['refactor_contract'])
        self.assertEqual(refactor_delivery.contract(self.data)['baseline'], self.base)
        self.assertEqual(refactor_delivery.require(self.data, self.head), self.data['refactor_result'])
        latest = self.commit('another commit')
        with self.assertRaisesRegex(refactor_delivery.RefactorGateError, 'current Git HEAD'):
            refactor_delivery.require(self.data, self.head)
        with self.assertRaisesRegex(refactor_delivery.RefactorGateError, 'required candidate'):
            refactor_delivery.require(self.data, latest)
        self.data['base_commit'] = self.first
        with self.assertRaisesRegex(refactor_delivery.RefactorGateError, 'task base'):
            refactor_delivery.contract(self.data)

    def test_rejects_missing_or_edited_raw_receipt_at_delivery(self):
        raw = self.out / 'step-1' / 'public-output-1.json'
        original = raw.read_bytes()
        raw.unlink()
        with self.assertRaisesRegex(refactor_delivery.RefactorGateError, 'missing'):
            refactor_delivery.require(self.data, self.head)
        raw.write_bytes(original + b'\n')
        with self.assertRaisesRegex(refactor_delivery.RefactorGateError, 'receipt changed'):
            refactor_delivery.require(self.data, self.head)

    def test_false_equivalent_is_rejected_even_with_consistent_updated_hashes(self):
        report = json.loads(self.report_path.read_bytes())
        step = report['steps'][0]
        changed = refactor._digest(b'changed output\n')
        for receipt in step['runs']['public-output']:
            raw = self.out / 'step-1' / receipt['report']
            saved = json.loads(raw.read_bytes())
            saved['execution']['stdout_sha256'] = changed
            cli.write(raw, saved)
            receipt['sha256'] = cli.digest(raw)
        step['observed']['public-output']['stdout_sha256'] = changed
        self.save_report(report)
        with self.assertRaisesRegex(refactor_delivery.RefactorGateError, 'behavior differs'):
            refactor_delivery.require(self.data, self.head)

    def test_cannot_reuse_container_id_from_the_frozen_baseline(self):
        baseline = json.loads((self.frozen / 'public-output-1.json').read_bytes())
        report = json.loads(self.report_path.read_bytes())
        receipt = report['steps'][-1]['runs']['public-output'][0]
        raw = self.out / 'step-2' / receipt['report']
        saved = json.loads(raw.read_bytes())
        saved['execution']['create_stdout'] = baseline['execution']['create_stdout']
        cli.write(raw, saved)
        receipt['sha256'] = cli.digest(raw)
        self.save_report(report)
        with self.assertRaisesRegex(refactor_delivery.RefactorGateError, 'fresh'):
            refactor_delivery.require(self.data, self.head)

    def test_requires_exact_ordered_commits_and_their_changed_paths(self):
        payload = self.report_path.read_bytes()
        for steps in ([], 'invalid', None):
            with self.subTest(steps=steps):
                report = json.loads(payload)
                report['steps'] = steps
                self.save_report(report)
                with self.assertRaisesRegex(refactor_delivery.RefactorGateError, 'every ordered commit'):
                    refactor_delivery.require(self.data, self.head)
        report = json.loads(payload)
        report['steps'][0]['paths'] = ['other.js']
        self.save_report(report)
        with self.assertRaisesRegex(refactor_delivery.RefactorGateError, 'invalid step'):
            refactor_delivery.require(self.data, self.head)

    def test_rejects_a_frozen_contract_with_missing_scope_or_changed_receipts(self):
        payload = self.contract_path.read_bytes()
        plan = json.loads(payload)
        plan['spec']['scope'] = []
        cli.write(self.contract_path, plan)
        with self.assertRaisesRegex(refactor_delivery.RefactorGateError, 'one to forty'):
            refactor_delivery.bind(self.contract_path, cli.digest(self.contract_path))
        self.contract_path.write_bytes(payload)
        raw = self.frozen / 'public-output-2.json'
        raw.write_bytes(raw.read_bytes() + b'\n')
        with self.assertRaisesRegex(refactor_delivery.RefactorGateError, 'receipt changed'):
            refactor_delivery.contract(self.data)


if __name__ == '__main__':
    unittest.main()
