from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from workbench import browser_probe, cli
from workbench.runtime_forensics import ForensicsError


SOURCE = b'({setup() {}, observe() {return {Height: 800}}, intervene() {}, restore() {}, cleanup() {}})'
HEIGHT_RULE = {'direction': 'decrease', 'min_change': 100, 'restore_tolerance': 1, 'max_spread': 1}
RATE_RULE = {'direction': 'increase', 'min_change': 10, 'restore_tolerance': 1, 'max_spread': 1}


class ProbePreparationTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Browser Probe Test')
        cli.git(self.repo, 'config', 'user.email', 'browser-probe-test@example.invalid')
        (self.repo / 'measure.js').write_bytes(SOURCE)
        self.data = {'schema': 1, 'source': 'measure.js', 'samples': 2, 'settle_ms': 50,
                     'metrics': {'Height': dict(HEIGHT_RULE)}}

    def commit(self, raw=None):
        raw = raw if raw is not None else json.dumps(self.data).encode('utf-8')
        (self.repo / 'experiment.json').write_bytes(raw)
        cli.git(self.repo, 'add', 'experiment.json', 'measure.js')
        cli.git(self.repo, 'commit', '-qm', 'freeze probe')
        return cli.sha(self.repo, 'HEAD'), raw

    def test_manifest_and_source_are_read_from_the_explicit_git_commit(self):
        rev, raw = self.commit()
        (self.repo / 'measure.js').write_text('working-tree body')
        (self.repo / 'experiment.json').write_text('working-tree manifest')
        config, source = browser_probe.prepare(self.repo, rev, 'experiment.json')
        self.assertEqual(source, SOURCE)
        self.assertEqual(config['revision'], rev)
        self.assertEqual(config['manifest'], {'path': 'experiment.json', 'sha256': sha256(raw).hexdigest()})
        self.assertEqual(config['source'], {'path': 'measure.js', 'sha256': sha256(SOURCE).hexdigest()})
        self.assertEqual(browser_probe.request(config), {
            'sha256': sha256(SOURCE).hexdigest(), 'samples': 2, 'settle_ms': 50, 'metrics': ['Height'],
        })

    def test_rejects_untracked_or_traversing_sources(self):
        self.data['source'] = '../measure.js'
        rev, _ = self.commit()
        with self.assertRaisesRegex(ForensicsError, 'repository-relative'):
            browser_probe.prepare(self.repo, rev, 'experiment.json')
        self.data['source'] = 'untracked.js'
        (self.repo / 'untracked.js').write_bytes(SOURCE)
        rev, _ = self.commit()
        with self.assertRaisesRegex(ForensicsError, 'tracked regular'):
            browser_probe.prepare(self.repo, rev, 'experiment.json')

    def test_rejects_duplicate_manifest_keys_and_nonfinite_rules(self):
        rev, _ = self.commit(b'{"schema":1,"schema":1}')
        with self.assertRaisesRegex(ForensicsError, 'duplicate key'):
            browser_probe.prepare(self.repo, rev, 'experiment.json')
        self.data['metrics']['Height']['min_change'] = float('inf')
        rev, _ = self.commit()
        with self.assertRaisesRegex(ForensicsError, 'metric rules'):
            browser_probe.prepare(self.repo, rev, 'experiment.json')

    def test_rejects_oversize_or_invalid_utf8_code(self):
        (self.repo / 'measure.js').write_bytes(b' ' * (browser_probe.SOURCE_LIMIT + 1))
        rev, _ = self.commit()
        with self.assertRaisesRegex(ForensicsError, 'allowed size'):
            browser_probe.prepare(self.repo, rev, 'experiment.json')
        (self.repo / 'measure.js').write_bytes(b'\xff')
        rev, _ = self.commit()
        with self.assertRaisesRegex(ForensicsError, 'UTF-8 JavaScript'):
            browser_probe.prepare(self.repo, rev, 'experiment.json')


class ProbeEvidenceTests(TestCase):
    def setUp(self):
        self.config = {'source': {'sha256': 'a' * 64}, 'samples': 2,
                       'metrics': {'height': dict(HEIGHT_RULE), 'rate': dict(RATE_RULE)}}
        self.window = {'pid': 40, 'tid': 12, 'start_us': 1000, 'end_us': 3000}
        self.receipt = {'sha256': 'a' * 64, 'cleanup_confirmed': True, 'phases': []}
        self.events = []
        names = ['setup-complete']
        for phase in browser_probe.PHASES:
            height, rate = (680, 25) if phase.startswith('intervention') else (800, 12)
            self.receipt['phases'].append({'name': phase, 'samples': [
                {'height': height, 'rate': rate}, {'height': height + 1, 'rate': rate + 1},
            ]})
            names.extend((phase + ':start', phase + ':end'))
        names.append('cleanup-complete')
        self.events = [{'ph': 'I', 'cat': 'blink.user_timing', 'name': 'iboard:probe:' + name,
                        'ts': 1100 + index * 100, 'pid': 40, 'tid': 12}
                       for index, name in enumerate(names)]

    def evaluate(self):
        return browser_probe.evaluate(self.config, self.receipt, {'traceEvents': self.events}, self.window)

    def test_requires_both_effect_directions_in_both_rounds_and_reports_timings(self):
        self.events.reverse()
        report = self.evaluate()
        self.assertEqual(report['status'], 'hypothesis-supported')
        self.assertEqual([phase['name'] for phase in report['phases']], list(browser_probe.PHASES))
        self.assertEqual(report['trace']['setup_complete_us'], 1100)
        self.assertEqual(report['trace']['cleanup_complete_us'], 2200)
        self.assertEqual(report['phases'][0]['elapsed_ms'], 0.1)
        self.assertEqual([row['minimum_effect'] for row in report['metrics']['height']['rounds']], [119, 119])
        self.assertEqual([row['minimum_effect'] for row in report['metrics']['rate']['rounds']], [12, 12])

    def test_treats_drift_failed_second_intervention_and_noise_as_inconclusive(self):
        correct = deepcopy(self.receipt)
        scenarios = (
            (3, [800, 801], 'effect_met'),
            (4, [790, 791], 'restoration_met'),
            (1, [680, 690], 'stable'),
        )
        for phase, samples, criterion in scenarios:
            with self.subTest(criterion=criterion):
                self.receipt = deepcopy(correct)
                for sample, height in zip(self.receipt['phases'][phase]['samples'], samples):
                    sample['height'] = height
                report = self.evaluate()
                self.assertEqual(report['status'], 'inconclusive')
                self.assertFalse(report['metrics']['height']['supported'])
                self.assertTrue(report['metrics']['rate']['supported'])

    def test_rejects_untrusted_receipt_identity_cleanup_and_observations(self):
        correct = deepcopy(self.receipt)
        for key, value in (('sha256', 'b' * 64), ('cleanup_confirmed', 1)):
            with self.subTest(key=key):
                self.receipt = deepcopy(correct)
                self.receipt[key] = value
                with self.assertRaisesRegex(ForensicsError, 'source or cleanup'):
                    self.evaluate()
        for value in (True, float('nan'), 10**13):
            with self.subTest(value=value):
                self.receipt = deepcopy(correct)
                self.receipt['phases'][0]['samples'][0]['height'] = value
                with self.assertRaisesRegex(ForensicsError, 'invalid observations'):
                    self.evaluate()
        self.receipt = deepcopy(correct)
        self.receipt['phases'][0]['samples'].pop()
        with self.assertRaisesRegex(ForensicsError, 'sample count'):
            self.evaluate()

    def test_rejects_missing_duplicate_out_of_order_or_cross_renderer_marks(self):
        correct = deepcopy(self.events)
        for mode in ('missing', 'duplicate', 'out-of-order', 'cross-renderer', 'outside-window'):
            with self.subTest(mode=mode):
                self.events = deepcopy(correct)
                if mode == 'missing':
                    self.events.pop()
                elif mode == 'duplicate':
                    self.events.append(dict(self.events[3]))
                elif mode == 'out-of-order':
                    self.events[2]['ts'] = self.events[4]['ts'] + 1
                elif mode == 'cross-renderer':
                    self.events[3]['tid'] += 1
                else:
                    self.events[3]['ts'] = self.window['end_us'] + 1
                with self.assertRaisesRegex(ForensicsError, 'trace markers'):
                    self.evaluate()
