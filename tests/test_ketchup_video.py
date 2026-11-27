from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import ketchup_render, ketchup_video


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


class KetchupVideoTests(unittest.TestCase):
    def setUp(self):
        target = tempfile.TemporaryDirectory()
        self.addCleanup(target.cleanup)
        self.root = Path(target.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.session = self.root / 'chat.jsonl'
        messages = [
            ('user', 'Check the workbench results.'),
            ('assistant', 'The run is finishing.'),
            ('user', [{'type': 'tool_result', 'content': 'result'}]),
            ('user', '/ketchup'),
            ('user', '/ketchup-video'),
        ]
        self.session.write_text(''.join(json.dumps({
            'cwd': str(self.repo), 'sessionId': 'real-fixture', 'timestamp': (START + timedelta(seconds=index)).isoformat(),
            'uuid': str(index), 'type': role, 'message': {'role': role, 'content': text},
        }) + '\n' for index, (role, text) in enumerate(messages)))
        self.result = self.root / 'run.json'
        self.result.write_text(json.dumps({'runs': 2, 'finished': (START + timedelta(seconds=2)).isoformat()}))
        self.entry = {'kind': 'data', 'path': str(self.result),
                      'sha256': hashlib.sha256(self.result.read_bytes()).hexdigest(),
                      'time_field': ['finished'], 'facts': {'runs': ['runs']}}
        self.story = {'takeaway': 'The same check ran twice.',
                      'nodes': [{'id': 'run', 'label': 'Run checks', 'evidence': ['E1']},
                                {'id': 'review', 'label': 'Compare', 'evidence': ['E1']}],
                      'links': [['run', 'review']], 'remaining_work': None, 'slides': [
                          {'title': 'Gather the evidence', 'point': 'The checks ran twice.', 'focus': 'run',
                           'subtitle': 'The same input reached both containers.', 'evidence': ['E1'],
                           'number': {'id': 'E1', 'field': 'runs', 'label': 'Independent runs'}},
                          {'title': 'Review it together', 'point': 'Keep the location of the receipt.', 'focus': 'review',
                           'subtitle': 'One receipt links to both outcomes.', 'evidence': ['E1']},
                      ]}

    def collect(self, evidence=None, pending=None):
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'evidence': [self.entry] if evidence is None else evidence,
                                        'pending': pending or []}))
        return ketchup_video.collect(repo=self.repo, transcript=self.session, invocation_line=5,
                                     out=self.root / 'collected.json', manifest=manifest)

    def plan(self, evidence=None):
        data = evidence or self.collect()
        storyboard = self.root / 'story.json'
        storyboard.write_text(json.dumps(self.story))
        return ketchup_video.plan(evidence=data['path'], sha256=data['sha256'],
                                  out=self.root / 'plan.json', storyboard=storyboard)

    def test_window_ignores_tool_results_and_an_earlier_summary_invocation(self):
        result = self.collect()
        packet = json.loads(Path(result['path']).read_text())
        self.assertEqual(packet['window']['last_user_line'], 1)
        self.assertEqual([turn['line'] for turn in packet['window']['later_turn_context']], [2, 4])
        self.assertEqual(packet['evidence']['E1']['facts'], {'runs': 2})

    def test_evidence_timestamp_must_fit_window_except_waiting_decisions(self):
        old = dict(self.entry)
        old.pop('time_field')
        old['at'] = (START - timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.collect(evidence=[old])
        decision = {'question': 'Which run should be retained?', 'options': [
            {'name': 'Keep it', 'gain': 'Retain the evidence.', 'cost': 'Keep one local file.'}],
            'recommendation': 'Keep it', 'evidence': ['E1']}
        packet = self.collect(evidence=[{**old, 'earlier_for_decision': True}], pending=[decision])
        result = self.plan(packet)
        self.assertEqual(result['status'], 'no-new-evidence')
        self.assertEqual(json.loads(Path(result['path']).read_text())['action_items'], [decision])

    def test_rejects_a_made_up_number_in_the_caption(self):
        self.story['slides'][0]['subtitle'] = 'The check ran 3 times.'
        with self.assertRaisesRegex(ValueError, 'lacks an exact cited fact'):
            self.plan()

    def test_planning_refuses_to_write_inside_the_project(self):
        evidence = self.collect()
        with self.assertRaisesRegex(ValueError, 'outside the project'):
            ketchup_video.plan(evidence=evidence['path'], sha256=evidence['sha256'],
                               out=self.repo / 'plan.json', model='gpt-test')

    def test_timing_counts_new_labels_and_caption_without_recounting_diagram(self):
        packet = {'E1': {'facts': {'runs': 2}}}
        slots = ketchup_render.schedule(self.story, packet)
        self.assertLess(slots[1]['words_added'], slots[0]['words_added'])
        self.assertEqual(slots[1]['start_frame'], slots[0]['frames'] + ketchup_render.TRANSITION)
        for slot in slots:
            self.assertGreaterEqual(slot['frames'] * 3, slot['words_added'] * ketchup_render.FPS)

    def test_render_failure_retains_the_exact_waiting_decisions(self):
        waiting = [{'question': 'Publish the checked text?', 'options': [
            {'name': 'Keep local', 'gain': 'Inspect it first.', 'cost': 'Publication waits.'}],
            'recommendation': 'Keep local', 'evidence': ['E1']}]
        plan = self.plan(self.collect(pending=waiting))
        with patch.object(ketchup_render, 'render', side_effect=ValueError('subtitle overflow')):
            output = ketchup_video.render(plan=plan['path'], sha256=plan['sha256'], out=self.root / 'render')
        self.assertEqual(output['status'], 'render-failed')
        reply = json.loads(Path(output['path']).read_text())
        self.assertEqual(reply['action_items'], waiting)
        self.assertNotIn('video', reply)

    @unittest.skipUnless(os.environ.get('WORKBENCH_VIDEO_TEST') == '1', 'requires local Chromium and FFmpeg')
    def test_real_video_matches_each_slide_and_detects_modified_movie(self):
        plan = self.plan()
        receipt = ketchup_video.render(plan=plan['path'], sha256=plan['sha256'], out=self.root / 'render')
        self.assertEqual(receipt['status'], 'verified', receipt)
        verified = ketchup_video.verify(receipt=receipt['path'], sha256=receipt['sha256'])
        self.assertEqual(verified['audit']['audio_streams'], 0)
        self.assertEqual(verified['audit']['transitions'], 1)
        self.assertTrue(all(item['matching_frames'] == item['frames'] for item in verified['audit']['holds']))
        with Path(verified['video']['path']).open('ab') as video:
            video.write(b'extra bytes')
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            ketchup_video.verify(receipt=receipt['path'], sha256=receipt['sha256'])
