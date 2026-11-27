from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from runner.sandbox import SandboxError, run_sandbox
from workbench import browser_forensics, cli


class ScriptBindingTests(unittest.TestCase):
    port = 18789
    origin = 'http://127.0.0.1:18789'

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        self.source = b'function work() {\n  return 1;\n}\n'
        self.source_digest = sha256(self.source).hexdigest()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Browser Forensics Test')
        cli.git(self.repo, 'config', 'user.email', 'browser-test@example.invalid')
        (self.repo / 'app.mjs').write_bytes(self.source)
        (self.repo / 'ambiguous.mjs').write_bytes(self.source)
        cli.git(self.repo, 'add', 'app.mjs', 'ambiguous.mjs')
        cli.git(self.repo, 'commit', '-qm', 'tracked browser scripts')
        self.revision = cli.sha(self.repo, 'HEAD')

    @staticmethod
    def script_observation(url, digest):
        path_url = urlsplit(url)._replace(query='', fragment='').geturl()
        return {'url': path_url, 'url_sha256': sha256(url.encode()).hexdigest(), 'sha256': digest}

    def bindings(self, observations, *, complete=True):
        receipt = {'scripts_complete': complete,
                   'scripts': [self.script_observation(url, digest) for url, digest in observations]}
        return browser_forensics._bindings(self.repo, self.revision, self.port, receipt)

    def test_only_unambiguous_same_origin_git_bytes_attest_the_exact_url(self):
        url = self.origin + '/app.mjs?cache=1'
        ambiguous = self.origin + '/ambiguous.mjs'
        mismatched = sha256(b'different script').hexdigest()
        (self.repo / 'app.mjs').write_bytes(b'changed working tree')
        (self.repo / 'untracked.mjs').write_bytes(self.source)
        self.assertEqual(self.bindings([
            (url, self.source_digest),
            (url, self.source_digest),
            (self.origin + '/app.mjs?cache=2', mismatched),
            (ambiguous, self.source_digest),
            (ambiguous, mismatched),
            (self.origin + '/untracked.mjs', self.source_digest),
            ('https://127.0.0.1:18789/app.mjs', self.source_digest),
            ('http://127.0.0.1:18790/app.mjs', self.source_digest),
            ('http://localhost:18789/app.mjs', self.source_digest),
        ]), {sha256(url.encode()).hexdigest(): {'path': 'app.mjs', 'sha256': self.source_digest, 'lines': 3}})

    def test_incomplete_debugger_inventory_cannot_attest_a_script(self):
        self.assertEqual(self.bindings([
            (self.origin + '/app.mjs', self.source_digest),
        ], complete=False), {})

    def test_capture_pins_both_trials_and_imports_only_attested_frame_locations(self):
        pinned = 'sha256:' + 'a' * 64
        source_url = self.origin + '/app.mjs?cache=1'
        shared_url = self.origin + '/app.mjs?cache=2'
        unobserved_url = self.origin + '/ambiguous.mjs'
        image_requests = []
        capture_requests = []

        def attest(repo, image, _timeout, *, commit):
            self.assertEqual((repo, commit), (self.repo.resolve(), self.revision))
            image_requests.append(image)
            return pinned

        def sandbox(stage, image, command, _timeout, **options):
            self.assertEqual(image, pinned)
            self.assertEqual(command, ['node', '/workspace/capture-browser.mjs'])
            self.assertEqual((options['source_root'], options['workspace_read_only']), ('/app', True))
            request = json.loads((stage / 'browser-request.json').read_text())
            capture_requests.append(request)
            paused = request['paused']
            events = [
                {'pid': 1, 'tid': tid, 'ph': 'X', 'cat': 'devtools.timeline', 'name': 'FunctionCall',
                 'ts': 230 + index * 10 + int(paused), 'dur': 5,
                 'args': {'data': {'url': url, 'lineNumber': 1, 'functionName': name}}}
                for index, (tid, url, name) in enumerate((
                    (1, source_url, 'work'), (1, unobserved_url, 'unknown'),
                    (1, shared_url, 'mixedWork'), (2, shared_url, 'mixedWork'),
                    (2, shared_url, 'workerOnly'),
                ))
            ]
            events += [
                {'pid':1,'tid':1,'ph':'I','cat':'blink.user_timing','name':'iboard:' + name,'ts':stamp}
                for name, stamp in (('before-action',150), ('window-start',200), ('window-end',100200))
            ]
            for thread, symbols in ((1, ('work', 'mixedWork')), (2, ('mixedWork', 'workerOnly'))):
                profile_id = f'profile-{thread}'
                events.append({'pid': 1, 'tid': thread, 'ph': 'P', 'name': 'Profile', 'id': profile_id,
                               'ts': 220, 'args': {'data': {'startTime': 220}}})
                nodes = [
                    {'id': index, 'callFrame': {'functionName': symbol,
                                                'url': source_url if symbol == 'work' else shared_url,
                                                'lineNumber': 0}}
                    for index, symbol in enumerate(symbols, 1)
                ]
                events.append({
                    'pid': 1, 'tid': 99, 'ph': 'P', 'name': 'ProfileChunk', 'id': profile_id, 'ts': 320,
                    'args': {'data': {'cpuProfile': {'nodes': nodes, 'samples': [1, 2, 1]},
                                      'timeDeltas': [10, 40, 40]}},
                })
            artifact = options['profile_output']
            raw = json.dumps({'traceEvents': events}).encode()
            artifact.write_bytes(raw)
            receipt = {
                'kind': 'iboard-browser-trace', 'port': self.port, 'route': '/', 'window_ms': 100,
                'paused': paused, 'playback_rate': 0 if paused else 1, 'clicked': False,
                'browser_closed': True, 'server_stopped': True, 'scripts_complete': True,
                'scripts': [self.script_observation(url, self.source_digest) for url in (source_url, shared_url)],
                'observed': {
                    'before': {'animations': [{'id': 'pulse', 'type': 'CSSAnimation', 'time': 20}]},
                    'after': {'animations': [{'id': 'pulse', 'type': 'CSSAnimation',
                                              'time': 20 if paused else 120}]},
                },
            }
            return {'exit_code': 0, 'container_created': True, 'cleanup_confirmed': True, 'image': image,
                    'profile_path': str(artifact), 'profile_sha256': sha256(raw).hexdigest(),
                    'stdout': json.dumps(receipt)}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / 'capture-browser.mjs'
            driver.write_text('// synthetic capture helper\n')
            with (patch.object(browser_forensics, 'DRIVER', driver),
                  patch.object(browser_forensics.workflow, '_verified_image', side_effect=attest),
                  patch.object(browser_forensics, 'run_sandbox', side_effect=sandbox)):
                report_path = browser_forensics.capture(
                    repo=self.repo, revision=self.revision, image='app:test',
                    request={'entry': 'app.mjs', 'loader': None, 'args': [], 'port': self.port,
                             'route': '/', 'click': None, 'observe': '#card', 'window_ms': 100},
                    pause_animations=True, timeout=10, memory_mb=512, out=root / 'capture')
            report = cli.read(report_path)
            self.assertEqual(image_requests, ['app:test', pinned])
            self.assertEqual([request['paused'] for request in capture_requests], [False, True])
            self.assertEqual(report['mechanism']['status'], 'reduced-animation-advance')
            for trial in report['trials'].values():
                self.assertEqual(trial['receipt']['scripts'][0]['url'], self.origin + '/app.mjs')
                finding = trial['trace']['finding']
                for key in ('duration_groups', 'cpu_frames'):
                    with self.subTest(key=key):
                        frames = {item['symbol']: item for item in finding[key]}
                        self.assertEqual((frames['work']['source_file'], frames['work']['source_status']),
                                         ('app.mjs', 'verified-git-source'))
                        self.assertNotEqual(frames['mixedWork']['source_status'], 'verified-git-source')
                        self.assertNotEqual(frames['workerOnly']['source_status'], 'verified-git-source')
                durations = {item['symbol']: item for item in finding['duration_groups']}
                self.assertEqual(durations['unknown']['source_status'], 'reported-browser-location')
                self.assertEqual(durations['mixedWork']['events'], 2)
                hot_frames = [frame for path in finding['hot_cpu_paths'] for frame in path['frames']]
                self.assertEqual({frame['symbol'] for frame in hot_frames}, {'work', 'mixedWork', 'workerOnly'})
                self.assertNotIn('cache=', json.dumps(hot_frames))
                artifact = trial['trace']['artifact']
                self.assertEqual(cli.digest(Path(artifact['snapshot'])), artifact['sha256'])


class AnimationMechanismTests(unittest.TestCase):
    @staticmethod
    def animation(time, ident='pulse', kind='CSSAnimation'):
        return {'id': ident, 'type': kind, 'time': time}

    @staticmethod
    def trial(before, after, *, paused=False, rate=None):
        return {'receipt': {
            'paused': paused,
            'playback_rate': (0 if paused else 1) if rate is None else rate,
            'observed': {'before': {'animations': before}, 'after': {'animations': after}},
        }}

    def test_real_timeline_advance_and_freeze_identify_the_named_animation(self):
        report = browser_forensics._mechanism({
            'baseline': self.trial([self.animation(20)], [self.animation(30)]),
            'paused': self.trial([self.animation(100)], [self.animation(99)], paused=True),
        })
        self.assertEqual(report, {
            'status': 'reduced-animation-advance',
            'animations': [{'id': 'pulse', 'type': 'CSSAnimation',
                            'baseline_delta_ms': 10, 'paused_delta_ms': -1}],
        })

    def test_missing_intervention_is_not_reported_as_an_effect(self):
        report = browser_forensics._mechanism({
            'baseline': self.trial([self.animation(20)], [self.animation(80)]),
        })
        self.assertEqual(report['status'], 'not-run')

    def test_unmatched_duplicate_or_unconfirmed_observations_are_inconclusive(self):
        baseline = self.trial([self.animation(20)], [self.animation(80)])
        paused = self.trial([self.animation(100)], [self.animation(100)], paused=True)
        with self.subTest(case='duplicate identifier in an observation'):
            self.assert_inconclusive(
                self.trial([self.animation(20), self.animation(25)], [self.animation(80)]), paused)
        with self.subTest(case='animation type changed between trials'):
            self.assert_inconclusive(baseline, self.trial(
                [self.animation(100, kind='CSSTransition')], [self.animation(100, kind='CSSTransition')],
                paused=True))
        with self.subTest(case='unnamed animation'):
            self.assert_inconclusive(
                self.trial([self.animation(20, ident='')], [self.animation(80, ident='')]),
                self.trial([self.animation(100, ident='')], [self.animation(100, ident='')], paused=True))
        with self.subTest(case='unconfirmed pause'):
            self.assert_inconclusive(baseline, self.trial(
                [self.animation(100)], [self.animation(100)], paused=True, rate=1))
        with self.subTest(case='baseline time moved backward'):
            self.assert_inconclusive(self.trial([self.animation(80)], [self.animation(20)]), paused)
        with self.subTest(case='animation still progressed when paused'):
            self.assert_inconclusive(baseline, self.trial(
                [self.animation(100)], [self.animation(102)], paused=True))

    def assert_inconclusive(self, baseline, paused):
        result = browser_forensics._mechanism({'baseline': baseline, 'paused': paused})
        self.assertEqual(result['status'], 'inconclusive')


class BrowserSandboxTests(unittest.TestCase):
    def test_trace_export_rejects_a_wrong_helper_or_an_unscoped_capture(self):
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            trace = workspace / 'evidence.cdptrace'
            options = {'source_root': '/app', 'workspace_read_only': True,
                       'profile_output': trace, 'find_executable': lambda _: 'docker'}

            def unexpected_docker(*_args, **_kwargs):
                self.fail('invalid capture reached Docker')

            for helper, overrides in (
                    ('node-profile.mjs', {}),
                    ('capture-browser.mjs', {'workspace_read_only': False}),
                    ('capture-browser.mjs', {'source_root': '/workspace'}),
                    ('capture-browser.mjs', {'profile_output': workspace / 'heap.heapsnapshot'})):
                with self.subTest(helper=helper, overrides=overrides):
                    with self.assertRaisesRegex(SandboxError, 'invalid scoped profile input'):
                        run_sandbox(workspace, 'example:latest', ['node', '/workspace/' + helper], 10,
                                    invoke=unexpected_docker, **(options | overrides))


if __name__ == '__main__':
    unittest.main()
