import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from workbench import cli, delivery, publish, local_gate
import test_publish as publish_fixtures
import test_local_gate as gate_fixtures


class NoRemoteActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.task = Path(self.tmp.name) / 'task.json'
        self.task.touch()
        self.record = {'id': 'case', 'repo': '/tmp/work', 'base_commit': 'base',
                       'local_gate': {'mode': 'explicit'}}
        self.fake = publish_fixtures.FakeGitHub()
        self.fake.actions_permissions = lambda repo: {'enabled': False}
        for target, kwargs in [
            ('workbench.publish.cli.read', {'return_value': self.record}),
            ('workbench.publish.cli.gate', {'return_value': ('head', [])}),
            ('workbench.publish.delivery._independent_verification', {}),
            ('workbench.publish._pr_body', {'return_value': 'verified body'}),
            ('workbench.local_gate.validate', {})]:
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_disabled_actions_allows_existing_evidence_guarded_publication(self):
        self.assertEqual(publish.publish(self.task, 'alice/fork', self.fake)['pr'], 7)
        self.assertEqual([e[0] for e in self.fake.events], ['push', 'create'])

    def test_enabled_unknown_or_unavailable_actions_blocks_before_push(self):
        for response in ({'enabled': True}, {}, None, {'enabled': 0}):
            with self.subTest(response=response):
                self.fake.actions_permissions = lambda repo: response
                with self.assertRaises(publish.PublishError):
                    publish.publish(self.task, 'alice/fork', self.fake)
                self.assertEqual(self.fake.events, [])
        def unavailable(repo):
            raise delivery.DeliveryError('permission unavailable')
        self.fake.actions_permissions = unavailable
        with self.assertRaises(publish.PublishError):
            publish.publish(self.task, 'alice/fork', self.fake)
        self.assertEqual(self.fake.events, [])

    def test_actions_reenabled_between_push_and_pr_creation_blocks_pr(self):
        responses = iter([{'enabled': False}, {'enabled': True}])
        self.fake.actions_permissions = lambda repo: next(responses)
        with self.assertRaises(publish.PublishError):
            publish.publish(self.task, 'alice/fork', self.fake)
        self.assertEqual([e[0] for e in self.fake.events], ['push'])

    def test_body_update_rechecks_actions(self):
        self.fake.remote = 'head'
        self.fake.existing = [publish_fixtures.PublishTests.pull(8, 'head')]
        self.fake.actions_permissions = lambda repo: {'enabled': True}
        with self.assertRaises(publish.PublishError):
            publish.publish(self.task, 'alice/fork', self.fake)
        self.assertEqual(self.fake.events, [])

    def test_republish_rechecks_actions_before_branch_update_and_body_edit(self):
        old, head = 'a' * 40, 'b' * 40
        for initial in (True, False):
            with self.subTest(initial=initial):
                fake = publish_fixtures.FakeGitHub()
                fake.remote = old
                fake.existing = [publish_fixtures.PublishTests.pull(8, old)]
                responses = iter([{'enabled': True}] if initial else
                                 [{'enabled': False}, {'enabled': True}])
                fake.actions_permissions = lambda repo: next(responses)
                with patch.object(publish.cli, 'gate', return_value=(head, [])):
                    with self.assertRaises(publish.PublishError):
                        publish.republish(self.task, 'alice/fork', 8, old, fake)
                self.assertEqual([e[0] for e in fake.events], [] if initial else ['update'])

    def test_standalone_publish_and_republish_require_full_local_gate(self):
        with patch.object(local_gate, 'validate', side_effect=local_gate.LocalGateError('missing bound report')):
            with self.assertRaises(publish.PublishError):
                publish.publish(self.task, 'alice/fork', self.fake)
            old = 'a' * 40
            self.fake.remote = old
            self.fake.existing = [publish_fixtures.PublishTests.pull(8, old)]
            with self.assertRaises(publish.PublishError):
                publish.republish(self.task, 'alice/fork', 8, old, self.fake)
        self.assertEqual(self.fake.events, [])

    def test_policy_change_during_remote_inspection_blocks_first_push(self):
        current = copy.deepcopy(self.record)
        def permissions(repo):
            current['local_gate'] = {'mode': 'different'}
            return {'enabled': False}
        self.fake.actions_permissions = permissions
        with patch.object(publish.cli, 'read', side_effect=lambda path: copy.deepcopy(current)):
            with self.assertRaises(publish.PublishError):
                publish.publish(self.task, 'alice/fork', self.fake)
        self.assertEqual(self.fake.events, [])

    def test_report_invalidated_between_push_and_create_blocks_create(self):
        checks = []
        def validate(data, head):
            checks.append(head)
            if self.fake.events:
                raise local_gate.LocalGateError('bound report hash changed')
        with patch.object(local_gate, 'validate', side_effect=validate):
            with self.assertRaises(publish.PublishError):
                publish.publish(self.task, 'alice/fork', self.fake)
        self.assertEqual([e[0] for e in self.fake.events], ['push'])
        self.assertGreaterEqual(len(checks), 3)

    def test_nonlocal_mode_does_not_require_actions_permission(self):
        self.record.pop('local_gate')
        del self.fake.actions_permissions
        self.assertEqual(publish.publish(self.task, 'alice/fork', self.fake)['pr'], 7)

    def test_other_owner_or_nonfork_rejected_by_permission_guard(self):
        with self.assertRaises(delivery.DeliveryError):
            delivery.require_actions_disabled(self.fake, 'bob/fork')
        self.fake.repository = lambda repo: {'fork': False, 'owner': {'login': 'alice'}, 'default_branch': 'main'}
        with self.assertRaises(delivery.DeliveryError):
            delivery.require_actions_disabled(self.fake, 'alice/fork')


class RealEvidencePublicationTests(unittest.TestCase):
    def test_standalone_publish_rejects_real_missing_or_tampered_local_reports(self):
        for failure in ('missing-run', 'tampered-report'):
            with self.subTest(failure=failure):
                f = gate_fixtures.LocalGateTests('test_explicit_local_gate_allows_guarded_personal_fast_forward')
                f.setUp()
                try:
                    data = cli.read(f.task)
                    if failure == 'missing-run':
                        data['events'] = [e for e in data['events'] if e.get('type') != 'local-gate-run']
                        cli.write(f.task, data)
                    else:
                        run = next(e for e in data['events'] if e.get('type') == 'local-gate-run')
                        Path(run['reports'][1]['path']).write_text('{}')
                    fake = publish_fixtures.FakeGitHub()
                    fake.base = data['base_commit']
                    fake.actions_permissions = lambda repo: {'enabled': False}
                    with self.assertRaises((publish.PublishError, delivery.DeliveryError)):
                        publish.publish(f.task, 'alice/project', fake)
                    self.assertEqual(fake.events, [])
                finally:
                    f.tearDown()


class FinalMergeActionsTests(unittest.TestCase):
    def test_reenabled_actions_before_main_push_refuses_delivery(self):
        f = gate_fixtures.LocalGateTests('test_explicit_local_gate_allows_guarded_personal_fast_forward')
        f.setUp()
        self.addCleanup(f.tearDown)
        responses = iter([{'enabled': False}, {'enabled': False}, {'enabled': True}])
        f.github.actions_permissions = lambda repo: next(responses)
        with self.assertRaises(delivery.DeliveryError):
            delivery.guarded_merge(f.task, 'alice/project', 1, f.github)
        self.assertFalse(f.github.pushed)

    def _merged_fixture(self):
        f = gate_fixtures.LocalGateTests('test_explicit_local_gate_allows_guarded_personal_fast_forward')
        f.setUp()
        self.addCleanup(f.tearDown)
        data = cli.read(f.task)
        data.update(source='https://github.com/alice/project/issues/9',
                    original_source='https://github.com/alice/project/issues/9')
        cli.write(f.task, data)
        f.github.merged = True
        return f

    def test_merged_local_issue_does_not_close_with_actions_enabled(self):
        f = self._merged_fixture()
        f.github.actions_permissions = lambda repo: {'enabled': True}
        with self.assertRaises(delivery.DeliveryError):
            delivery.close_verified_issue(f.task, 'alice/project', 1, f.github)
        self.assertEqual(f.github.closed_issues, [])

    def test_merged_local_issue_revalidates_reports_after_actions_lookup(self):
        f = self._merged_fixture()
        def permissions(repo):
            data = cli.read(f.task)
            item = next(e for e in data['evidence'] if e['kind'] == 'test-result')
            Path(item['path']).write_text('{}')
            return {'enabled': False}
        f.github.actions_permissions = permissions
        with self.assertRaises(delivery.DeliveryError):
            delivery.close_verified_issue(f.task, 'alice/project', 1, f.github)
        self.assertEqual(f.github.closed_issues, [])

    def test_complete_local_evidence_allows_personal_issue_close(self):
        f = self._merged_fixture()
        self.assertTrue(delivery.close_verified_issue(f.task, 'alice/project', 1, f.github)['closed'])
        self.assertEqual(f.github.closed_issues, [9])

    def test_permission_api_uses_read_only_endpoint(self):
        github = delivery.GitHub()
        with patch.object(github, '_call', return_value={'enabled': False}) as call:
            self.assertEqual(github.actions_permissions('alice/fork'), {'enabled': False})
        call.assert_called_once_with('api', 'repos/alice/fork/actions/permissions')


if __name__ == '__main__':
    unittest.main()
