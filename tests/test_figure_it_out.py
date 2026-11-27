import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from workbench import cli, figure_it_out as figure


class FigureItOutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        for key, value in (('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid'), ('commit.gpgsign', 'false')):
            self.git('config', key, value)
        (self.repo/'check.mjs').write_text('// frozen observations\n')
        (self.repo/'b.txt').write_text('before')
        self.base = self.commit('a.txt', 'before')
        self.git('checkout', '-q', '-b', 'trials')
        bad = self.commit('a.txt', 'tentative')
        self.git('checkout', '-q', '-b', 'accepted-chain', self.base)
        first = self.commit('a.txt', 'accepted')
        second = self.commit('b.txt', 'accepted')
        self.git('checkout', '-q', 'main')
        self.spec = {
            'schema': 1, 'intent': 'Migrate two parts.', 'done': 'Both parts and shared route are verified.',
            'frame': {'rigor': 'reversible', 'reason': 'Modular fixture', 'estimated_minutes': 5, 'blockers': []},
            'image': 'sha256:' + 'a'*64, 'seconds': 75, 'memory_mb': 512,
            'model': 'glm-5.3', 'review_model': 'gpt-6.1-sol', 'design': None,
            'checks': {name: {'command': ['node', '/workspace/check.mjs', name],
                              'expectation': f'Observe the {name} behavior on the product.',
                              'pass': name.upper()+'_IS_PASS', 'fail': name.upper()+'_IS_FAIL'}
                       for name in ('left', 'right', 'keep', 'done')},
            'guards': ['keep'], 'final': 'done',
            'units': [
                {'id': 'first', 'unknown': 'Left prerequisite', 'check': 'left', 'scope': ['a.txt'],
                 'hypotheses': [{'commit': bad, 'description': 'Try a partial change.'},
                                {'commit': first, 'description': 'Finish the complete left behavior.'}]},
                {'id': 'second', 'unknown': 'Right consumer', 'check': 'right', 'scope': ['b.txt'],
                 'hypotheses': [{'commit': second, 'description': 'Adopt the right-hand change.'}]},
            ],
        }
        self.calls = []
        self.judge_packets = []

    def git(self, *args):
        return cli.git(self.repo, *args)

    def commit(self, name, value):
        (self.repo/name).write_text(value)
        self.git('add', '.')
        self.git('commit', '-qm', f'{name}: {value}')
        return cli.sha(self.repo, 'HEAD')

    def freeze(self):
        manifest = self.root / 'manifest.json'
        cli.write(manifest, self.spec)
        return figure.freeze(manifest, self.repo, self.base, self.root / 'plan.json')

    def sandbox(self, path, image, command, timeout, **opts):
        self.calls.append(path)
        left = (path/'a.txt').read_text() == 'accepted'
        right = (path/'b.txt').read_text() == 'accepted'
        name = command[-1]
        passed = {'left': left, 'right': right, 'keep': True, 'done': left and right}[name]
        code = 0 if passed else 7
        raw = self.spec['checks'][name]['pass' if passed else 'fail']+'\n'
        return {'workspace': str(path), 'workspace_read_only': opts['workspace_read_only'],
                'source_root': '/workspace', 'image': image, 'memory_mb': opts['memory_mb'], 'command': command,
                'container_created': True, 'cleanup_confirmed': True, 'create_exit_code': 0,
                'timed_out': False, 'stdout_truncated': False, 'stderr_truncated': False, 'stdout': raw, 'stderr': '',
                'create_stdout': hashlib.sha256(str(len(self.calls)).encode()).hexdigest(), 'exit_code': code,
                'stdout_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                'stderr_sha256': hashlib.sha256(b'').hexdigest(), 'runtime_diagnostics': {
                    'state': {'status': 'exited', 'exit_code': code, 'oom_killed': False}}}

    def judge(self, prompt, model, timeout):
        packet = json.loads(prompt.removeprefix(figure.REVIEW))
        self.judge_packets.append(packet)
        self.assertTrue(all(status == 'pass' for status in packet['observations'].values()))
        self.assertIn('diff --git', packet['patch'])
        return {'verdict': 'VERIFIED', 'reason': 'The complete scoped behavior was observed.',
                'observations': list(packet['observations'])}

    def test_rejects_first_hypothesis_and_audits_the_retained_chain(self):
        approval = self.freeze()
        result = figure.run(**approval, out=self.root/'out', sandbox=self.sandbox, judge=self.judge)
        self.assertEqual(result['status'], 'VERIFIED', result)
        self.assertEqual([row['status'] for row in result['units'][0]['attempts']], ['NOT VERIFIED', 'VERIFIED'])
        self.assertIn('left', self.judge_packets[1]['observations'])
        self.assertEqual(cli.sha(self.repo, 'main'), self.base)
        self.assertTrue(all(not path.exists() for path in self.calls))
        audited = figure.audit(**approval, report=result['report'], report_sha256=result['sha256'])
        self.assertEqual(audited['verified_units'], ['first', 'second'])
        self.assertEqual(audited['sandbox_runs'], 30)
        patch = Path(result['report']).parent/'verified.mbox'
        self.assertNotIn(b'tentative', patch.read_bytes())
        patch.write_bytes(patch.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            figure.audit(**approval, report=result['report'], report_sha256=result['sha256'])

    def test_final_failure_keeps_evidence_and_withholds_export(self):
        def sandbox(path, image, command, timeout, **opts):
            report = self.sandbox(path, image, command, timeout, **opts)
            if command[-1] == 'done' and 'whole-product' in str(path):
                marker = self.spec['checks']['done']['fail']+'\n'
                report.update(exit_code=7, stdout=marker, stdout_sha256=hashlib.sha256(marker.encode()).hexdigest(),
                              runtime_diagnostics={'state': {'status': 'exited', 'exit_code': 7, 'oom_killed': False}})
            return report

        result = figure.run(**self.freeze(), out=self.root/'out', sandbox=sandbox, judge=self.judge)
        self.assertEqual(result['status'], 'NOT VERIFIED')
        self.assertNotIn('export', result)
        self.assertEqual(result['final']['done']['outcome'], 'fail')

    def test_unstable_evidence_stops_without_model_or_export(self):
        def sandbox(path, image, command, timeout, **opts):
            report = self.sandbox(path, image, command, timeout, **opts)
            if len(self.calls) == 2:
                marker = self.spec['checks']['done']['pass']+'\n'
                report.update(exit_code=0, stdout=marker, stdout_sha256=hashlib.sha256(marker.encode()).hexdigest())
            return report

        result = figure.run(**self.freeze(), out=self.root/'out', sandbox=sandbox, judge=self.judge)
        self.assertEqual(result['status'], 'INCONCLUSIVE')
        self.assertIn('repetitions disagree', result['reason'])
        self.assertEqual(self.judge_packets, [])
        self.assertNotIn('export', result)

    def test_high_impact_requires_a_selected_design(self):
        self.spec['frame']['rigor'] = 'high-impact'
        with self.assertRaisesRegex(ValueError, 'design decision'):
            self.freeze()
        self.spec['frame']['rigor'] = 'reversible'
        self.spec['units'][0]['scope'].append('check.mjs')
        with self.assertRaisesRegex(ValueError, 'outside the edit scope'):
            self.freeze()


if __name__ == '__main__':
    unittest.main()
