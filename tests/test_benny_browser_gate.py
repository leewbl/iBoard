import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import test_benny_reproduce
from test_browser_recording import frame
from workbench import benny_repair, browser_recording, cli, delivery


BASELINE_VISIBLE = 'The final sidebar row shows the previous name after saving.'
CANDIDATE_VISIBLE = 'The final sidebar row shows the replacement name after saving.'


class BennyBrowserGateTests(unittest.TestCase):
    def setUp(self):
        fixture = test_benny_reproduce.BennyReproductionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        self.runtime = fixture.runtime
        self.reviewed = []
        self.uncertain_at = None
        self.browser_manifest(recording=True)

    def browser_manifest(self, *, recording):
        manifest = json.loads(self.runtime.manifest.read_text())
        manifest.update(project='openclaw', surface='Control UI')
        manifest['features'][0].update(browser=True, screenshot=True, recording=recording)
        self.runtime.manifest.write_text(json.dumps(manifest))

    def sandbox(self, workspace, image, argv, timeout, **settings):
        screenshot = settings.get('screenshot_output')
        if screenshot is None:
            return self.runtime.sandbox(workspace, image, argv, timeout, **settings)
        script = Path(argv[-1]).name
        workspace = Path(workspace)
        revision = (workspace/'expected-commit.txt').read_text().strip()
        self.assertEqual({path.name for path in workspace.iterdir()}, {script, 'expected-commit.txt'})
        with TemporaryDirectory(dir=self.fixture.root) as directory:
            (Path(directory)/script).write_bytes((workspace/script).read_bytes())
            run = self.runtime.sandbox(directory, image, argv, timeout, **settings)
        self.assertTrue(settings['capture_failure_screenshot'])
        screenshot.write_bytes(b'\x89PNG\r\n\x1a\n' + revision.encode())
        run.update(screenshot_path=str(screenshot), screenshot_sha256=cli.digest(screenshot))
        event = {'phase': 'ui', 'servedCommit': revision, 'screenshot': True}
        recording = settings.get('recording_output')
        if recording is not None:
            journey = 'fixture-save-name'
            rows = [{'schema': 1, 'producer': 'chromium-cdp', 'served_commit': revision, 'journey': journey}]
            for i in range(3):
                rows.append({'ms': i * 500 + 8, 'page': 1 + i // 2,
                             'jpeg': base64.b64encode(frame(i)).decode()})
            rows.append({'end': 3})
            recording.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            recording_digest = cli.digest(recording)
            run.update(recording_path=str(recording), recording_sha256=recording_digest)
            event.update(recording=True, recordingJourney=journey, recordingSha256=recording_digest)
        run['stdout'] += json.dumps(event) + '\n'
        return run

    def review(self, report, sha256, condition, output, *, timeout):
        report, output = Path(report), Path(output)
        self.reviewed.append((report, condition, output))

        def inspect(images, prompt, allowed_seconds):
            self.assertEqual(len(images), 4)
            self.assertIn(condition, prompt)
            self.assertEqual(timeout, allowed_seconds)
            if len(self.reviewed) == self.uncertain_at:
                return json.dumps({'verdict': 'uncertain', 'reason': 'The text is clipped.', 'references': ['FINAL']})
            return json.dumps({'verdict': 'visible', 'reason': 'The full text is visible.',
                               'references': ['F2', 'FINAL']})

        return browser_recording.review(report, sha256, condition, output, timeout=timeout, inspect=inspect)

    def reproduce(self, **overrides):
        options = dict(baseline_visible=BASELINE_VISIBLE, candidate_visible=CANDIDATE_VISIBLE,
                       sandbox=self.sandbox, review=self.review)
        return self.fixture.reproduce(**(options | overrides))

    def verify_candidate(self):
        def derive(repo, base, head, image, timeout, *, rebuild_openclaw_ui):
            self.assertTrue(rebuild_openclaw_ui)
            return self.runtime.derive(repo, base, head, image, timeout)

        return self.fixture.after(sandbox=self.sandbox, derive=derive, review=self.review)

    def candidate_reference(self):
        path = self.fixture.root/'after/result.json'
        return {'path': str(path), 'sha256': cli.digest(path)}

    def proof(self):
        reproduction = self.fixture.receipt()
        return benny_repair.candidate_proof(
            {'path': str(reproduction['receipt']), 'sha256': reproduction['sha256']},
            self.candidate_reference(),
            self.runtime.revision)

    def visual_capture(self):
        self.assertEqual(self.reproduce()['status'], 'reproduced')
        task = Path(self.fixture.prepare()['task'])
        self.assertEqual(self.verify_candidate()['status'], 'verified')
        data = cli.read(task)
        head = self.runtime.revision
        cli.git(data['repo'], 'merge', '--ff-only', head)
        actor = 'independent'
        capture, observations, script = benny_repair.record_visual_capture(
            task, data, self.candidate_reference(), head, actor)
        self.assertEqual(data, cli.read(task))
        self.assertEqual([row['condition'] for row in observations], [CANDIDATE_VISIBLE] * 2)
        self.assertEqual(script, (self.runtime.helpers/'main.sh').read_bytes())
        report = Path(capture['report'])
        review = task.with_name('independent.json')
        cli.write(review, {'schema': cli.SCHEMA, 'screenshot_hashes': [cli.digest(capture['screenshot'])],
                           'assessments': [{'evidence': ['SCREENSHOT']}],
                           'reports': {'SCREENSHOT': {'path': str(report), 'sha256': cli.digest(report)}}})
        return data, report, {'path': str(review), 'actor': actor}

    def test_visual_before_and_after_confirm_both_attempts_using_frozen_conditions(self):
        self.assertEqual(self.reproduce()['status'], 'reproduced')
        prepared = self.fixture.prepare()
        self.assertEqual(cli.read(Path(prepared['task']))['surface'], 'ui')
        self.assertEqual(self.verify_candidate()['status'], 'verified')
        self.assertEqual(self.proof()['head'], self.runtime.revision)
        self.assertEqual([(report.name, condition) for report, condition, _ in self.reviewed],
                         [('main.primary-1.json', BASELINE_VISIBLE), ('main.primary-2.json', BASELINE_VISIBLE),
                          ('main.primary-1.json', CANDIDATE_VISIBLE), ('main.primary-2.json', CANDIDATE_VISIBLE)])
        verdicts = [cli.read(path) for _, _, path in self.reviewed]
        self.assertEqual([item['served_commit'] for item in verdicts],
                         [self.fixture.base] * 2 + [self.runtime.revision] * 2)
        self.assertTrue(all(item['verdict'] == 'visible' for item in verdicts))
        self.assertEqual(len({item['report_sha256'] for item in verdicts}), 4)

    def test_visual_reproduction_requires_two_distinct_conditions(self):
        for baseline, candidate in ((None, CANDIDATE_VISIBLE), ('  ', CANDIDATE_VISIBLE),
                                    (BASELINE_VISIBLE, BASELINE_VISIBLE)):
            with self.subTest(baseline=baseline, candidate=candidate):
                with self.assertRaises(ValueError):
                    self.reproduce(baseline_visible=baseline, candidate_visible=candidate)
        self.assertEqual(self.reviewed, [])
        self.assertEqual(self.runtime.calls, [])

    def test_uncertain_baseline_review_blocks_preparation(self):
        self.uncertain_at = 1
        self.assertNotEqual(self.reproduce()['status'], 'reproduced')
        self.assertEqual(cli.read(self.reviewed[0][2])['verdict'], 'uncertain')
        with self.assertRaises(ValueError):
            self.fixture.prepare()

    def test_uncertain_candidate_review_blocks_delivery(self):
        self.assertEqual(self.reproduce()['status'], 'reproduced')
        self.uncertain_at = 4
        self.assertNotEqual(self.verify_candidate()['status'], 'verified')
        self.assertEqual(cli.read(self.reviewed[-1][2])['verdict'], 'uncertain')
        with self.assertRaises(ValueError):
            self.proof()

    def test_changed_review_or_browser_artifact_blocks_candidate_proof(self):
        self.assertEqual(self.reproduce()['status'], 'reproduced')
        self.assertEqual(self.verify_candidate()['status'], 'verified')
        report, _, review = self.reviewed[-1]
        run = cli.read(report)
        for path in (review, Path(run['recording_path']), Path(run['screenshot_path'])):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b'\n')
                with self.assertRaises(ValueError):
                    self.proof()
                path.write_bytes(original)
        self.assertEqual(self.proof()['status'], 'verified')

    def test_normalized_capture_preserves_original_run_and_passes_generic_delivery_check(self):
        data, report, independent = self.visual_capture()
        evidence = data['evidence'][-1]
        capture = cli.read(report)
        raw = self.reviewed[-1][0]
        self.assertEqual(capture['original_execution'], {'path': str(raw), 'sha256': cli.digest(raw)})
        self.assertEqual(evidence['path'], cli.read(raw)['screenshot_path'])
        self.assertTrue(delivery._valid_screenshot(evidence, independent['actor'], self.runtime.revision, data['repo']))
        benny_repair.require_visual_capture(data, self.candidate_reference(), self.runtime.revision, independent)

        review = Path(independent['path'])
        result = cli.read(review)
        result['assessments'][0]['evidence'] = []
        cli.write(review, result)
        with self.assertRaisesRegex(ValueError, 'must cite'):
            benny_repair.require_visual_capture(data, self.candidate_reference(), self.runtime.revision, independent)

    def test_visual_capture_gate_recomputes_normalized_and_raw_evidence(self):
        data, report, independent = self.visual_capture()
        readback = cli.read(report)
        review = Path(independent['path'])
        accepted = cli.read(review)
        forged = {**readback, 'original_execution': {**readback['original_execution'], 'sha256': '0' * 64}}
        cli.write(report, forged)
        digest = cli.digest(report)
        data['evidence'][-1]['execution_report_sha256'] = digest
        accepted['reports']['SCREENSHOT']['sha256'] = digest
        cli.write(review, accepted)
        self.assertTrue(delivery._valid_screenshot(data['evidence'][-1], independent['actor'],
                                                   self.runtime.revision, data['repo']))
        with self.assertRaisesRegex(ValueError, 'must cite'):
            benny_repair.require_visual_capture(data, self.candidate_reference(), self.runtime.revision, independent)

        cli.write(report, readback)
        digest = cli.digest(report)
        data['evidence'][-1]['execution_report_sha256'] = digest
        accepted['reports']['SCREENSHOT']['sha256'] = digest
        cli.write(review, accepted)
        raw = Path(readback['original_execution']['path'])
        raw.write_bytes(raw.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'frozen sandbox receipt'):
            benny_repair.require_visual_capture(data, self.candidate_reference(), self.runtime.revision, independent)

    def test_old_screenshot_only_reproduction_cannot_prepare_browser_repair(self):
        self.browser_manifest(recording=False)
        result = self.fixture.reproduce(sandbox=self.sandbox)
        self.assertEqual(result['status'], 'reproduced')
        with self.assertRaises(ValueError):
            self.fixture.prepare()
