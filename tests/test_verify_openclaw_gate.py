import json
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

    def reports(self, *, same_workspace=False, fail=False, report_head=None, skills_image=None,
                skills_inspect_image=None, config_image=None, rename=False, browser_receipt=True,
                same_container=False):
        refs = {}
        self.data['evidence'] = []
        gateway_image = 'sha256:' + 'f' * 64
        journeys = {'gateway-health': gateway_image}
        if skills_image is not None:
            journeys['skills-check'] = skills_image
        if skills_inspect_image is not None:
            journeys['skills-inspect'] = skills_inspect_image
        if config_image is not None:
            journeys['config-validate'] = config_image
        if rename:
            journeys['control-ui-session-rename'] = gateway_image
        for journey, image in journeys.items():
            helper, marker, command = verify_openclaw._journey(journey)
            for actor in ('builder', 'independent-verifier'):
                name = f'{journey}-{actor}'
                path = self.root / f'{name}.json'
                extra = {'stdout': marker + '\n'}
                if journey == 'control-ui-session-rename':
                    png = path.with_suffix('.png')
                    png.write_bytes(name.encode())
                    extra = {'stdout': self.browser_stdout(journey, fresh=browser_receipt),
                             'screenshot_path': str(png.resolve()), 'screenshot_sha256': cli.digest(png),
                             'create_stdout': ('a' if same_container or actor == 'builder' else 'b') * 64}
                cli.write(path, {
                    'journey': journey, 'actor': actor, 'commit': report_head or self.head,
                    'image': image, 'verified_image_id': image,
                    'helper_sha256': cli.digest(verify_openclaw.SKILL / helper),
                    'passed': not fail, 'exit_code': int(fail), 'cleanup_confirmed': True, 'timed_out': False,
                    'container_created': True, 'source_root': '/app', 'command': command,
                    'workspace_read_only': True, 'workspace': 'shared' if same_workspace else name,
                    **extra,
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

    def browser_stdout(self, journey, *, fresh=True):
        events = [
            {'phase': 'doctor', 'ready': True, 'live': True},
            {'phase': 'ui', 'route': '/sessions', 'servedCommit': self.head, 'screenshot': True,
             'handshake': {'protocol': 4, 'method': 'token', 'role': 'operator'},
             'readback': {'method': 'sessions.list', 'count': int(journey == 'control-ui-session-rename')}},
            {'phase': 'cleanup', 'exit': {'code': 0, 'signal': None}, 'scratchRemoved': True},
        ]
        if journey == 'control-ui-session-rename':
            events[1]['mutation'] = {'method': 'sessions.patch', 'key': 'agent:main:iboard-browser-verification',
                                     'sessionId': 'fixture-session-id', 'originalLabel': 'iBoard verification fixture',
                                     'finalLabel': 'iBoard verification persisted', 'canceledWithoutWrite': True,
                                     'accepted': True, 'freshContextReadback': fresh}
        return '\n'.join([*(json.dumps(event) for event in events), verify_openclaw._journey(journey)[1], ''])

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

    def test_skills_change_requires_all_journeys_reviewed_on_one_image(self):
        self.changed_paths.return_value = 'src/skills/discovery/status.ts\0'
        for skills_image in (None, 'sha256:' + 'e' * 64):
            with self.subTest(skills_image=skills_image):
                self.reports(skills_image=skills_image)
                with self.assertRaisesRegex(verify_openclaw.GatewayGateError, 'skills-check'):
                    verify_openclaw.require(self.data, self.head, self.destination)
        image = 'sha256:' + 'f' * 64
        self.reports(skills_image=image)
        with self.assertRaisesRegex(verify_openclaw.GatewayGateError, 'skills-inspect'):
            verify_openclaw.require(self.data, self.head, self.destination)
        self.reports(skills_image=image, skills_inspect_image=image)
        self.data.update(kind='feature', delivery_repo=self.destination)
        self.data['evidence'].append({'kind': 'test-result', 'commit': self.head,
                                     'actor': 'independent-verifier', 'sha256': '0' * 64})
        with patch.object(publish, '_feature_doc_link', return_value='https://example.com/doc'), \
                patch.object(publish, '_report_summary', return_value=('test passed', '')):
            body = publish._pr_body(self.data, self.head)
        for journey in ('gateway-health', 'skills-check', 'skills-inspect'):
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

    def test_connected_ui_requires_a_bound_screenshot_in_its_own_output(self):
        journey = 'control-ui-connected'
        helper, _ = verify_openclaw.BROWSER_JOURNEYS[journey]
        image = 'sha256:' + 'f' * 64
        screenshot = self.root / 'connected.png'
        screenshot.write_bytes(b'verified capture')
        options = {'timeout': 90, 'memory_mb': 3328}
        with self.assertRaisesRegex(ValueError, 'requires a screenshot path'):
            verify_openclaw.verify(self.root, image, journey, **options)
        sandbox = {'exit_code': 0, 'cleanup_confirmed': True, 'stdout': self.browser_stdout(journey)}
        replies = iter((sandbox, {**sandbox, 'screenshot_sha256': cli.digest(screenshot),
                                  'screenshot_path': str(screenshot.resolve())}))

        def execute(scratch, *args, **kwargs):
            self.assertEqual((Path(scratch) / 'expected-commit.txt').read_text(), self.head + '\n')
            return next(replies)

        with patch.object(cli, 'sha', return_value=self.head), \
                patch.object(verify_openclaw.workflow, '_verified_image', return_value=image), \
                patch.object(verify_openclaw, 'run_sandbox', side_effect=execute) as run:
            self.assertFalse(verify_openclaw.verify(self.root, image, journey, screenshot=screenshot, **options)['passed'])
            result = verify_openclaw.verify(self.root, image, journey, screenshot=screenshot, **options)
        self.assertTrue(result['passed'])
        self.assertEqual(result['helper_sha256'], cli.digest(verify_openclaw.SKILL / helper))
        self.assertEqual(run.call_args.args[1:3], (image, ['node', f'/workspace/{helper}']))
        self.assertEqual(run.call_args.kwargs['screenshot_output'], screenshot)
        self.assertEqual(run.call_args.kwargs['pids_limit'], 384)
        self.assertNotIn(journey, verify_openclaw.required_journeys(self.data, self.head))

    def test_sessions_ui_delivery_rechecks_browser_receipts_before_writing(self):
        self.changed_paths.return_value = 'ui/src/pages/sessions/page-dialog.ts\0'
        github = Mock()
        for options in ({}, {'rename': True, 'browser_receipt': False}, {'rename': True, 'same_container': True}):
            with self.subTest(options=options):
                self.reports(**options)
                with patch.object(cli, 'read', return_value=self.data), \
                        patch.object(cli, 'gate', return_value=(self.head, [])), \
                        patch.object(publish.delivery, '_independent_verification'):
                    with self.assertRaisesRegex(publish.PublishError, 'control-ui-session-rename'):
                        publish.publish.__wrapped__(self.root / 'task.json', self.destination, github=github)
        self.assertEqual(github.mock_calls, [])
        self.reports(rename=True)
        receipts = verify_openclaw.require(self.data, self.head, self.destination)
        _, verifier = receipts['control-ui-session-rename']
        self.data.update(kind='feature', delivery_repo=self.destination)
        self.data['evidence'].append({'kind': 'test-result', 'commit': self.head,
                                     'actor': 'independent-verifier', 'sha256': '0' * 64})
        with patch.object(publish, '_feature_doc_link', return_value='https://example.com/doc'), \
                patch.object(publish, '_report_summary', return_value=('test passed', '')):
            self.assertIn(verifier['screenshot_sha256'], publish._pr_body(self.data, self.head))
        (self.root / 'control-ui-session-rename-independent-verifier.png').write_bytes(b'exchanged image')
        with self.assertRaisesRegex(verify_openclaw.GatewayGateError, 'control-ui-session-rename'):
            verify_openclaw.require(self.data, self.head, self.destination)

    def test_collect_keeps_failed_browser_run_for_task_recovery(self):
        self.changed_paths.return_value = 'ui/src/lib/session-rename.ts\0'
        calls = []

        def execute(repo, image, journey, **options):
            calls.append((journey, options))
            return {'commit': self.head, 'passed': len(calls) < 4, 'timed_out': len(calls) == 4,
                    'cleanup_confirmed': True, 'stdout': ''}

        task = self.root / 'task.json'
        with patch.object(verify_openclaw, 'verify', side_effect=execute):
            with self.assertRaisesRegex(verify_openclaw.workflow.WorkflowError, 'raw report saved at') as blocked:
                verify_openclaw.collect(task, self.data, self.head, 'image', ('builder', 'independent-verifier'),
                                        remaining=lambda: 90, memory_mb=3328)
        paths = [options['screenshot'] for journey, options in calls if journey == 'control-ui-session-rename']
        self.assertEqual(len(paths), 2)
        self.assertNotEqual(*paths)
        self.assertTrue(all(path.with_suffix('.json').is_file() for path in paths))
        self.assertIn(str(paths[1].with_suffix('.json')), str(blocked.exception))
