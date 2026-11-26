from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workbench import cli, publish, verify_openclaw


class GatewayGateTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.head = 'a' * 40
        self.data = {'id': 'pilot', 'repo': str(self.root), 'base_commit': 'b' * 40,
                     'implementer': 'builder', 'evidence': [], 'criteria': [{'id': 'x', 'text': 'x'}]}
        self.destination = 'owner/myopenclaw'
        changed_paths = patch.object(cli, 'git', return_value='src/gateway/server.ts\0')
        self.changed_paths = changed_paths.start()
        self.addCleanup(changed_paths.stop)

    def reports(self, *, same_workspace=False, fail=False, report_head=None, skills_image=None, config_image=None):
        refs = {}
        self.data['evidence'] = []
        gateway_image = 'sha256:' + 'f' * 64
        journeys = {'gateway-health': gateway_image}
        if skills_image is not None:
            journeys['skills-check'] = skills_image
        if config_image is not None:
            journeys['config-validate'] = config_image
        for journey, image in journeys.items():
            helper, marker = verify_openclaw.JOURNEYS[journey]
            for actor in ('builder', 'independent-verifier'):
                name = f'{journey}-{actor}'
                path = self.root / f'{name}.json'
                cli.write(path, {
                    'journey': journey, 'actor': actor, 'commit': report_head or self.head,
                    'image': image, 'verified_image_id': image,
                    'helper_sha256': cli.digest(verify_openclaw.SKILL / helper),
                    'passed': not fail, 'exit_code': int(fail), 'cleanup_confirmed': True, 'timed_out': False,
                    'container_created': True, 'source_root': '/app', 'command': ['node', f'/workspace/{helper}'],
                    'workspace_read_only': True, 'workspace': 'shared' if same_workspace else name,
                    'stdout': marker + '\n',
                })
                refs[name] = {'path': str(path), 'sha256': cli.digest(path)}
                self.data['evidence'].append({'kind': 'runtime-log', 'commit': self.head,
                                              'actor': actor, **refs[name]})
        path = self.root / 'review.json'
        cli.write(path, {'verdict': 'PASS', 'candidate_commit': self.head, 'reports': refs,
                         'baseline_commit': self.data['base_commit'], 'criterion_digest': cli.criteria_digest(self.data)})
        self.data['evidence'].append({
            'kind': 'independent-review', 'actor': 'independent-verifier', 'commit': self.head,
            'criteria_digest': cli.criteria_digest(self.data), 'path': str(path), 'sha256': cli.digest(path),
        })

    def test_publish_and_republish_block_before_any_github_call(self):
        github = Mock()
        for kwargs in ({'fail': True}, {'same_workspace': True}, {'report_head': 'b' * 40}, None):
            if kwargs is None:
                self.data['evidence'] = []
            else:
                self.reports(**kwargs)
            with patch.object(cli, 'read', return_value=self.data), patch.object(cli, 'gate', return_value=(self.head, [])), patch.object(publish.delivery, '_independent_verification'):
                # Test the unlocked implementations without an on-disk task lock.
                for call, extra in ((publish.publish, ()), (publish.republish, (1, 'b' * 40))):
                    with self.assertRaisesRegex(publish.PublishError, 'two passing'):
                        call.__wrapped__(self.root / 'task.json', self.destination, *extra, github=github)
        self.assertEqual(github.mock_calls, [])

    def test_passes_matching_pair_and_blocks_modified_evidence(self):
        self.reports()
        verify_openclaw.require(self.data, self.head, self.destination)
        (self.root / 'gateway-health-builder.json').write_text('{}')
        with self.assertRaises(verify_openclaw.GatewayGateError):
            verify_openclaw.require(self.data, self.head, self.destination)

    def test_skills_change_requires_both_journeys_reviewed_on_one_image(self):
        self.changed_paths.return_value = 'src/skills/discovery/status.ts\0'
        for skills_image in (None, 'sha256:' + 'e' * 64):
            with self.subTest(skills_image=skills_image):
                self.reports(skills_image=skills_image)
                with self.assertRaisesRegex(verify_openclaw.GatewayGateError, 'skills-check'):
                    verify_openclaw.require(self.data, self.head, self.destination)
        self.reports(skills_image='sha256:' + 'f' * 64)
        self.data.update(kind='feature', delivery_repo=self.destination)
        self.data['evidence'].append({'kind': 'test-result', 'commit': self.head,
                                     'actor': 'independent-verifier', 'sha256': '0' * 64})
        with patch.object(publish, '_feature_doc_link', return_value='https://example.com/doc'), \
                patch.object(publish, '_report_summary', return_value=('test passed', '')):
            body = publish._pr_body(self.data, self.head)
        for journey in ('gateway-health', 'skills-check'):
            for actor in ('builder', 'independent-verifier'):
                digest = cli.digest(self.root / f'{journey}-{actor}.json')
                self.assertIn(digest, body)

    def test_map_audit_stops_when_a_registered_journey_is_missing_from_index(self):
        features = self.root / 'features'
        features.mkdir()
        (features / 'README.md').write_text('| [gateway-health](gateway-health.md) | Health |')
        for name in verify_openclaw.JOURNEYS:
            (features / f'{name}.md').write_text(name)
        with patch.object(verify_openclaw, 'SKILL', self.root), patch.object(verify_openclaw, 'verify') as run:
            report = verify_openclaw.audit(self.root, 'local', timeout=30, memory_mb=512)
        self.assertEqual(report['status'], 'map-drift')
        self.assertFalse(report['passed'])
        self.assertIn('skills-check', report['registered'])
        run.assert_not_called()

    def test_configuration_changes_need_config_receipts_on_the_shared_image(self):
        self.changed_paths.return_value = 'src/config/zod-schema.gateway.ts\0'
        for image in (None, 'sha256:' + 'e' * 64):
            self.reports(config_image=image)
            with self.assertRaisesRegex(verify_openclaw.GatewayGateError, 'config-validate'):
                verify_openclaw.require(self.data, self.head, self.destination)
        self.reports(config_image='sha256:' + 'f' * 64)
        receipts = verify_openclaw.require(self.data, self.head, self.destination)
        self.assertEqual(set(receipts), {'gateway-health', 'config-validate'})
