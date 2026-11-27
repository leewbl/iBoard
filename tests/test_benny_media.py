import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock

from test_benny import CHANNEL, Fixture, ROOT, RULES, WORKSPACE, reply
from test_runner import tiny_png
from workbench import benny, benny_media, cli


class MediaTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.fixture = Fixture(self.root)
        self.addCleanup(self.fixture.__exit__)
        self.fixture.__enter__()

    def collect(self, name, **options):
        route = benny.freeze(rules=RULES, slack=self.fixture.slack, channel=CHANNEL, message_ts=ROOT,
                             out=self.root/f'route-{name}')
        snapshot = cli.read(Path(route['receipt']))['snapshot']
        return benny_media.collect(self.fixture.slack, snapshot, WORKSPACE, self.root/name,
                                   time.monotonic() + 30, **options)

    def test_log_is_retained_and_only_named_source_files_are_read(self):
        data = b'Exact observed log output.\n'
        self.fixture.attach('F123LOG', data, 'text/plain')
        self.fixture.attach('F123PRIVATE', b'Other report.', 'text/plain')
        self.fixture.root['files'] = [{'id': 'F123LOG'}]
        self.fixture.pages[0].append(reply(1, 'Repeated attachment link.', files=[{'id': 'F123LOG'}]))
        items, excerpts, failed = self.collect('log', download=True)
        self.assertFalse(failed)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(items[0]['messages']), 2)
        self.assertEqual(items[0]['status'], 'read')
        self.assertEqual(excerpts['A1.1']['text'], data.decode())
        self.assertEqual(excerpts['A1.1']['sha256'], cli.digest(self.root/'log/F123LOG.txt'))
        reads = [query['file'] for _, endpoint, query in self.fixture.calls if endpoint == 'files.info']
        self.assertEqual(reads, ['F123LOG'])
        self.assertTrue(benny_media.unchanged(self.root/'log', items))
        (self.root/'log/F123LOG.txt').write_bytes(b'altered')
        self.assertFalse(benny_media.unchanged(self.root/'log', items))

    def test_large_utf8_log_has_bounded_exact_excerpts_and_is_marked_partial(self):
        data = '开头\n'.encode() + b'line of log data\n' * 350 + '结尾\n'.encode()
        self.fixture.attach('F123LOG', data, 'text/plain')
        self.fixture.root['files'] = [{'id': 'F123LOG'}]
        items, excerpts, _ = self.collect('excerpt', download=True)
        self.assertEqual(items[0]['status'], 'excerpted')
        self.assertEqual(len(excerpts), 2)
        self.assertTrue(all(value['excerpt'] and value['text'] in data.decode() for value in excerpts.values()))
        self.assertLessEqual(sum(len(value['text']) for value in excerpts.values()), benny_media.MAX_EXCERPT)

    def test_external_url_wrong_workspace_and_redirect_are_never_followed(self):
        self.fixture.root['files'] = [{'id': 'F123ONE'}, {'id': 'F123TWO'}, {'id': 'F123THREE'}]
        self.fixture.attach('F123ONE', b'one', 'text/plain', url_private_download='https://untrusted.invalid/download')
        self.fixture.attach('F123TWO', b'two', 'text/plain',
                            url_private_download='https://files.slack.com/files-pri/T999OTHER-F123TWO/download/evidence')
        self.fixture.attach('F123THREE', b'three', 'text/plain', redirect='https://private.invalid/elsewhere')
        items, excerpts, _ = self.collect('rejected', download=True)
        self.assertEqual([record['status'] for record in items], ['unread'] * 3)
        self.assertEqual(excerpts, {})
        downloads = [endpoint for _, endpoint, _ in self.fixture.calls if endpoint.startswith('/files-pri/')]
        self.assertEqual(len(downloads), 1)
        self.assertIn('F123THREE', downloads[0])
        self.assertIn('HTTP 302', items[-1]['reason'])

    def test_size_mismatch_and_bad_image_do_not_create_evidence(self):
        self.fixture.root['files'] = [{'id': 'F123SIZE'}, {'id': 'F123BAD'}]
        self.fixture.attach('F123SIZE', b'longer than the metadata', 'text/plain', size=3)
        self.fixture.attach('F123BAD', b'invalid PNG', 'image/png')
        vision = Mock()
        items, excerpts, _ = self.collect('bad', inspect_media=True, vision=vision)
        self.assertEqual([record['status'] for record in items], ['unread'] * 2)
        self.assertEqual(excerpts, {})
        vision.assert_not_called()

    def test_screenshot_observation_requires_opt_in_and_is_bound_to_its_bytes(self):
        self.fixture.root['files'] = [{'id': 'F123SHOT'}]
        self.fixture.attach('F123SHOT', tiny_png(), 'image/png')
        vision = Mock(return_value='One red pixel is visible.')
        items, evidence, _ = self.collect('no-media', download=True, vision=vision)
        self.assertEqual(items[0]['status'], 'downloaded')
        self.assertEqual(evidence, {})
        vision.assert_not_called()
        items, evidence, _ = self.collect('vision', inspect_media=True, vision=vision)
        self.assertEqual(items[0]['status'], 'read')
        self.assertEqual(evidence['V1']['scope'], 'screenshot')
        self.assertEqual(evidence['V1']['sha256'], items[0]['file']['sha256'])
        self.assertEqual(vision.call_args.args[0], tiny_png())

    def test_media_provider_error_does_not_attempt_more_attachment_calls(self):
        self.fixture.root['files'] = [{'id': 'F123SHOT'}, {'id': 'F123OTHER'}]
        self.fixture.attach('F123SHOT', tiny_png(), 'image/png')
        self.fixture.attach('F123OTHER', tiny_png(), 'image/png')
        items, excerpts, failed = self.collect('provider-error', inspect_media=True,
                                               vision=Mock(side_effect=RuntimeError('provider quota')))
        self.assertTrue(failed)
        self.assertEqual(excerpts, {})
        self.assertEqual(items[0]['status'], 'downloaded')
        self.assertEqual(items[1]['reason'], 'media provider stopped')
        self.assertEqual([query['file'] for _, endpoint, query in self.fixture.calls if endpoint == 'files.info'], ['F123SHOT'])

    @unittest.skipUnless(os.environ.get('WORKBENCH_VIDEO_TEST') == '1' and shutil.which('ffmpeg') and shutil.which('ffprobe'),
                         'enable video tests with FFmpeg and FFprobe')
    def test_real_mp4_frames_have_offsets_and_are_only_partial_evidence(self):
        source = self.root/'clip.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=80x48:d=1:r=20',
                        '-c:v', 'libx264', '-threads', '1', str(source)], check=True, timeout=15)
        self.fixture.root['files'] = [{'id': 'F123CLIP'}]
        self.fixture.attach('F123CLIP', source.read_bytes(), 'video/mp4')
        items, excerpts, failed = self.collect('video', inspect_media=True, vision=lambda *_: 'Blue frame.')
        self.assertFalse(failed)
        self.assertEqual(items[0]['status'], 'sampled', items[0].get('reason'))
        self.assertEqual([frame['offset_seconds'] for frame in items[0]['frames']], [0.1, 0.5, 0.85])
        self.assertTrue(all(row['scope'] == 'video-frame' for row in excerpts.values()))
        self.assertTrue(benny_media.unchanged(self.root/'video', items))
