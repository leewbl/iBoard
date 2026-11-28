import hashlib
import itertools
import json
from pathlib import Path
import tempfile
import unittest

from workbench import cli, refactor


class RefactorTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        cli.git(self.repo, 'config', 'commit.gpgsign', 'false')
        self.base = self.commit('app.js', 'original')
        self.manifest = self.root / 'manifest.json'
        cli.write(self.manifest, {'schema': 1, 'intent': 'Group conditions', 'target_shape': 'One optional branch',
                                  'scope': ['app.js'], 'cases': [{'id': 'invalid-config',
                                  'helper': 'fixture.mjs', 'args': ['invalid'], 'exit_code': 1,
                                  'behavior': 'Print the public diagnostic'}]})
        self.helpers = self.root / 'helpers'
        self.helpers.mkdir()
        (self.helpers / 'fixture.mjs').write_text('fixture revision one')
        self.path = self.root / 'freeze' / 'contract.json'
        self.image = 'sha256:' + '1' * 64
        self.views = {self.image: b'\x80'}
        self.serials = itertools.count(1)
        self.commands = []
        self.revisions = {}
        self.truncate = False

    def commit(self, path, contents):
        (self.repo / path).write_text(contents)
        cli.git(self.repo, 'add', path)
        cli.git(self.repo, 'commit', '-qm', 'fixture step')
        return cli.sha(self.repo, 'HEAD')

    def sandbox(self, workspace, image, command, timeout, **kwargs):
        self.commands.append((image, command, (Path(workspace) / 'fixture.mjs').read_text()))
        payload = self.views[image]
        if isinstance(payload, list):
            payload = payload.pop(0)
        digest = lambda value: hashlib.sha256(value).hexdigest()
        return {'command': command, 'image': image, **kwargs,
                'create_stdout': f'{next(self.serials):064x}', 'create_exit_code': 0,
                'container_created': True, 'cleanup_confirmed': True, 'timed_out': False,
                'exit_code': 1, 'stdout': payload.decode('utf-8', errors='replace'), 'stderr': '',
                'runtime_diagnostics': {'state': {'status': 'exited', 'exit_code': 1, 'oom_killed': False}},
                'stdout_truncated': self.truncate, 'stderr_truncated': False,
                'stdout_sha256': digest(payload), 'stderr_sha256': digest(b'')}

    def attest(self, repo, image, timeout, *, commit):
        self.assertEqual(str(repo), str(self.repo.resolve()))
        self.assertEqual(image, self.revisions.get(commit, self.image))
        return image

    def derive(self, repo, base, head, image, timeout):
        self.assertEqual(base, self.base)
        self.assertEqual(image, self.image)
        return self.revisions[head]

    def make_candidate(self, payload, path='app.js'):
        commit = self.commit(path, f'structure step {len(self.revisions)}')
        image = self.revisions[commit] = 'sha256:' + f'{len(self.revisions) + 2:064x}'
        self.views[image] = payload
        return commit

    def freeze(self):
        return refactor.freeze(repo=self.repo, base=self.base, image=self.image, manifest=self.manifest,
                               helpers=self.helpers, out=self.path.parent, sandbox=self.sandbox, attest=self.attest)

    def verify(self, head, *, digest=None):
        return refactor.verify(repo=self.repo, contract=self.path, contract_sha256=digest or cli.digest(self.path),
                               candidate=head, out=self.root / 'report', sandbox=self.sandbox,
                               attest=self.attest, derive=self.derive)

    def test_same_frozen_helper_and_exact_streams_across_each_commit(self):
        self.assertEqual(self.freeze()['status'], 'frozen')
        (self.helpers / 'fixture.mjs').write_text('this mutable input must never be used again')
        first = self.make_candidate(b'\x80')
        head = self.make_candidate(b'\x80')
        report = self.verify(head)
        self.assertEqual(report['status'], 'equivalent')
        self.assertEqual([item['commit'] for item in report['steps']], [first, head])
        self.assertEqual(len(self.commands), 6)
        self.assertTrue(all(command == ['node', '/workspace/fixture.mjs', 'invalid']
                            and text == 'fixture revision one' for _, command, text in self.commands))

    def test_transient_behavior_drift_is_rejected_even_when_tip_restores_it(self):
        self.freeze()
        first = self.make_candidate(b'\x81')
        head = self.make_candidate(b'\x80')
        report = self.verify(head)
        self.assertEqual(report['status'], 'behavior-changed')
        self.assertEqual(len(report['steps']), 1)
        self.assertEqual(report['steps'][0]['commit'], first)
        self.assertEqual(report['steps'][0]['differences'], {'invalid-config': ['stdout_sha256']})
        self.assertEqual(len(self.commands), 4)

    def test_unstable_and_truncated_baselines_cannot_be_frozen(self):
        for self.truncate, payload in ((False, [b'\x80', b'\x81']), (True, b'\x80')):
            with self.subTest(truncated=self.truncate):
                self.path = self.root / f'freeze-{self.truncate}' / 'contract.json'
                self.views[self.image] = payload
                result = self.freeze()
                self.assertEqual(result['status'], 'inconclusive')
                self.assertIn('error', result)

    def test_tampered_baseline_receipt_and_contract_digest_are_rejected(self):
        self.freeze()
        head = self.make_candidate(b'\x80')
        with self.assertRaisesRegex(ValueError, 'contract SHA-256'):
            self.verify(head, digest='0' * 64)
        path = self.path.parent / 'invalid-config-2.json'
        receipt = json.loads(path.read_bytes())
        receipt['execution']['stdout'] = 'exchanged after the freeze'
        cli.write(path, receipt)
        with self.assertRaisesRegex(ValueError, 'receipt changed'):
            self.verify(head)
        self.assertEqual(len(self.commands), 2)

    def test_check_refuses_scope_expansion_before_building_any_candidate(self):
        self.freeze()
        head = self.make_candidate(b'\x80', path='documentation.md')
        with self.assertRaisesRegex(ValueError, 'outside its declared scope'):
            self.verify(head)
        self.assertEqual(len(self.commands), 2)


if __name__ == '__main__':
    unittest.main()
