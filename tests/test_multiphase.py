from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from workbench import cli, multiphase, phase_audit


PROBE = 'One controlled work unit.'
DIGEST = 'a' * 64
BASE_IMAGE = 'sha256:' + 'b' * 64
HEAD_IMAGE = 'sha256:' + 'c' * 64


class MultiPhaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        app = self.repo / 'app.py'
        app.write_text('def run():\n    return 6\n')
        cli.git(self.repo, 'add', 'app.py')
        cli.git(self.repo, 'commit', '-qm', 'baseline')
        self.base = cli.sha(self.repo, 'HEAD')
        app.write_text('def run():\n    return 3 + 3\n')
        cli.git(self.repo, 'commit', '-qam', 'candidate')
        self.head = cli.sha(self.repo, 'HEAD')
        self.spec = {'repo': str(self.repo), 'revision': self.base, 'request': 'Plan a split workflow.',
                     'files': ['app.py:1-2'], 'scope': ['app.py', 'notes.md'], 'obvious': False,
                     'open_questions': [], 'prototypes': [],
                     'plan': {'summary': 'Stage the caller and its dependent documentation.',
                              'phases': [self.phase('P1', 'trunk', ['app.py'], True),
                                         self.phase('P2', 'P1', ['notes.md'], False)]}}

    def phase(self, ident, branch, files, interaction):
        return {'id': ident, 'base': branch, 'depends_on': [] if branch == 'trunk' else [branch],
                'files': files, 'change': 'Keep one scoped change.',
                'observed_result': 'The public output remains six.', 'interaction': interaction,
                'citations': [{'id': 'S1', 'line': 2}],
                'verify': {'unit': {'scenario': 'Check the output.', 'predicate': 'Value is six.',
                                    'marker': 'UNIT_PASS'},
                           'live': {'scenario': 'Call the entrypoint.', 'predicate': 'CLI prints six.',
                                    'marker': 'LIVE_PASS'},
                           'perf': {'probe': PROBE, 'metric': 'median_ms', 'max_ms': 250}}}

    def plan(self, folder='plan'):
        path = self.root / 'manifest.json'
        path.write_text(json.dumps(self.spec))
        return multiphase.plan(path, self.root / folder)

    def ref(self, path):
        return {'path': str(path.relative_to(self.root)), 'sha256': cli.digest(path)}

    def perf(self, folder, head, base_time, elapsed):
        directory = self.root / folder
        frozen = directory / 'frozen'
        frozen.mkdir(parents=True)
        helper = frozen / 'work.sh'
        helper.write_text('printf work\n')
        sha = cli.digest(helper)
        contract = {'schema': cli.SCHEMA, 'claim': PROBE, 'rounds': 5,
                    'commits': {'baseline': self.base, 'candidate': head},
                    'helpers': {'work.sh': sha},
                    'workload': {'helper': 'work.sh', 'completed': 1, 'digest': DIGEST,
                                 'max_ops_per_second': 1000}}
        images = {'baseline': BASE_IMAGE, 'candidate': BASE_IMAGE if head == self.base else HEAD_IMAGE}
        cli.write(frozen / 'contract.json', contract)
        rows = []
        for index in range(1, 6):
            for side in ('baseline', 'candidate'):
                measured = {'kind': 'iboard-benchmark', 'completed': 1, 'errors': 0, 'unit': 'ms',
                            'elapsed_ms': 100 if side == 'baseline' else elapsed, 'digest': DIGEST}
                time = base_time + timedelta(seconds=len(rows))
                path = directory / f'{index}-{side}.json'
                cli.write(path, {'schema': cli.SCHEMA, 'image': images[side],
                                 'commit': contract['commits'][side], 'helper_sha256': sha,
                                 'stdout': json.dumps(measured), 'started_at': time.isoformat(),
                                 'elapsed_seconds': 1, 'exit_code': 0, 'container_created': True,
                                 'cleanup_confirmed': True, 'workspace_read_only': True})
                rows.append({'lane': 'workload', 'round': index, 'side': side, 'report': path.name,
                             'sha256': cli.digest(path), 'measurement': measured})
        result = directory / 'result.json'
        cli.write(result, {'schema': cli.SCHEMA, 'commits': contract['commits'], 'images': images,
                           'samples': rows, 'order': 'baseline,candidate',
                           'contract_sha256': cli.digest(frozen / 'contract.json'),
                           'outcome': 'no-measurable-difference' if head == self.base else 'slower'})
        return self.ref(result)

    def audit(self, *, measured=120, live_image=HEAD_IMAGE):
        path = self.root / 'plan' / 'result.json'
        digest = cli.digest(path)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stage = {'commit': self.head, 'baseline_perf': self.perf('before', self.base, start, 100),
                 'perf': self.perf('after', self.head, start + timedelta(minutes=1), measured)}
        for mode, marker in (('unit', 'UNIT_PASS'), ('live', 'LIVE_PASS')):
            gate = self.root / f'{mode}.json'
            cli.write(gate, {'schema': cli.SCHEMA, 'commit': self.head, 'stdout': marker + '\n',
                             'image': live_image if mode == 'live' else HEAD_IMAGE, 'exit_code': 0,
                             'container_created': True, 'cleanup_confirmed': True, 'workspace_read_only': True})
            stage[mode] = self.ref(gate)
        receipts = self.root / 'receipts.json'
        receipts.write_text(json.dumps({'plan_sha256': digest, 'phases': {'P1': stage}}))
        return phase_audit.audit(repo=self.repo, plan=path, plan_sha256=digest,
                                 receipts=receipts, out=self.root / 'audit')

    def test_plan_and_audit_hold_interaction_and_dependents_for_operator_review(self):
        planned = self.plan()
        self.assertEqual(planned['review_gated'], ['P1'])
        markdown = (self.root / 'plan/plan.md').read_text()
        self.assertIn('Before candidate work, measure the trunk baseline', markdown)
        self.assertIn('Live', markdown)
        result = self.audit()
        self.assertEqual(result['phases']['P1']['status'], 'awaiting-operator-review')
        self.assertEqual(result['phases']['P2']['waits'], ['P1'])
        self.assertEqual(cli.sha(self.repo, 'HEAD'), self.head)

    def test_noninteracting_phase_with_evidence_allows_next_phase_to_be_assessed(self):
        self.spec['plan']['phases'][0]['interaction'] = False
        self.plan()
        result = self.audit()
        self.assertEqual(result['phases']['P1']['status'], 'evidence-verified')
        self.assertEqual(result['phases']['P2']['status'], 'evidence-missing')

    def test_exceeding_performance_bound_blocks_phase(self):
        self.plan()
        checked = self.audit(measured=255)
        self.assertIn('exceeds 250', checked['phases']['P1']['reason'])
        self.assertEqual(checked['phases']['P2']['status'], 'waiting-dependencies')

    def test_tests_from_another_image_cannot_clear_the_phase(self):
        self.plan()
        result = self.audit(live_image=BASE_IMAGE)
        self.assertEqual(result['phases']['P1']['status'], 'blocked')
        self.assertIn('exact candidate', result['phases']['P1']['reason'])

    def test_overlapping_independent_edits_and_missing_checks_block_planning(self):
        self.spec['plan']['phases'][1] = self.phase('P2', 'trunk', ['app.py'], False)
        result = self.plan('overlap')
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('independent phases overlap', result['reason'])
        self.spec['plan']['phases'][1]['depends_on'] = ['P1']
        del self.spec['plan']['phases'][1]['verify']['live']
        result = self.plan('missing')
        self.assertIn('unit, live and performance', result['reason'])

    def test_open_questions_and_explicitly_obvious_change_do_not_generate_plan(self):
        self.spec['open_questions'] = ['Which runtime can run the fixture?']
        self.assertEqual(self.plan('next')['status'], 'needs-prototype')
        self.spec.update(open_questions=[], obvious=True)
        self.assertEqual(self.plan('simple')['status'], 'simple-change')
