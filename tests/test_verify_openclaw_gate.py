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
        self.data = {'id': 'pilot', 'base_commit': 'b' * 40, 'implementer': 'builder', 'evidence': [], 'criteria': [{'id': 'x', 'text': 'x'}]}
        self.destination = 'owner/myopenclaw'

    def reports(self, *, same_workspace=False, fail=False, report_head=None):
        refs = {}
        self.data['evidence'] = []
        for actor in ('builder', 'independent-verifier'):
            path = self.root / (actor + '.json')
            image = 'sha256:' + 'f' * 64
            cli.write(path, {
                'journey': 'gateway-health', 'actor': actor, 'commit': report_head or self.head,
                'image': image, 'verified_image_id': image,
                'helper_sha256': cli.digest(verify_openclaw.SKILL / 'gateway.mjs'),
                'passed': not fail, 'exit_code': int(fail), 'cleanup_confirmed': True, 'timed_out': False,
                'container_created': True, 'source_root': '/app', 'command': ['node', '/workspace/gateway.mjs'],
                'workspace_read_only': True, 'workspace': 'shared' if same_workspace else actor,
                'stdout': 'VERIFY_OPENCLAW_GATEWAY_HEALTH_PASS\n',
            })
            refs[actor] = {'path': str(path), 'sha256': cli.digest(path)}
            self.data['evidence'].append({'kind': 'runtime-log', 'commit': self.head, 'actor': actor, **refs[actor]})
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
        (self.root / 'builder.json').write_text('{}')
        with self.assertRaises(verify_openclaw.GatewayGateError):
            verify_openclaw.require(self.data, self.head, self.destination)
