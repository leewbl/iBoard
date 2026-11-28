import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from model import codex_edit


def _lines(*events):
    return '\n'.join(json.dumps(event) for event in events).encode()


def _call(tool, path, status='completed'):
    return {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'server': 'desk',
            'tool': tool, 'arguments': {'path': path}, 'status': status}}


class CodexEditTests(unittest.TestCase):
    def test_reads_come_from_broker_even_when_model_claims_success(self):
        output = _lines(_call('read_file', 'guide.md', 'failed'), _call('write_file', 'code.mjs'),
                        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'I read it.'}},
                        {'type': 'turn.completed'})
        rows = _lines({'id': '1', 'tool': 'Write', 'path': 'code.mjs', 'ok': True},
                      {'id': '2', 'tool': 'Read', 'path': 'guide.md', 'ok': False})
        calls = codex_edit._calls(output)
        trace = codex_edit._observed(rows, calls, ['guide.md', 'code.mjs'], ['code.mjs'])
        self.assertEqual([event['error'] for event in trace[1::2]], [False, True])
        with self.assertRaisesRegex(codex_edit.EditError, 'disagree'):
            codex_edit._observed(rows, calls[:1], ['guide.md', 'code.mjs'], ['code.mjs'])

    def test_declined_out_of_scope_path_is_not_retained(self):
        output = _lines(_call('write_file', 'guide.md', 'failed'), {'type': 'turn.completed'})
        rows = _lines({'id': '1', 'tool': 'Write', 'path': '', 'ok': False})
        trace = codex_edit._observed(rows, codex_edit._calls(output), ['guide.md', 'code.mjs'], ['code.mjs'])
        self.assertEqual(trace[0]['path'], '')
        self.assertTrue(trace[1]['error'])

    def test_other_tools_and_incomplete_sessions_are_refused(self):
        with self.assertRaisesRegex(codex_edit.EditError, 'unexpected tool'):
            codex_edit._calls(_lines({'type': 'item.completed', 'item': {'type': 'command_execution'}},
                                     {'type': 'turn.completed'}))
        with self.assertRaisesRegex(codex_edit.EditError, 'complete bounded turn'):
            codex_edit._calls(_lines(_call('read_file', 'guide.md')))

    def test_execute_keeps_the_user_request_and_restricts_the_tool_configuration(self):
        prompt = 'Read guide.md and revise the status.'
        with TemporaryDirectory() as folder:
            work = Path(folder).resolve()

            class Child:
                returncode = 0

                def __init__(child, command, **kwargs):
                    self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
                    self.assertIn('--ephemeral', command)
                    self.assertIn('web_search="disabled"', command)
                    self.assertEqual(command[command.index('shell_tool') - 1], '--disable')
                    server = next(arg for arg in command if arg.startswith('mcp_servers='))
                    self.assertIn('enabled_tools=["read_file","write_file"]', server)
                    config_path = json.JSONDecoder().raw_decode(server.split(',args=', 1)[1])[0][-1]
                    cfg = json.loads(Path(config_path).read_text())
                    self.assertEqual(cfg['workspace'], str(work))
                    self.assertEqual(cfg['editable'], ['status.mjs'])
                    self.assertFalse(Path(cfg['trace']).is_relative_to(work))
                    Path(cfg['trace']).write_bytes(_lines(
                        {'id': '1', 'tool': 'Read', 'path': 'guide.md', 'ok': True}))
                    kwargs['stdout'].write(_lines(_call('read_file', 'guide.md'), {'type': 'turn.completed'}))

                def communicate(child, actual, *, timeout):
                    self.assertEqual(actual, prompt)
                    self.assertEqual(timeout, 30)

            with patch.object(codex_edit.subprocess, 'Popen', Child):
                result = codex_edit.execute(work, prompt, 30, requested_model='gpt-worker',
                                            files=['guide.md', 'status.mjs'], editable=['status.mjs'])
            self.assertEqual(result['host'], 'codex')
            self.assertFalse(result['trace'][1]['error'])
