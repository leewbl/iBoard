import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from workbench import browser_recording, cli


SHA = 'a'*40


def frame(i):
    return (b'\xff\xd8\xff\xc0\x00\x08\x08\x00\x20\x00\x40'
            + bytes([i]) + b'\xff\xd9')


class BrowserRecordingTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.path = self.root/'recording.jsonl'
        self.png = self.root/'observed.png'
        self.png.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        self.report = self.root/'report.json'
        self.items = [{'schema':1,'producer':'chromium-cdp','served_commit':SHA,'journey':'session-rename'},
                      *[{'ms':i*500+8,'page':1+i//2,'jpeg':base64.b64encode(frame(i)).decode()} for i in range(3)],
                      {'end':3}]

    def record(self):
        self.path.write_text('\n'.join(json.dumps(i) for i in self.items)+'\n')
        digest = cli.digest(self.path)
        event = {'phase':'ui','recording':True,'recordingJourney':'session-rename','recordingSha256':digest,
                 'screenshot':True,'servedCommit':SHA}
        receipt = {'schema':cli.SCHEMA,'recording_path':str(self.path),'recording_sha256':digest,
                   'screenshot_path':str(self.png),'screenshot_sha256':cli.digest(self.png),'commit':SHA,
                   'exit_code':0,'source_root':'/app','workspace_read_only':True,'container_created':True,
                   'cleanup_confirmed':True,'timed_out':False,'stdout':json.dumps(event)}
        cli.write(self.report, receipt)
        return receipt

    def test_commit_digest_and_browser_journey_are_checked(self):
        run = self.record()
        self.assertTrue(browser_recording.receipt(run,self.path,SHA,expected_journey='session-rename'))
        self.assertFalse(browser_recording.receipt(run,self.path,SHA,expected_journey='session-delete'))
        self.assertFalse(browser_recording.receipt(run,self.path,'b'*40))
        self.path.write_text(self.path.read_text() + '\n')
        self.assertFalse(browser_recording.receipt(run,self.path,SHA))

    def test_must_have_changed_frames_order_and_a_completed_footer(self):
        for change in ('repeat','time','footer'):
            before = [dict(x) for x in self.items]
            if change=='repeat':
                self.items[2]['jpeg']=self.items[3]['jpeg']=self.items[1]['jpeg']
            elif change=='time':
                self.items[2]['ms']=self.items[1]['ms']
            else:
                self.items[-1]['end']=4
            result=self.record()
            self.assertFalse(browser_recording.receipt(result,self.path,SHA))
            self.items=before

    def test_positive_review_must_cite_the_final_frame_and_screenshot(self):
        self.record()
        review_path=self.root/'review.json'
        vision=Mock(return_value='{"verdict":"visible","reason":"The final label is present.","references":["F0","FINAL"]}')
        with self.assertRaisesRegex(ValueError,'evidence-bound'):
            browser_recording.review(self.report,cli.digest(self.report),'Final label appears.',review_path,inspect=vision)
        self.assertFalse(review_path.exists())
        vision.return_value='{"verdict":"visible","reason":"The final label is present.","references":["F2","FINAL"]}'
        review=browser_recording.review(self.report,cli.digest(self.report),'Final label appears.',review_path,inspect=vision)
        self.assertEqual(review['verdict'],'visible')
        images, prompt, timeout = vision.call_args.args
        self.assertEqual(len(images),4)
        self.assertIn('Final label appears.', prompt)
        self.assertEqual(timeout,90)
        self.assertEqual(review['samples'][-1]['page'],2)

    def test_media_review_accepts_an_alias_to_the_artifact_directory(self):
        self.record()
        alias = self.root/'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        aliased_report, review_path = alias/self.report.name, alias/'review.json'
        vision = Mock(return_value='{"verdict":"visible","reason":"Label visible.","references":["F2","FINAL"]}')
        expected = 'Final label appears.'
        digest = cli.digest(self.report)
        proof = browser_recording.review(aliased_report, digest, expected, review_path, inspect=vision)
        self.assertEqual(browser_recording.audit_review(self.report, digest, expected, review_path,
                                                       cli.digest(review_path)), proof)
        indirect = self.root/'linked.json'
        indirect.symlink_to(self.report)
        with self.assertRaisesRegex(ValueError, 'bounded sandbox receipt'):
            browser_recording.audit_review(indirect, digest, expected, review_path, cli.digest(review_path))

    def test_media_changed_during_review_cannot_get_a_receipt(self):
        self.record()
        review_path=self.root/'review.json'

        def vision(*_):
            self.path.write_bytes(self.path.read_bytes()+b'\n')
            return '{"verdict":"visible","reason":"Label visible.","references":["F2","FINAL"]}'

        with self.assertRaisesRegex(ValueError,'changed during'):
            browser_recording.review(self.report,cli.digest(self.report),'Final label appears.',review_path,inspect=vision)
        self.assertFalse(review_path.exists())
