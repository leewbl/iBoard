import json
from pathlib import Path
import shutil
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zlib

from workbench import cli, delivery, publish, visual_parity


def png(color):
    def chunk(kind, payload):
        return (struct.pack('>I', len(payload)) + kind + payload
                + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 64, 64, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + bytes(color) * 64) * 64)) + chunk(b'IEND', b''))


class VisualParityTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.repo = self.root / 'source'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Test Operator')
        cli.git(self.repo, 'config', 'user.email', 'trace@example.invalid')
        (self.repo / 'index.html').write_text('<main>baseline</main>')
        cli.git(self.repo, 'add', 'index.html')
        cli.git(self.repo, 'commit', '-qm', 'baseline')
        self.base = cli.sha(self.repo, 'HEAD')
        self.plan = {'schema': 1, 'viewport': {'width': 64, 'height': 64},
                     'states': [{'id': 'closed', 'path': 'index.html', 'steps': []},
                                {'id': 'open', 'path': 'index.html', 'steps': [{'action': 'click', 'selector': '#toggle'}]}]}
        self.plan_path = self.root / 'manifest.json'
        self.plan_path.write_text(json.dumps(self.plan))
        self.calls = 0
        self.unstable = False
        self.changed = False

    def capture(self, workspace, image, command, timeout, *, screenshot_output, **kwargs):
        self.calls += 1
        state = next(item for item in self.plan['states'] if item['id'] == command[-1])
        alternate = self.unstable and self.calls % 2 == 0 or self.changed and state['id'] == 'open'
        screenshot_output.write_bytes(png((200, 30, 60) if alternate else (20, 50, 60)))
        checksum = cli.digest(screenshot_output)
        receipt = {'kind': 'visual-parity-capture', 'state': state['id'], 'actions': state['steps'],
                   'viewport': self.plan['viewport'], 'browser': 'chromium', 'playwright': 'test',
                   'chromium_sha256': '9'*64, 'screenshot_sha256': checksum}
        return {'exit_code': 0, 'cleanup_confirmed': True, 'container_created': True, **kwargs,
                'started_at': '2026-10-01T00:00:00+00:00', 'elapsed_seconds': 4.5,
                'command': command, 'create_stdout': f'{self.calls:064x}\n', 'image': image,
                'screenshot_sha256': checksum, 'screenshot_path': str(screenshot_output), 'stdout': json.dumps(receipt)}

    def test_recomputes_pixel_evidence_and_refuses_stale_or_changed_candidate(self):
        sha = 'sha256:' + 'a'*64
        with patch.object(visual_parity.workflow, '_verified_image', return_value=sha), \
                patch.object(visual_parity, '_same_runtime'), patch.object(visual_parity, 'run_sandbox', self.capture):
            baseline = visual_parity.capture_baseline(self.repo, self.base, 'image', self.plan_path,
                                                      self.root/'baseline', 60, 1024)
            copied = self.root / 'archived-baseline'
            shutil.copytree(self.root/'baseline', copied)
            portable = visual_parity.bind(copied/'baseline.json', baseline['sha256'], self.base)
            self.assertEqual(portable['sha256'], baseline['sha256'])
            self.assertEqual(visual_parity.bind(portable['path'], portable['sha256'], self.base, memory_mb=1024,
                                               before='2026-10-01T00:00:05+00:00'), portable)
            with self.assertRaisesRegex(visual_parity.ParityError, 'completed before the phase began'):
                visual_parity.bind(portable['path'], portable['sha256'], self.base, memory_mb=1024,
                                   before='2026-10-01T00:00:04+00:00')
            with self.assertRaisesRegex(visual_parity.ParityError, 'approved memory limit'):
                visual_parity.bind(portable['path'], portable['sha256'], self.base, memory_mb=512)
            (self.repo/'index.html').write_text('<main>candidate</main>')
            cli.git(self.repo, 'add', 'index.html')
            cli.git(self.repo, 'commit', '-qm', 'candidate')
            head = cli.sha(self.repo, 'HEAD')
            success, identical = visual_parity.compare_candidate(self.repo, self.base, baseline, head, 'image',
                                                                   self.root/'same', 60, 1024)
            self.assertTrue(identical)
            data = {'visual_baseline': baseline, 'visual_parity_result': success, 'surface': 'ui', 'base_commit': self.base}
            self.assertEqual(visual_parity.require(data, head), success)
            self.changed = True
            failure, identical = visual_parity.compare_candidate(self.repo, self.base, baseline, head, 'image',
                                                                   self.root/'changed', 60, 1024)
            self.assertFalse(identical)
            checks = cli.read(failure['path'])['states']
            self.assertEqual(checks['closed']['independent-verifier']['changed_pixels'], 0)
            self.assertEqual(checks['open']['independent-verifier']['changed_pixels'], 4096)
            self.assertTrue((self.root/'changed'/'open'/'independent-verifier-diff.png').is_file())
            with self.assertRaisesRegex(delivery.DeliveryError, 'differs from the required candidate'):
                delivery._independent_verification({**data, 'visual_parity_result': failure}, head)
            task = self.root / 'task.json'
            data['repo'] = str(self.repo)
            cli.write(task, {'schema': 1, **data})
            (self.root/'same'/'closed'/'implementer.png').write_bytes(png((1, 1, 1)))
            with self.assertRaisesRegex(visual_parity.ParityError, 'hashes changed'):
                visual_parity.require(data, head)
            with self.assertRaisesRegex(publish.PublishError, 'hashes changed'):
                publish._local_remote_guard(task, data, head, object(), 'owner/fork')
            cli.write(task, {'schema': 1, **data, 'visual_baseline': None})
            with self.assertRaisesRegex(delivery.DeliveryError, 'binding changed'):
                delivery.recheck_visual_write(task, data, head, 'owner/fork')
            self.plan['viewport']['width'] = 80
            (self.root/'baseline'/'manifest.json').write_text(json.dumps(self.plan))
            with self.assertRaisesRegex(visual_parity.ParityError, 'manifest changed'):
                visual_parity.read_baseline(baseline, self.base)

    def test_does_not_freeze_an_unstable_baseline(self):
        self.unstable = True
        with patch.object(visual_parity.workflow, '_verified_image', return_value='sha256:' + 'a'*64), \
                patch.object(visual_parity, 'run_sandbox', self.capture):
            with self.assertRaisesRegex(visual_parity.ParityError, 'baseline is unstable'):
                visual_parity.capture_baseline(self.repo, self.base, 'image', self.plan_path,
                                               self.root/'baseline', 60, 1024)
        self.assertFalse((self.root/'baseline'/'baseline.json').exists())

    def test_capturing_preserves_a_sandbox_failure_without_accepting_baseline(self):
        with patch.object(visual_parity.workflow, '_verified_image', return_value='sha256:' + 'a'*64), \
                patch.object(visual_parity, 'run_sandbox', side_effect=visual_parity.SandboxError('Docker stopped')):
            with self.assertRaisesRegex(visual_parity.ParityError, 'report saved'):
                visual_parity.capture_baseline(self.repo, self.base, 'image', self.plan_path,
                                               self.root/'baseline', 60, 1024)
        failure = json.loads((self.root/'baseline'/'closed'/'implementer.json').read_text())
        self.assertEqual(failure['error'], 'Docker stopped')
        self.assertFalse(failure['cleanup_confirmed'])
