import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from workbench import benny_correction_language, cli


class BennyCorrectionLanguageTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        event = Path(scratch.name).resolve()
        self.path = event/'correction/interpretation.json'
        self.binding = {}
        for key, name in benny_correction_language.REFS.items():
            artifact = event/name
            cli.write(artifact, {'schema': cli.SCHEMA, 'artifact': key})
            self.binding[key] = {'path': str(artifact), 'sha256': cli.digest(artifact)}
        self.message = {'ts': '1800100010.000001', 'author': 'UREPORTER', 'bot': False, 'files': [],
                        'text': 'I used an existing session by mistake. Please use a session with no saved history.'}
        self.guidance = {'fresh-session': 'Start a session without saved history before running the same probes.',
                         'retained-session': 'Restore the session with a seeded history before running the same probes.'}
        self.model = Mock(side_effect=self.selected)

    def result(self, **fields):
        return {'exit_code': 0, 'timed_out': False, 'provider': 'zhipu', 'model': 'glm-5.3[1m]',
                'stdout': json.dumps({'status': 'ready', **fields})}

    def selected(self, workspace, prompt, timeout, **flags):
        self.assertTrue(workspace.is_dir())
        self.assertEqual(list(workspace.iterdir()), [])
        self.assertEqual(timeout, 30)
        self.assertEqual(flags, {'read_only': True, 'file_tools_only': True,
                                 'private_workspace': True, 'capture_trace': True})
        payload = json.loads(prompt.split('\n', 1)[1])
        self.assertEqual(payload, {'reporter_message': self.message['text'], 'approved_setups': self.guidance})
        self.assertEqual(cli.read(self.path)['status'], 'interpreting')
        return self.result(variant='fresh-session', quote='use a session with no saved history',
                           guidance_quote='Start a session without saved history')

    def choose(self):
        return benny_correction_language.choose(self.path, message=self.message, guidance=self.guidance,
                                                binding=self.binding, model=self.model)

    def confirmed(self, reference, **changes):
        inputs = {'message': self.message, 'guidance': self.guidance, 'binding': self.binding}
        return benny_correction_language.confirmed(reference, **(inputs | changes))

    def test_reuses_cited_selection_without_another_model_call(self):
        reference = self.choose()
        self.assertEqual(self.confirmed(reference), 'fresh-session')
        self.assertEqual(self.choose(), reference)
        self.model.assert_called_once()
        state = cli.read(self.path)
        self.assertEqual(state['model'], {'provider': 'zhipu', 'model': 'glm-5.3[1m]', 'exit_code': 0, 'timed_out': False})
        self.assertEqual(state['selection']['quote'], 'use a session with no saved history')
        self.assertEqual(state['prompt_sha256'], hashlib.sha256(self.model.call_args.args[1].encode()).hexdigest())

    def test_preserves_no_match_without_falling_back(self):
        self.model.side_effect = None
        self.model.return_value = self.result(variant=None, quote=None, guidance_quote=None)
        reference = self.choose()
        self.assertIsNone(self.confirmed(reference))
        self.assertEqual(self.choose(), reference)
        self.model.assert_called_once()

    def test_rejects_a_guidance_quote_shared_by_other_setups(self):
        self.model.side_effect = None
        self.model.return_value = self.result(variant='fresh-session', quote='use a session with no saved history',
                                              guidance_quote='before running the same probes')
        with self.assertRaisesRegex(ValueError, 'interpretation blocked'):
            self.choose()
        self.assertEqual(cli.read(self.path)['status'], 'blocked')
        with self.assertRaisesRegex(ValueError, 'lacks a completed result'):
            self.choose()
        self.model.assert_called_once()

    def test_rejects_invented_message_excerpt(self):
        self.model.side_effect = None
        self.model.return_value = self.result(variant='fresh-session', quote='never use the existing profile',
                                              guidance_quote='Start a session without saved history')
        with self.assertRaisesRegex(ValueError, 'interpretation blocked'):
            self.choose()
        self.assertNotIn('never use the existing profile', self.path.read_text())

    def test_rejects_unapproved_setup_and_changed_evidence(self):
        reference = self.choose()
        with self.assertRaisesRegex(ValueError, 'lacks a completed result'):
            self.confirmed(reference, guidance={**self.guidance, 'fresh-session': 'Use an empty context.'})
        state = cli.read(self.path)
        state['selection']['variant'] = 'unapproved-session'
        cli.write(self.path, state)
        with self.assertRaisesRegex(ValueError, 'approved digest'):
            self.confirmed(reference)
        with self.assertRaisesRegex(ValueError, 'unique supporting excerpts'):
            self.confirmed({**reference, 'sha256': cli.digest(self.path)})

    def test_interrupted_attempt_is_not_repeated(self):
        self.model.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.choose()
        self.assertEqual(cli.read(self.path)['status'], 'interpreting')
        with self.assertRaisesRegex(ValueError, 'lacks a completed result'):
            self.choose()
        self.model.assert_called_once()

    def test_provider_failure_retains_metadata_only_and_blocks_retry(self):
        self.model.side_effect = None
        self.model.return_value = {'exit_code': None, 'timed_out': True, 'provider': 'zhipu',
                                   'stdout': 'internal provider diagnostic', 'stderr': 'credential callback diagnostic'}
        with self.assertRaisesRegex(ValueError, 'interpretation blocked'):
            self.choose()
        record = cli.read(self.path)
        self.assertTrue(record['model']['timed_out'])
        self.assertNotIn('diagnostic', self.path.read_text())
        with self.assertRaises(ValueError):
            self.choose()
        self.model.assert_called_once()

    def test_rejects_mismatched_event_references_before_invoking_model(self):
        self.binding['notice']['path'] = str(self.path.parent/'source-notice.json')
        with self.assertRaisesRegex(ValueError, 'original event receipts'):
            self.choose()
        self.assertFalse(self.path.exists())
        self.model.assert_not_called()
