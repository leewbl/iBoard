import argparse
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

import test_benny
import test_benny_reproduce
import test_project_triage
from workbench import autopilot, benny_repair, cli, delivery, start


class BennyRepairTests(unittest.TestCase):
    def setUp(self):
        source = test_benny_reproduce.BennyReproductionTests()
        self.addCleanup(source.doCleanups)
        source.setUp()
        self.source = source
        source.reproduce()

    def prepare(self):
        real_run = subprocess.run

        def run(args, *options, **kwargs):
            if args[:3] == ['docker', 'image', 'inspect']:
                return subprocess.CompletedProcess(args, 0, json.dumps([{'Id': test_project_triage.BASE_IMAGE}]).encode(), b'')
            return real_run(args, *options, **kwargs)

        with patch.object(start.shutil, 'which', return_value='/mock/bin/tool'), patch.object(start.subprocess, 'run', run):
            return Path(self.source.prepare(launch=start.start)['task'])

    def test_terminal_receipt_rechecks_original_raw_candidate_probes(self):
        self.assertEqual(self.source.after()['status'], 'verified')
        root = self.source.root
        path = root/'after/result.json'
        reference = {'path': str(path), 'sha256': cli.digest(path)}
        binding = {'path': str(self.source.receipt()['receipt']), 'sha256': self.source.receipt()['sha256']}
        head = self.source.runtime.revision
        self.assertEqual(benny_repair.candidate_proof(binding, reference, head)['head'], head)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            benny_repair.candidate_proof(binding, reference, self.source.base)
        observation = root/'after/candidate/main.primary-1.json'
        observation.write_text(observation.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'frozen sandbox receipt'):
            benny_repair.candidate_proof(binding, reference, head)

    def test_new_report_owner_blocks_before_model_invocation(self):
        task = self.prepare()
        self.source.source.pages[0].append(test_benny.reply(2, "I will fix this"))
        model = Mock()

        def execute(*args, **kwargs):
            return autopilot.run(*args, **kwargs, model=model)

        with self.assertRaisesRegex(start.StartError, 'source changed'):
            benny_repair.run_repair(task=task, slack=self.source.source.slack, tracker=self.source.tracker, execute=execute)
        model.assert_not_called()
        record = cli.read(task)
        self.assertEqual((record['status'], record['iterations']), ('paused', 0))

    def test_tracker_outage_holds_task_before_editing(self):
        task = self.prepare()
        model = Mock()
        self.source.tracker.search.side_effect = delivery.DeliveryError('unavailable')

        def execute(*args, **kwargs):
            return autopilot.run(*args, **kwargs, model=model)

        with self.assertRaisesRegex(start.StartError, 'tracker confirmation failed'):
            benny_repair.run_repair(task=task, slack=self.source.source.slack, tracker=self.source.tracker, execute=execute)
        model.assert_not_called()
        self.assertEqual(cli.read(task)['status'], 'paused')

    def test_original_tracker_search_cannot_be_broadened_at_launch(self):
        task = self.prepare()
        execute = Mock()
        with self.assertRaisesRegex(ValueError, 'cannot change'):
            benny_repair.run_repair(task=task, slack=self.source.source.slack, tracker=self.source.tracker,
                                    fix_search=('different phrase',), execute=execute)
        execute.assert_not_called()

    def test_completed_paused_checkpoint_rechecks_evidence_without_restarting(self):
        task = self.prepare()
        data = cli.read(task)
        head = self.source.runtime.revision
        cli.criterion_add(argparse.Namespace(id='match', text='The observed outcome is corrected.', reason='Test'), data)
        before = self.source.receipt()['receipt']
        cli.evidence_add(argparse.Namespace(file=before, kind='reproduction', commit=self.source.base, actor='fixture',
                                            criterion=['match'], description='Real baseline'), data)
        cli.git(data['repo'], 'merge', '--ff-only', head)
        self.assertEqual(self.source.after()['status'], 'verified')
        candidate = self.source.root/'after/result.json'
        proof = {'path': str(candidate), 'sha256': cli.digest(candidate)}
        data['benny_candidate'] = proof
        review = task.with_name('independent.json')
        cli.write(review, {'benny_candidate': proof})
        for kind, path in (('resolution', candidate), ('runtime-log', candidate), ('test-result', candidate),
                           ('independent-review', review)):
            cli.evidence_add(argparse.Namespace(file=path, kind=kind, commit=head, actor='fixture',
                                                criterion=['match'], description='Retained candidate'), data)
        cli.write(task, data)
        cli.write(task.with_name('autopilot.json'), {'schema': cli.SCHEMA, 'phase': 'delivery', 'candidate_commit': head})
        execute = Mock()
        with patch.object(delivery, '_independent_verification', return_value={'path': str(review)}):
            result = benny_repair.run_repair(task=task, slack=self.source.source.slack, execute=execute)
        self.assertEqual((result['status'], result['head'], cli.read(task)['status']), ('verified', head, 'paused'))
        execute.assert_not_called()

    def test_idle_running_plan_can_resume_but_interrupted_edit_cannot(self):
        task = self.prepare()
        data = {**cli.read(task), 'status': 'running'}
        stage = {'schema': cli.SCHEMA, 'phase': 'implementing', 'benny_reproduction': data['benny_reproduction']}
        cli.write(task, data)
        cli.write(task.with_name('autopilot.json'), stage)
        execute = Mock(side_effect=autopilot.AutopilotError('reached native worker'))
        with self.assertRaisesRegex(ValueError, 'unresolved interrupted operation'):
            benny_repair.run_repair(task=task, slack=self.source.source.slack, execute=execute)
        execute.assert_not_called()
        cli.write(task.with_name('autopilot.json'), {**stage, 'phase': 'plan'})
        with self.assertRaisesRegex(start.StartError, 'reached native worker'):
            benny_repair.run_repair(task=task, slack=self.source.source.slack, execute=execute)
        execute.assert_called_once()
        data = cli.read(task)
        self.assertEqual(data['status'], 'paused')
        self.assertTrue(any('unchanged local checkpoint' in event.get('reason', '') for event in data['events']))

    def test_benny_implementation_and_review_get_only_private_file_tools(self):
        task = self.prepare()
        cli.write(task, {**cli.read(task), 'status': 'running'})
        model = Mock(return_value={'exit_code': 0, 'stdout': 'ok'})
        with patch.object(autopilot, 'execute_model', model):
            for stage, read_only in (('implementation', False), ('final-review', True)):
                autopilot._model(task, stage, 'A frozen report.', read_only=read_only, model=model)
                _, prompt, _ = model.call_args.args
                self.assertIn('Never send Slack messages', prompt)
                self.assertEqual(model.call_args.kwargs, {'read_only': read_only, 'file_tools_only': True,
                    'capture_trace': True, 'private_workspace': True})
