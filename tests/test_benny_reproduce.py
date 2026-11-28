from contextlib import ExitStack
from datetime import datetime, timezone
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_benny
import test_project_triage
from workbench import autopilot, benny, benny_reproduce, cli, delivery, publish


class BennyReproductionTests(unittest.TestCase):
    def setUp(self):
        runtime = test_project_triage.ProjectTriageTests()
        self.addCleanup(runtime.doCleanups)
        runtime.setUp()
        self.runtime = runtime
        self.root = runtime.root
        self.repo = runtime.repo
        self.base = runtime.base
        cli.git(self.repo, 'remote', 'add', 'origin', 'https://github.com/leewbl/myopenclaw.git')
        runtime.states.update({(test_project_triage.BASE_IMAGE, probe): 'observed' for probe in ('MAIN', 'ORACLE')})
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        self.source = contexts.enter_context(test_benny.Fixture(self.root))
        self.source.pages = [[self.source.root, test_benny.reply(1, '[benny:bug]', user=test_benny.TRIAGE)]]
        self.route = benny.freeze(rules=test_benny.RULES, slack=self.source.slack, channel=test_benny.CHANNEL,
                                  message_ts=test_benny.ROOT, out=self.root/'route')
        self.tracker = Mock()
        self.tracker.search.return_value = {'items': [], 'total_count': 0, 'incomplete_results': False}

    def reproduce(self, **override):
        runtime = self.runtime
        options = dict(slack=self.source.slack, route=self.route['receipt'], sha256=self.route['sha256'],
                       repo=self.repo, revision=self.base, image='runtime:local', manifest=runtime.manifest,
                       helpers=runtime.helpers, journey='main', oracle=runtime.oracle,
                       oracle_marker='ORACLE_PASS', oracle_failure_marker='ORACLE_FAIL',
                       out=self.root/'before', memory_mb=512, attester=runtime.attester, sandbox=runtime.sandbox,
                       claim_seconds=0)
        return benny_reproduce.reproduce(**(options | override))

    def receipt(self):
        path = self.root/'before/result.json'
        return dict(receipt=path, sha256=cli.digest(path))

    def publish_notice(self, reproduction=None):
        from workbench import benny_notice
        if reproduction is None:
            receipt = self.receipt()
            reproduction = {'path': str(receipt['receipt']), 'sha256': receipt['sha256']}
        self.source.reply_behavior = 'accepted'
        return benny_notice.publish(path=self.root/'source-notice.json', reproduction=reproduction,
                                    slack=self.source.slack, writer=self.source.writer, identity=test_benny.TRIAGE)

    def after(self, **override):
        def derive(repo, base, head, image, timeout, *, rebuild_openclaw_ui):
            self.assertFalse(rebuild_openclaw_ui)
            return self.runtime.derive(repo, base, head, image, timeout)

        options = dict(**self.receipt(), revision=self.runtime.revision, out=self.root/'after',
                       sandbox=self.runtime.sandbox, derive=derive)
        return benny_reproduce.verify_candidate(**(options | override))

    def prepare(self, **override):
        def launch(*, prepare, prepare_only, require_root_cause, repo, issue_file, source, kind, surface, task_id,
                   tasks_root, workspaces_root, iterations, seconds, image, source_mode, build_source_image,
                   memory_mb):
            self.assertTrue(prepare_only and require_root_cause and build_source_image)
            self.assertEqual((source_mode, memory_mb, image), ('image', 512, test_project_triage.BASE_IMAGE))
            self.assertIn(self.source.root['text'], issue_file.read_text())
            info = prepare(repo=repo, source=source, kind=kind, surface=surface, task_id=task_id, tasks_root=tasks_root,
                           workspaces_root=workspaces_root, iterations=iterations, seconds=seconds, implementer='test')
            task = Path(info['task'])
            cli.write(task, {**cli.read(task), 'status': 'paused'})
            return {**info, 'status': 'paused'}

        options = dict(**self.receipt(), slack=self.source.slack, task_id='safe-intake', tasks_root=self.root/'tasks',
                       workspaces_root=self.root/'workspaces', tracker=self.tracker, fix_search=('affected feature',),
                       launch=launch)
        return benny_reproduce.prepare_repair(**(options | override))

    def test_valid_frozen_before_and_after_use_distinct_sandboxes_and_same_drivers(self):
        self.assertEqual(self.reproduce()['status'], 'reproduced')
        self.assertEqual(self.after()['status'], 'verified')
        for directory, image, observation in (
                ('before/baseline', test_project_triage.BASE_IMAGE, 'observed-failure'),
                ('after/candidate', test_project_triage.CANDIDATE_IMAGE, 'pass')):
            report = cli.read(self.root/directory/'result.json')
            self.assertEqual(report['observations']['primary'], [observation] * 2)
            self.assertEqual(report['observations']['oracle'], [observation] * 2)
            self.assertEqual(self.runtime.calls.count((image, 'MAIN')), 2)
            self.assertEqual(self.runtime.calls.count((image, 'ORACLE')), 2)
        self.assertEqual(cli.git(self.repo, 'status', '--porcelain'), '')

    def test_healthy_baseline_is_retained_without_admitting_candidate(self):
        self.runtime.states.clear()
        self.assertEqual(self.reproduce()['status'], 'not-reproduced')
        with self.assertRaisesRegex(ValueError, 'confirmed, unchanged reproduction'):
            self.after()

    def test_runtime_failure_without_discriminating_marker_is_environment_blocked(self):
        self.runtime.states[(test_project_triage.BASE_IMAGE, 'ORACLE')] = 'crash'
        self.assertEqual(self.reproduce()['status'], 'environment-blocked')

    def test_source_change_during_runtime_withholds_confirmation(self):
        def sandbox(*args, **kwargs):
            self.source.pages[0].append(test_benny.reply(2, "I'll fix it"))
            return self.runtime.sandbox(*args, **kwargs)

        result = self.reproduce(sandbox=sandbox)
        self.assertEqual((result['status'], result['runtime_status']), ('source-unconfirmed', 'reproduced'))
        with self.assertRaisesRegex(ValueError, 'confirmed, unchanged reproduction'):
            self.prepare()

    def test_tampered_sandbox_receipt_blocks_work(self):
        self.reproduce()
        path = self.root/'before/baseline/main.primary-1.json'
        path.write_text(path.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'frozen sandbox receipt'):
            self.prepare()

    def test_candidate_cannot_reuse_a_baseline_container(self):
        self.reproduce()
        self.runtime.calls.clear()
        result = self.after()
        self.assertEqual(result['status'], 'environment-blocked')
        self.assertIn('reused a baseline container', result['reason'])

    def test_candidate_with_the_same_failure_does_not_verify(self):
        self.reproduce()
        self.runtime.states.update({(test_project_triage.CANDIDATE_IMAGE, probe): 'observed'
                                    for probe in ('MAIN', 'ORACLE')})
        self.assertEqual(self.after()['status'], 'still-reproduced')

    def test_claim_window_or_existing_fix_blocks_preparation(self):
        self.reproduce(claim_seconds=60)
        with self.assertRaisesRegex(ValueError, 'ownership window'):
            self.prepare()
        self.tracker.search.assert_not_called()
        self.tracker.search.return_value.update(total_count=1, items=[{'number': 5, 'state': 'open',
            'title': 'Fix the affected feature', 'html_url': test_benny.PR, 'pull_request': {}}])
        with patch.object(benny_reproduce.time, 'time', return_value=4_000_000_000):
            with self.assertRaisesRegex(ValueError, 'possible existing fix'):
                self.prepare()
        self.assertFalse((self.root/'tasks').exists())

    def test_source_change_after_reproduction_blocks_preparation(self):
        self.reproduce()
        self.source.pages[0].append(test_benny.reply(2, 'I am working on this'))
        with self.assertRaisesRegex(ValueError, 'source changed'):
            self.prepare()
        self.tracker.search.assert_not_called()

    def test_only_the_confirmed_notice_can_be_removed_from_the_frozen_source_comparison(self):
        self.reproduce()
        notice = self.publish_notice()
        with self.assertRaisesRegex(ValueError, 'source changed'):
            self.prepare()
        posted = self.source.pages[-1][-1]
        self.assertIn('reproduced', posted['text'].casefold())
        text = posted['text']
        with patch.dict(posted, text=text + ' Edited'):
            with self.assertRaisesRegex(ValueError, 'confirmed reproduction notice is missing'):
                self.prepare(source_notice=notice)
        self.source.pages[-1].append(test_benny.reply(95, "I'll fix it"))
        with self.assertRaisesRegex(ValueError, 'source changed'):
            self.prepare(source_notice=notice)
        self.source.pages[-1].pop()
        self.tracker.search.assert_not_called()
        task = Path(self.prepare(source_notice=notice)['task'])
        self.assertEqual(cli.read(task)['benny_reproduction']['source_notice'], notice)
        self.assertEqual(len(self.source.sent), 1)

    def test_ownership_window_extends_from_the_confirmed_notice(self):
        completed = self.reproduce(claim_seconds=60)
        before = float(test_benny.ROOT) - 120
        path = self.receipt()['receipt']
        cli.write(path, {**completed, 'finished_at': datetime.fromtimestamp(before, timezone.utc).isoformat()})
        notice = self.publish_notice()
        posted_at = float(self.source.pages[-1][-1]['ts'])
        with patch.object(benny_reproduce.time, 'time', return_value=posted_at + 59):
            with self.assertRaisesRegex(ValueError, 'ownership window'):
                self.prepare(source_notice=notice)
        self.tracker.search.assert_not_called()

    def test_local_handoff_preserves_actual_baseline_and_requires_dedicated_delivery(self):
        self.reproduce()
        ready = self.prepare()
        task = Path(ready['task'])
        data = cli.read(task)
        self.assertEqual(data['benny_reproduction']['sha256'], self.receipt()['sha256'])
        self.assertEqual(data['base_commit'], self.base)
        self.assertEqual(cli.sha(ready['workspace'], 'HEAD'), self.base)
        github = Mock()
        with self.assertRaisesRegex(autopilot.AutopilotError, 'dedicated launch'):
            issue = self.root/'before/report.txt'
            autopilot._run_locked(task, issue, test_project_triage.BASE_IMAGE, journal=None)
        with self.assertRaisesRegex(publish.PublishError, 'requires a draft PR'):
            publish.publish(task, test_benny.RULES['repository'], github=github)
        with self.assertRaisesRegex(publish.PublishError, 'requires a draft PR'):
            publish.republish(task, test_benny.RULES['repository'], 5, self.base, github=github)
        with self.assertRaisesRegex(delivery.DeliveryError, 'do not authorize merging'):
            delivery._guarded_merge(task, test_benny.RULES['repository'], 5, github=github)
        self.assertFalse(github.mock_calls)
