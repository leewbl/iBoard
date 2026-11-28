import base64
import json
from pathlib import Path
import tempfile
import unittest

from workbench import cli, multiphase, prototype


IMAGE = 'sha256:' + 'a' * 64
PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGMQ0gj4DwACZAGKmz8bNgAAAABJRU5ErkJggg=='
)


class PrototypeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')
        cli.git(self.repo, 'config', 'user.name', 'Fixture')
        cli.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'task.py').write_text('def pick():\n    return "previous"\n')
        cli.git(self.repo, 'add', '.')
        cli.git(self.repo, 'commit', '-qm', 'baseline')
        self.baseline = cli.sha(self.repo, 'HEAD')
        self.question = 'Which prototype retains the last verified parent?'
        self.spec = {'repo': str(self.repo), 'revision': self.baseline, 'image': 'fixture:local',
                     'question': self.question, 'criteria': 'Choose the verified parent.',
                     'surface': 'behavior', 'entry': 'driver.mjs', 'sources': ['task.py:1-2'],
                     'variants': [{'id': 'early', 'label': 'Early', 'approach': 'Record before checks.'},
                                  {'id': 'late', 'label': 'Late', 'approach': 'Record after checks.'}]}
        self.manifest = self.root / 'manifest.json'
        self.wrong_variant = False
        self.image_review = None
        self.sandbox_calls = []
        self.review_calls = 0

    def sandbox(self, workspace, image, command, timeout, **options):
        snapshot = options['screenshot_output']
        self.assertTrue(options['workspace_read_only'])
        self.assertEqual(image, IMAGE)
        variant = command[2]
        self.sandbox_calls.append(variant)
        self.assertTrue((Path(workspace) / self.spec['entry']).exists())
        packet = {'kind': 'iboard-prototype', 'variant': 'wrong' if self.wrong_variant else variant,
                  'surface': self.spec['surface'], 'observation': {'retained': variant == 'late'}}
        raw = {'image': image, 'command': command, 'stdout': json.dumps(packet) + '\n',
               'exit_code': 0, 'container_created': True, 'cleanup_confirmed': True, 'workspace_read_only': True}
        if snapshot:
            snapshot.write_bytes(PNG)
            raw['screenshot_sha256'] = cli.digest(snapshot)
        return raw

    def reviewer(self, prompt, model, remaining, **kwargs):
        if prompt.startswith('Draft'):
            return {'content': 'prototype data'}
        self.review_calls += 1
        self.image_review = kwargs.get('images')
        return {'choice': 'late', 'conclusion': 'Record after verification to retain the verified parent.',
                'tradeoffs': [{'variant': 'early', 'evidence': 'early', 'tradeoff': 'Can promote an unverified candidate.'},
                              {'variant': 'late', 'evidence': 'late', 'tradeoff': 'Recovery sees the verified parent.'}]}

    def run_prototype(self):
        self.manifest.write_text(json.dumps(self.spec))
        return prototype.run(self.manifest, self.root / 'run', 'gpt-fixture',
                             reviewer=self.reviewer, sandbox=self.sandbox,
                             resolve_image=lambda name, remaining: IMAGE)

    def handoff(self, result, folder='plan'):
        path = self.root / 'run/result.json'
        fields = {
            'repo': str(self.repo), 'revision': self.baseline, 'request': 'Plan the recovery transition.',
            'files': ['task.py:1-2'], 'scope': ['task.py'], 'obvious': False,
            'open_questions': [self.question],
            'prototypes': [{'kind': 'native', 'question': self.question, 'evidence': str(path),
                            'sha256': cli.digest(path), 'conclusion': result['recommendation']['conclusion']}],
            'plan': {'summary': 'Bounded single transition.', 'phases': [
                {'id': 'P1', 'base': 'trunk', 'depends_on': [], 'files': ['task.py'],
                 'change': 'Keep accepted parent until verification.', 'observed_result': 'Stable recovery.',
                 'interaction': False, 'citations': [{'id': 'S1', 'line': 2}],
                 'verify': {'unit': {'scenario': 'Use a controlled parent.', 'predicate': 'Parent retained.',
                                     'marker': 'UNIT_PASS'},
                            'live': {'scenario': 'Interrupt the real command.', 'predicate': 'Recover prior parent.',
                                     'marker': 'LIVE_PASS'},
                            'perf': {'probe': 'Fixed replay.', 'metric': 'median_ms', 'max_ms': 100}}}
            ]}
        }
        path = self.root / 'handoff.json'
        path.write_text(json.dumps(fields))
        return multiphase.plan(path, self.root / folder)

    def test_recommendation_and_handoff_require_both_observed_variants(self):
        result = self.run_prototype()
        self.assertEqual(result['status'], 'recommended')
        self.assertEqual(self.sandbox_calls, ['early', 'late'])
        self.assertEqual(result['evidence'][0]['observation'], '{"retained": false}')
        self.assertEqual(cli.git(self.repo, 'status', '--porcelain'), '')
        handoff = self.handoff(result)
        self.assertEqual(handoff['status'], 'planned')
        self.assertEqual(handoff['resolved_by_prototype'], [self.question])

    def test_changed_raw_evidence_cannot_close_a_question(self):
        result = self.run_prototype()
        (self.root / 'run/early.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'recorded digest'):
            self.handoff(result)

    def test_wrong_runtime_variant_stops_before_review(self):
        self.wrong_variant = True
        result = self.run_prototype()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.review_calls, 0)

    def test_visual_review_uses_the_captured_images_in_variant_order(self):
        self.spec.update(surface='visual', entry='prototype.html')
        result = self.run_prototype()
        self.assertEqual(result['status'], 'recommended')
        self.assertEqual([image.name for image in self.image_review], ['early.png', 'late.png'])
        for item, image in zip(result['evidence'], self.image_review):
            self.assertEqual(cli.digest(image), item['screenshot_sha256'])
        self.assertTrue(prototype.validate_receipt(self.root / 'run/result.json', result, baseline=self.baseline,
                       question=self.question, conclusion=result['recommendation']['conclusion']))
