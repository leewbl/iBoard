import json
from pathlib import Path
import shutil
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zlib

from workbench import cli, phase_contract, verify_openclaw, visual_parity


VIEWPORT = {'width': 1280, 'height': 800}
JOURNEYS = ('empty', 'active', 'archived', 'all')
REQUESTS = {'empty': None, 'active': 'list-3', 'archived': 'list-1', 'all': 'list-2'}


def png(*, changed=False):
    def chunk(kind, content):
        return (struct.pack('>I', len(content)) + kind + content
                + struct.pack('>I', zlib.crc32(kind + content) & 0xffffffff))

    width, height = VIEWPORT['width'], VIEWPORT['height']
    row = b'\0' + b'\x26\x45\x64' * width
    left = row[0:1] + b'\xf6' + row[2:] if changed else row
    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
            + chunk(b'IDAT', zlib.compress(left + row * (height - 1))) + chunk(b'IEND', b''))


def gateway_events(journey, revision, digest):
    ready = {'phase': 'doctor', 'origin': 'http://127.0.0.1:18789',
             'ready': True, 'live': True, 'gatewayPid': 23}
    cleanup = {'phase': 'cleanup', 'exit': {'code': 0, 'signal': None}, 'scratchRemoved': True}
    ui = {'phase': 'ui', 'route': '/sessions', 'servedCommit': revision, 'screenshot': True, 'gatewayPid': 23,
          'handshake': {'protocol': 4, 'role': 'operator', 'method': 'token'},
          'readback': {'method': 'sessions.list', 'count': int(journey != 'empty')},
          'visual': {'state': journey, 'viewport': VIEWPORT, 'browser': 'chromium', 'playwright': 'test',
                     'chromium_sha256': '9' * 64, 'screenshot_sha256': digest,
                     'request_id': REQUESTS[journey]}}
    if journey != 'empty':
        active = {'key': 'agent:main:iboard-browser-verification', 'sessionId': 'active-session'}
        archived = {'key': 'agent:main:iboard-browser-archived', 'sessionId': 'archived-session'}
        scopes = (('active', 'load', [active], None), ('archived', 'filter', [archived], True),
                  ('all', 'filter', [active, archived], 'all'), ('active', 'filter-refresh', [active], None))
        steps = []
        for i, (state, trigger, rows, scope) in enumerate(scopes):
            step = {'state': state, 'trigger': trigger, 'source': 'sessions-page', 'requestId': f'list-{i}',
                    'gateway': rows, 'visibleKeys': [row['key'] for row in rows]}
            if scope is not None:
                step['archived'] = scope
            steps.append(step)
        ui['filter'] = {'fixtures': {'active': active, 'archived': archived}, 'steps': steps, 'sessionMutations': 0}
    return ready, ui, cleanup


class ConnectedVisualParityTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        (self.repo / 'src').mkdir(parents=True)
        (self.repo / 'ui').mkdir()
        (self.repo / 'src' / 'entry.ts').write_text('export const revision = 1;\n')
        (self.repo / 'ui' / 'vite.config.ts').write_text('export default {};\n')
        (self.repo / 'package.json').write_text('{"name":"openclaw"}\n')
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Visual Parity Test')
        cli.git(self.repo, 'config', 'user.email', 'visual@example.invalid')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'baseline')
        self.base = cli.sha(self.repo, 'HEAD')
        self.driver = visual_parity.CONNECTED_DRIVER
        self.manifest_path = self.root / 'manifest.json'
        self.original, self.built, self.candidate_image, self.changed_image = (
            'sha256:' + char * 64 for char in 'abcd')
        self.calls = []
        self.pixels = png(), png(changed=True)

    def make_manifest(self, *journeys):
        return {'schema': 1, 'viewport': dict(VIEWPORT),
                'openclaw_connected': {'driver_sha256': cli.digest(self.driver)},
                'rebuild': {'profile': visual_parity.OPENCLAW_PROFILE,
                            'sha256': cli.digest(visual_parity.OPENCLAW_RECIPE)},
                'states': [{'id': f'sessions-{journey}', 'journey': journey} for journey in journeys]}

    def sandbox(self, workspace, image, command, timeout, *, screenshot_output, **options):
        self.assertEqual(command[:2], ['node', '/workspace/control-ui-connected.mjs'])
        self.assertEqual(command[3], '--visual-state')
        journey = command[4]
        self.assertEqual(command[2], 'connected' if journey == 'empty' else 'session-status-filter')
        self.assertEqual(options['pids_limit'], 384)
        self.assertIs(options['workspace_read_only'], True)
        self.assertEqual(options['source_root'], '/app')
        workspace = Path(workspace)
        revision = (workspace / 'expected-commit.txt').read_text().strip()
        self.assertEqual(cli.digest(workspace / self.driver.name), cli.digest(self.driver))
        self.calls.append((revision, journey, image))
        screenshot_output.write_bytes(self.pixels[int(image == self.changed_image and journey == 'archived')])
        digest = cli.digest(screenshot_output)
        events = gateway_events(journey, revision, digest)
        proof_journey = 'control-ui-connected' if journey == 'empty' else verify_openclaw.SESSION_FILTER
        marker = verify_openclaw.BROWSER_JOURNEYS[proof_journey][1]
        return {**options, 'exit_code': 0, 'cleanup_confirmed': True, 'container_created': True,
                'started_at': '2026-10-01T00:00:00+00:00', 'elapsed_seconds': 4.5,
                'command': command, 'create_stdout': f'{len(self.calls):064x}\n', 'image': image,
                'screenshot_sha256': digest, 'screenshot_path': str(screenshot_output),
                'stdout': '\n'.join([*(json.dumps(event) for event in events), marker, ''])}

    def baseline(self, *journeys, out=None):
        cli.write(self.manifest_path, self.make_manifest(*journeys))
        return visual_parity.capture_baseline(self.repo, self.base, self.original, self.manifest_path,
                                              out or self.root / 'baseline', 60, 1024)

    def sandbox_patches(self, *builds):
        verified = patch.object(visual_parity.workflow, '_verified_image', side_effect=lambda repo, image, *a, **k: image)
        build = patch.object(visual_parity.workflow, 'build_source_image', side_effect=list(builds))
        runtime = patch.object(visual_parity, '_same_runtime')
        sandbox = patch.object(visual_parity, 'run_sandbox', side_effect=self.sandbox)
        return verified, build, runtime, sandbox

    def test_manifest_limits_connected_capture_to_fixed_driver_surface(self):
        self.assertEqual(visual_parity.manifest(self.make_manifest(*JOURNEYS))['states'],
                         [{'id': f'sessions-{journey}', 'journey': journey} for journey in JOURNEYS])
        mutations = {
            'mixed server': lambda plan: plan.update(server={}),
            'missing rebuild': lambda plan: plan.pop('rebuild'),
            'unsupported viewport': lambda plan: plan['viewport'].update(width=1279),
            'raw actions': lambda plan: plan['states'][0].update(steps=[]),
            'raw path': lambda plan: plan['states'][0].update(path='/sessions'),
            'unknown journey': lambda plan: plan['states'][0].update(journey='session-delete'),
            'duplicate state ID': lambda plan: plan['states'][1].update(id='sessions-empty'),
            'invalid driver digest': lambda plan: plan['openclaw_connected'].update(driver_sha256='z' * 64),
        }
        for name, change in mutations.items():
            with self.subTest(name=name), self.assertRaises(visual_parity.ParityError):
                plan = self.make_manifest('empty', 'archived')
                change(plan)
                visual_parity.manifest(plan)

    def test_authenticated_states_rebuild_compare_and_gate_one_pixel_difference(self):
        verified, build, runtime, sandbox = self.sandbox_patches(self.built, self.candidate_image, self.changed_image)
        with verified, build, runtime, sandbox:
            baseline = self.baseline(*JOURNEYS)
            self.assertEqual(self.calls, [(self.base, journey, self.built) for journey in JOURNEYS for _ in range(2)])
            self.assertEqual(cli.read(baseline['path'])['driver_sha256'], cli.digest(self.driver))
            relocated = self.root / 'archived-baseline'
            shutil.copytree(Path(baseline['path']).parent, relocated)
            self.assertEqual(visual_parity.bind(relocated / 'baseline.json', baseline['sha256'], self.base)['sha256'],
                             baseline['sha256'])
            (self.repo / 'src' / 'entry.ts').write_text('export const revision = 2;\n')
            cli.git(self.repo, 'commit', '-qam', 'candidate')
            head = cli.sha(self.repo, 'HEAD')
            equal_ref, equal = visual_parity.compare_candidate(self.repo, self.base, baseline, head, self.built,
                                                               self.root / 'equal', 60, 1024)
            self.assertTrue(equal)
            data = {'visual_baseline': baseline, 'visual_parity_result': equal_ref, 'surface': 'ui', 'base_commit': self.base}
            self.assertEqual(visual_parity.require(data, head), equal_ref)
            changed, equal = visual_parity.compare_candidate(self.repo, self.base, baseline, head, self.built,
                                                             self.root / 'changed', 60, 1024)
            self.assertFalse(equal)
            checks = cli.read(changed['path'])['states']
            for journey in JOURNEYS:
                for role in visual_parity.ROLES:
                    self.assertEqual(checks[f'sessions-{journey}'][role]['changed_pixels'], int(journey == 'archived'))
            with self.assertRaises(visual_parity.ParityError):
                visual_parity.require({**data, 'visual_parity_result': changed}, head)

    def test_rehashing_a_falsified_receipt_does_not_restore_connected_evidence(self):
        verified, build, runtime, sandbox = self.sandbox_patches(self.built)
        with verified, build, runtime, sandbox:
            ref = self.baseline('archived')
        path = Path(ref['path'])
        receipt_path = path.parent / 'sessions-archived' / 'independent-verifier.json'
        original = json.loads(receipt_path.read_bytes())
        mutations = {
            'wrong request': lambda ui: ui['visual'].update(request_id=REQUESTS['all']),
            'wrong screenshot digest': lambda ui: ui['visual'].update(screenshot_sha256='5' * 64),
            'wrong viewport': lambda ui: ui['visual'].update(viewport={'width': 1280, 'height': 640}),
            'unauthenticated': lambda ui: ui['handshake'].update(method='none'),
            'incorrect served commit': lambda ui: ui.update(servedCommit='5' * 40),
            'wrong scope': lambda ui: ui['filter']['steps'][1].update(archived='all'),
            'unrendered row': lambda ui: ui['filter']['steps'][1].update(visibleKeys=[]),
        }
        for reason, falsify in mutations.items():
            with self.subTest(reason=reason), self.assertRaises(visual_parity.ParityError):
                lines = original['stdout'].splitlines()
                ui = json.loads(lines[1])
                falsify(ui)
                lines[1] = json.dumps(ui)
                cli.write(receipt_path, {**original, 'stdout': '\n'.join(lines) + '\n'})
                baseline = cli.read(path)
                baseline['states']['sessions-archived']['independent-verifier']['report_sha256'] = cli.digest(receipt_path)
                cli.write(path, baseline)
                visual_parity.bind(path, cli.digest(path), self.base)

    def test_changing_the_connected_driver_invalidates_the_frozen_baseline(self):
        verified, build, runtime, sandbox = self.sandbox_patches(self.built)
        with verified, build, runtime, sandbox:
            baseline = self.baseline('empty')
        modified = self.root / self.driver.name
        modified.write_bytes(self.driver.read_bytes() + b'\n// changed after baseline\n')
        with patch.object(visual_parity, 'CONNECTED_DRIVER', modified):
            with self.assertRaises(visual_parity.ParityError):
                visual_parity.bind(baseline['path'], baseline['sha256'], self.base)

    def test_phase_contract_approves_connected_driver_and_freezes_gateway_entry(self):
        for name in ('unit.mjs', 'live.mjs', 'browser.mjs'):
            (self.repo / name).write_text('process.exit(0);\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'record phase check entrypoints')
        revision = cli.sha(self.repo, 'HEAD')
        self.manifest_path.write_text(json.dumps(self.make_manifest('archived')))
        helper = self.root / 'measure.mjs'
        helper.write_text('console.log("fixture benchmark");\n')
        benchmark = self.root / 'benchmark.json'
        claim = 'Measure opening the archived Sessions view.'
        cli.write(benchmark, {'claim': claim, 'scope': 'end-to-end', 'configuration': 'Controlled app run.',
                              'production_tuning': None, 'minimum_delta_pct': 25, 'rounds': 5,
                              'workload': {'helper': helper.name, 'input': 'Known input.', 'completed': 1,
                                           'digest': 'd' * 64, 'max_ops_per_second': 1000,
                                           'plausibility': 'One observed call.'}})

        def reference(path):
            return {'path': str(path), 'sha256': cli.digest(path)}

        entry = {'verify': {'unit': {'marker': 'UNIT_PASS'}, 'live': {'marker': 'LIVE_PASS'},
                            'perf': {'probe': claim}}}
        visual = {**reference(self.manifest_path), 'driver_sha256': cli.digest(self.driver)}
        step = {'image': self.original, 'unit': ['node', '/app/unit.mjs'], 'live': ['node', '/app/live.mjs'],
                'feature_doc': 'DOC.md', 'benchmark': reference(benchmark),
                'helpers': {helper.name: reference(helper)}, 'visual_parity': visual,
                'screenshot': {'command': ['node', '/workspace/browser.mjs'], 'marker': 'UI_PASS', 'exit_code': 0}}
        work = {'ui/vite.config.ts'}
        actual = phase_contract._step(self.repo, revision, work, self.root, entry, step)
        self.assertEqual(actual['visual_parity'], visual)
        self.assertEqual(actual['scripts']['visual_server'],
                         {'path': 'src/entry.ts', 'blob': cli.git(self.repo, 'rev-parse', revision + ':src/entry.ts')})
        with self.assertRaisesRegex(phase_contract.PhaseContractError, 'outside the approved edit scope'):
            phase_contract._step(self.repo, revision, work | {'src/entry.ts'}, self.root, entry, step)
        visual['driver_sha256'] = cli.digest(visual_parity.DRIVER)
        with self.assertRaisesRegex(phase_contract.PhaseContractError, 'capture driver changed'):
            phase_contract._step(self.repo, revision, work, self.root, entry, step)
