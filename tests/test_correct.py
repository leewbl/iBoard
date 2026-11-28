import hashlib
from pathlib import Path
import tempfile
import unittest

from workbench import cli, correct


class HistoryCorrectionTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.repo = self.root / 'repository'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q', '-b', 'main')
        for key, value in (('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid'),
                           ('commit.gpgsign', 'false')):
            cli.git(self.repo, 'config', key, value)
        (self.repo / 'probe.py').write_text('print("private check")\n')
        self.spec = {'schema': 1, 'rule': {'id': 'one-path', 'level': 'architecture',
                     'description': 'Keep a single route.', 'mechanism': 'An authoritative router.', 'higher_levels': {}},
                     'scope': ['routes.txt'], 'cases': [],
                     'driver': {'engine': 'python3', 'path': 'probe.py'},
                     'image': 'sha256:' + 'a' * 64, 'seconds': 30}
        for case in ('left', 'right'):
            bad = self.commit(f'missed-{case}')
            fix = self.commit(f'guarded-{case}')
            self.spec['cases'].append({'id': case, 'bad': bad, 'fix': fix, 'paths': ['routes.txt'],
                                       'observation': f'Accepted an obsolete {case} route.'})
        self.current = self.commit('all-guarded')
        self.calls = []

    def commit(self, body):
        (self.repo / 'routes.txt').write_text(body)
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', body)
        return cli.sha(self.repo, 'HEAD')

    def freeze(self):
        path = self.root / 'manifest.json'
        cli.write(path, self.spec)
        return correct.freeze(path, self.repo, self.current, self.current, self.root / 'plan.json')

    def sandbox(self, folder, image, command, seconds, **options):
        folder = Path(folder).resolve()
        self.assertEqual((folder / correct.PROBE).read_text(), 'print("private check")\n')
        self.assertFalse((folder / '.git').exists())
        self.calls.append(folder)
        case = command[-1]
        missed = (folder / 'routes.txt').read_text() == f'missed-{case}'
        code = 7 if missed else 0
        marker = f'GUARD_{"MISSED" if missed else "HOLDS"} {case}\n'
        return {'image': image, 'workspace': str(folder), 'source_root': '/workspace', 'command': command,
                'memory_mb': options['memory_mb'], 'workspace_read_only': options['workspace_read_only'],
                'container_created': True, 'cleanup_confirmed': True, 'create_exit_code': 0,
                'timed_out': False, 'stdout_truncated': False, 'stderr_truncated': False,
                'create_stdout': hashlib.sha256(str(folder).encode()).hexdigest(), 'exit_code': code,
                'stdout': marker, 'stderr': '', 'stdout_sha256': hashlib.sha256(marker.encode()).hexdigest(),
                'stderr_sha256': hashlib.sha256(b'').hexdigest(), 'runtime_diagnostics': {
                    'state': {'status': 'exited', 'exit_code': code, 'oom_killed': False}}}

    def test_replays_distinct_past_misses_and_fixed_controls_and_rejects_tampering(self):
        approved = self.freeze()
        result = correct.run(**approved, out=self.root / 'proof', sandbox=self.sandbox)
        self.assertEqual(result['status'], 'proved', result)
        self.assertEqual([x['observation']['exit_code'] for x in result['runs']], [7, 0, 0, 7, 0, 0])
        self.assertEqual(len(set(self.calls)), 6)
        self.assertTrue(all(not workspace.exists() for workspace in self.calls))
        proof = correct.audit(**approved, report=result['report'], report_sha256=result['sha256'])
        self.assertEqual(proof['cases'], ['left', 'right'])
        record = Path(result['report']).parent / 'run-2.json'
        record.write_bytes(record.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'digest changed'):
            correct.audit(**approved, report=result['report'], report_sha256=result['sha256'])

    def test_receipt_without_expected_observation_remains_blocked(self):
        def unavailable(*args, **kwargs):
            return {**self.sandbox(*args, **kwargs), 'cleanup_confirmed': False}

        result = correct.run(**self.freeze(), out=self.root / 'proof', sandbox=unavailable)
        self.assertEqual(result['status'], 'blocked')
        self.assertNotIn('observation', result['runs'][0])
        with self.assertRaisesRegex(ValueError, 'complete correction'):
            correct.audit(self.root / 'plan.json', result['plan_sha256'], result['report'], result['sha256'])

    def test_rejects_one_occurrence_and_invalid_repair_chain(self):
        cases = self.spec['cases']
        self.spec['cases'] = cases[:1]
        with self.assertRaisesRegex(ValueError, 'separate events'):
            self.freeze()
        self.spec['cases'] = cases
        cases[0]['fix'] = self.current
        with self.assertRaisesRegex(ValueError, 'repair parent'):
            self.freeze()


if __name__ == '__main__':
    unittest.main()
