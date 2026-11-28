import csv
import difflib
import json
from pathlib import Path
import re
import tempfile
import unittest

from workbench import cli, hillclimb


class HillclimbTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        self.code = 'latency=100\ncorrect=true\n'
        self.file = self.repo / 'app.txt'
        self.file.write_text(self.code)
        cli.git(self.repo, 'add', 'app.txt')
        cli.git(self.repo, 'commit', '-qm', 'baseline')
        self.base = cli.sha(self.repo, 'HEAD')
        self.file.write_text(self.code.replace('100', '170'))
        cli.git(self.repo, 'commit', '-qam', 'control')
        self.control = cli.sha(self.repo, 'HEAD')
        self.helpers = self.root / 'helpers'
        self.helpers.mkdir()
        for name in ('metric.sh', 'gate.sh'):
            (self.helpers / name).write_text(f'printf "{name}"\n')
        self.benchmark = self.root / 'benchmark.json'
        self.benchmark.write_text(json.dumps({
            'claim': 'Compare one operation.', 'scope': 'end-to-end',
            'configuration': 'Same fixture configuration.', 'production_tuning': None, 'minimum_delta_pct': 5,
            'rounds': 5, 'workload': {'helper': 'metric.sh', 'input': 'Fixture state.', 'completed': 1,
                                       'digest': 'a' * 64, 'max_ops_per_second': 1000,
                                       'plausibility': 'Controlled operation timing.'},
        }))
        best = self.code.replace('100', '55')
        self.make_patch('slow.patch', best, best.replace('55', '90'))
        self.make_patch('wrong.patch', best, best.replace('55', '10').replace('true', 'false'))
        self.spec = {'repo': str(self.repo), 'revision': self.base, 'image': 'fixture:local',
                     'benchmark': str(self.benchmark), 'helpers': str(self.helpers),
                     'gate': {'helper': 'gate.sh', 'marker': 'CORRECT_PASS'},
                     'sensitivity': {'revision': self.control, 'direction': 'slower'},
                     'seconds': 90, 'minimum_attempts': 3, 'target_reduction_pct': 30,
                     'hypotheses': [
                        {'hypothesis': 'Remove extra wait.', 'files': ['app.txt']},
                        {'hypothesis': 'Compare slower control.', 'files': ['app.txt'], 'patch': 'slow.patch'},
                        {'hypothesis': 'Reject broken status.', 'files': ['app.txt'], 'patch': 'wrong.patch'},
                     ]}
        self.images = {}
        self.calls = []
        self.model_calls = 0
        self.baseline_accepts = True
        self.final_accepts = True
        self.final_image = None

    def make_patch(self, name, before, after):
        (self.root / name).write_text(''.join(difflib.unified_diff(
            before.splitlines(True), after.splitlines(True), 'a/app.txt', 'b/app.txt'
        )))

    def edit(self, workspace, prompt, timeout, **opts):
        self.model_calls += 1
        self.assertTrue(opts['file_tools_only'])
        self.assertIn('Remove extra wait.', prompt)
        (workspace / 'app.txt').write_text(self.code.replace('100', '55'))
        return {'exit_code': 0, 'stdout': '{"status":"ready","rationale":"Shorten unnecessary wait."}'}

    def compare(self, *, repo, base, revision, out, **kwargs):
        def image(sha):
            ident = 'sha256:' + sha + sha[:24]
            value = cli.git(repo, 'show', f'{sha}:app.txt')
            self.images[ident] = value
            return ident, int(re.search(r'latency=(\d+)', value)[1])
        a, before = image(base)
        b, after = image(revision)
        self.calls.append((base, revision))
        if out.name.startswith('goal-check'):
            self.final_image = b
        value = {'schema': cli.SCHEMA, 'outcome': 'faster' if after < before else 'slower',
                 'commits': {'baseline': base, 'candidate': revision},
                 'images': {'baseline': a, 'candidate': b},
                 'comparisons': {'workload': {'baseline': {'median_ms': before}, 'candidate': {'median_ms': after},
                                               'delta_pct': 100 * (before - after) / before}}}
        cli.write(out / 'result.json', value)
        return value

    def sandbox(self, folder, image, command, timeout, **options):
        self.assertTrue(options['workspace_read_only'])
        self.assertEqual([path.name for path in Path(folder).iterdir()], ['gate.sh'])
        code = self.images[image]
        passed = (('correct=true' in code) and ('latency=100' not in code or self.baseline_accepts)
                  and (image != self.final_image or self.final_accepts))
        return {'image': image, 'command': command, 'stdout': 'CORRECT_PASS\n' if passed else '',
                'container_created': True, 'cleanup_confirmed': True, 'workspace_read_only': True,
                'exit_code': 0 if passed else 1}

    def invoke(self, name='results'):
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps(self.spec))
        return hillclimb.run(manifest, self.root / name, editor=self.edit,
                             compare=self.compare, sandbox=self.sandbox)

    def test_treats_verified_metric_and_correctness_as_joint_gate_and_honors_floor(self):
        result = self.invoke()
        self.assertEqual(result['status'], 'target-met')
        self.assertEqual(self.model_calls, 1)
        self.assertEqual([row['verdict'] for row in result['attempts']], ['kept', 'reverted', 'reverted'])
        self.assertEqual(result['attempts'][2]['tests'], 'failed')
        self.assertEqual(result['attempts'][2]['after_ms'], 10)
        self.assertEqual(self.calls[-1], (self.base, result['best']))
        self.assertEqual(self.calls.count((self.base, result['best'])), 2)
        self.assertEqual(len(result['kept']), 1)
        self.assertEqual(result['final_comparison']['delta_pct'], 45)
        output = self.root / 'results'
        self.assertEqual(cli.digest(output / 'decision.tsv'), result['decision_sha256'])
        with (output / 'decision.tsv').open() as journal:
            records = list(csv.DictReader(journal, dialect='excel-tab'))
        self.assertEqual(len(records), 3)
        patch = (output / result['export']['path']).read_text()
        self.assertIn('+latency=55', patch)
        self.assertNotIn('+correct=false', patch)
        self.assertEqual(cli.sha(self.repo, 'HEAD'), self.control)

    def test_failing_baseline_blocks_edits(self):
        self.baseline_accepts = False
        result = self.invoke()
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('baseline correctness', result['reason'])
        self.assertEqual(self.model_calls, 0)

    def test_final_correctness_failure_withholds_patch(self):
        self.final_accepts = False
        result = self.invoke()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'final correctness gate failed')
        self.assertFalse((self.root / 'results' / 'accepted.mbox').exists())
