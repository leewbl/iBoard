"""Exercise post-settlement correction against the native reproduction fixtures."""

from pathlib import Path
from shutil import copytree
import time
import unittest
from unittest.mock import Mock

import test_benny
import test_benny_correction as fixtures
from workbench import benny_correction, benny_ops_correction, benny_reproduce, cli


class BennyOperationsCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BennyCorrectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.event = self.fixture.root
        self.runner = Mock(wraps=self.fixture.fixture.reproduce)

    def arguments(self, text='Correction: setup: clean-setup'):
        before = self.fixture.ready()
        timestamp = f"{int(before['ready_at']) + 10}.000002"
        message = {'ts': timestamp, 'author': 'U123PERSON', 'text': f'<@{test_benny.TRIAGE}> {text}',
                   'bot': False, 'files': []}
        scope = {'workspace': test_benny.WORKSPACE, 'source_channel': test_benny.CHANNEL,
                 'source_root': test_benny.ROOT, 'repository': test_benny.RULES['repository'],
                 'channel': 'G123OPERATIONS', 'identity': test_benny.TRIAGE}
        return {'event': self.event, 'outcome': {'status': 'reproduced', 'reproduction': before['initial'],
                 'source_notice': before['notice'], 'correction_options': before['archive']},
                'operations': {'scope': scope, 'root': timestamp[:-1]+'1', 'status': 'Reproduced'},
                'message': message, 'text': text, 'runtime': self.fixture.runtime, 'verification_seconds': 60,
                'slack': self.fixture.source.slack, 'runner': self.runner}

    def test_named_setup_reruns_once_on_same_revision_and_image(self):
        args = self.arguments()
        original = Path(args['outcome']['reproduction']['path'])
        before = cli.read(original)
        result = benny_ops_correction.run(**args)
        self.assertEqual(benny_ops_correction.run(**args), result)
        self.runner.assert_called_once()
        options = self.runner.call_args.kwargs
        self.assertEqual((options['revision'], options['image'], options['claim_seconds']),
                         (before['base'], before['image'], 0))
        record = cli.read(Path(result['reference']['path']))
        self.assertEqual(record['status'], 'reproduced')
        self.assertEqual((record['plan']['message'], record['plan']['variant']), (args['message'], 'clean-setup'))
        self.assertEqual(cli.digest(original), args['outcome']['reproduction']['sha256'])
        self.assertEqual(len(self.fixture.source.sent), 1)
        self.assertFalse((self.event/'tasks').exists())

    def test_original_reporter_natural_correction_can_use_frozen_guidance(self):
        self.fixture.variants['clean-setup']['applies_when'] = fixtures.GUIDANCE
        args = self.arguments('Correction: ' + fixtures.NATURAL_CORRECTION)
        model = self.fixture.language_model()
        result = benny_ops_correction.run(**args, model=model)
        record = cli.read(Path(result['reference']['path']))
        selection = cli.read(Path(record['plan']['interpretation']['path']))
        self.assertEqual(selection['selection']['variant'], 'clean-setup')
        model.assert_called_once()
        self.assertLessEqual(model.call_args.args[2], args['verification_seconds'])

    def test_unauthorized_or_changed_source_cannot_trigger_a_run(self):
        args = self.arguments()
        with self.assertRaisesRegex(ValueError, 'original reporter'):
            benny_ops_correction.run(**{**args, 'message': {**args['message'], 'author': 'U123OTHER'}})
        self.fixture.source.pages[-1].append(test_benny.reply(700, 'I now claim this report.'))
        with self.assertRaisesRegex(ValueError, 'source changed'):
            benny_ops_correction.run(**args)
        self.runner.assert_not_called()

    def test_process_loss_recovers_completed_receipt_without_a_second_run(self):
        args = self.arguments()

        def interrupted(**options):
            self.runner(**options)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            benny_ops_correction.run(**{**args, 'runner': interrupted})
        result = benny_ops_correction.run(**args)
        self.assertEqual(cli.read(Path(result['reference']['path']))['phase'], 'completed')
        self.runner.assert_called_once()

    def test_incomplete_attempt_holds_and_a_second_message_cannot_reopen(self):
        args = self.arguments()
        with self.assertRaises(KeyboardInterrupt):
            benny_ops_correction.run(**{**args, 'runner': Mock(side_effect=KeyboardInterrupt)})
        with self.assertRaisesRegex(ValueError, 'no completed receipt'):
            benny_ops_correction.run(**args)
        next_message = {**args['message'], 'ts': args['message']['ts'][:-1] + '3'}
        with self.assertRaisesRegex(ValueError, 'already admitted'):
            benny_ops_correction.run(**{**args, 'message': next_message})
        self.runner.assert_not_called()

    def test_healthy_correction_replies_from_checks_and_retains_settled_draft_state(self):
        args = self.arguments()
        outcome = {**args['outcome'], 'status': 'draft', 'repair': {'url': 'https://github.com/owner/repo/pull/1'}}
        operations = {**args['operations'], 'status': 'Draft pull request opened'}
        self.fixture.fixture.runtime.states.clear()
        result = benny_ops_correction.run(**{**args, 'outcome': outcome, 'operations': operations})
        record = cli.read(Path(result['reference']['path']))
        self.assertEqual(record['status'], 'not-reproduced')
        self.assertIn('did not reproduce', result['body'])
        self.assertEqual((outcome['status'], operations['status']), ('draft', 'Draft pull request opened'))
        self.assertFalse((self.event/'tasks').exists())

    def test_completed_evidence_cannot_be_replaced(self):
        args = self.arguments()
        result = benny_ops_correction.run(**args)
        audit = {key: args[key] for key in ('event', 'outcome', 'operations')}
        self.assertEqual(benny_ops_correction.confirmed(result['reference'], **audit), result['body'])
        sample = self.event/'operations-correction/reproduction/baseline/main.primary-1.json'
        sample.write_bytes(sample.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'frozen sandbox receipt'):
            benny_ops_correction.confirmed(result['reference'], **audit)
        with self.assertRaisesRegex(ValueError, 'frozen sandbox receipt'):
            benny_ops_correction.run(**args)
        self.runner.assert_called_once()

    def test_can_check_a_distinct_ops_correction_after_a_source_window_correction(self):
        initial_helpers = self.fixture.variants['clean-setup']['helpers']
        alternate = self.event/'fresh-ops-helpers'
        copytree(initial_helpers, alternate)
        main = alternate/'main.sh'
        main.write_bytes(main.read_bytes() + b'\n# alternate approved setup\n')
        self.fixture.variants['fresh-ops'] = {**self.fixture.variants['clean-setup'], 'helpers': alternate}
        args = self.arguments('Correction: setup: fresh-ops')
        outcome = args['outcome']
        row = cli.read(Path(outcome['source_notice']['path']))['reply']
        proof = cli.read(Path(outcome['reproduction']['path']))
        ready = benny_reproduce._claim_ready_at(proof, row)
        source = {'ready_at': ready, 'initial': outcome['reproduction'], 'notice': outcome['source_notice'],
                  'archive': outcome['correction_options'], 'slack': args['slack'], 'out': self.event/'correction/source'}
        self.fixture.add_correction(source)
        route = benny_correction.select(**source)
        options = benny_correction.options(source['archive'], 'clean-setup', self.fixture.runtime)
        out = self.event/'correction/reproduction'
        self.fixture.fixture.reproduce(route=route['path'], sha256=route['sha256'], out=out, **options)
        corrected = {'path': str(out/'result.json'), 'sha256': cli.digest(out/'result.json')}
        live = {**outcome, 'reproduction': corrected, 'original_reproduction': outcome['reproduction'],
                'corrected_route': route}
        result = benny_ops_correction.run(**{**args, 'outcome': live})
        record = cli.read(Path(result['reference']['path']))
        self.assertEqual(record['plan']['settled'], corrected)
        self.assertEqual(cli.digest(out/'result.json'), corrected['sha256'])

    def test_does_not_start_a_model_or_run_without_enough_window_time(self):
        self.fixture.variants['clean-setup']['applies_when'] = fixtures.GUIDANCE
        args = self.arguments('Correction: ' + fixtures.NATURAL_CORRECTION)
        model = self.fixture.language_model()
        with self.assertRaisesRegex(ValueError, 'insufficient time'):
            benny_ops_correction.run(**args, model=model, expires_at=time.time() + 5)
        model.assert_not_called()
        self.runner.assert_not_called()
