import json
from pathlib import Path
import tempfile
import unittest

from model import codex_review
from workbench import cli, decisions, decision_review


class DecisionReviewTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.task = self.root / 'task.json'
        cli.write(self.task, {'schema':cli.SCHEMA, 'id':'case', 'repo':str(self.root)})
        self.kept = self.root / 'candidate.json'
        cli.write(self.kept, {'exit_code':0, 'cleanup_confirmed':True, 'stdout':'OBSERVED',
                             'runs':{'gateway':{'stdout':'unselected log line\n{"scratchRemoved":true}\nGATEWAY_READY'}}})
        self.ref = decisions.artifact(self.task, self.kept)
        self.run = decisions.begin(self.task)
        decisions.retain_stage(self.task, self.run, 'final-review', cli.now(),
                               {'provider':'zhipu', 'model':'glm-test', 'exit_code':0, 'stdout':'Reviewed the runner output'})
        decisions.record(self.task, self.run, 'verify', 'Verified a real observation.', 'Runner emitted OBSERVED.',
                         self.ref, 'Verification complete.')

    def reply(self, prompt, model, timeout):
        data = json.loads(prompt.split('\n\n', 1)[-1])
        self.assertEqual(data['transcript']['T1']['provider'], 'zhipu')
        self.assertEqual(data['evidence']['E3']['observations']['stdout_tail'], 'OBSERVED')
        self.assertEqual(data['evidence']['E3']['observations']['runs']['gateway']['signals'],
                         [{'scratchRemoved':True}, 'GATEWAY_READY'])
        return {'decisions':[{'row':3, 'verdict':'supported', 'reason':'The runner completed.',
                              'citations':['T1', 'E3']}], 'gaps':[]}

    def test_review_freezes_sources_and_records_the_distinct_vendor(self):
        other = decisions.begin(self.task)
        decisions.retain_stage(self.task, other, 'plan', cli.now(), {'provider':'zhipu', 'stdout':'Unrelated run'})
        output = decision_review.review(self.task, self.run, 'gpt-test', reviewer=self.reply)
        self.assertFalse(output['attention'])
        self.assertEqual(output['working_provider'], 'zhipu')
        self.assertEqual(output['reviewer'], {'provider':'openai', 'model':'gpt-test'})
        self.assertEqual(len(output['transcript']), 1)
        self.assertEqual(cli.read(Path(output['report']))['input_sha256'], output['input_sha256'])

    def test_review_rejects_hallucinated_citations_and_changed_inputs(self):
        def nonexistent(*args):
            response = self.reply(*args)
            response['decisions'][0]['citations'] = ['T2', 'E3']
            return response
        with self.assertRaisesRegex(decisions.DecisionError, 'invalid citation'):
            decision_review.review(self.task, self.run, 'gpt-test', reviewer=nonexistent)

        def changed(*args):
            response = self.reply(*args)
            self.kept.write_text('substituted')
            return response
        with self.assertRaisesRegex(decisions.DecisionError, 'changed while'):
            decision_review.review(self.task, self.run, 'gpt-test', reviewer=changed)
        self.assertEqual(list(self.root.glob('decision-review-*.json')), [])

    def test_changed_linked_report_cannot_support_a_success_claim(self):
        parent = self.root / 'independent-review.json'
        cli.write(parent, {'verdict':'PASS','reports':{'R4':{'path':str(self.kept), 'sha256':cli.digest(self.kept)}}})
        decisions.record(self.task, self.run, 'verify', 'Independent review passed.', 'Runtime test supported it.',
                         decisions.artifact(self.task, parent), 'Reviewed.')
        self.kept.unlink()
        data, _ = decision_review._capture(self.task, self.run, None, None)
        with self.assertRaisesRegex(decisions.DecisionError, 'unresolved evidence'):
            decision_review._validate({
                'decisions':[{'row':3, 'verdict':'uncertain', 'reason':'Original run unavailable.', 'citations':['E3']},
                             {'row':4, 'verdict':'supported', 'reason':'Review claims pass.', 'citations':['E4','T1']}],
                'gaps':[]}, data)

    def test_model_unavailable_requires_failed_structured_cli_error(self):
        body = {'type': 'error', 'status': 400, 'error': {'type': 'invalid_request_error',
                'message': 'The model is not supported with this account.', 'code': 'model_not_found'}}
        for kind in ('error', 'turn.failed'):
            for status in (400, 404):
                payload = {**body, 'status': status}
                event = ({'type': 'error', 'message': json.dumps(payload)} if kind == 'error' else
                         {'type': 'turn.failed', 'error': {'message': json.dumps(payload)}})
                with self.subTest(kind=kind, status=status):
                    with self.assertRaises(codex_review.ModelUnavailable):
                        codex_review._result(json.dumps(event), 1)
                    with self.assertRaises(codex_review.ReviewError) as success:
                        codex_review._result(json.dumps(event), 0)
                    self.assertNotIsInstance(success.exception, codex_review.ModelUnavailable)
                    with self.assertRaises(codex_review.ReviewError) as completed:
                        codex_review._result(json.dumps(event) + '\n' + json.dumps({'type': 'turn.completed'}), 1)
                    self.assertNotIsInstance(completed.exception, codex_review.ModelUnavailable)

    def test_other_cli_failures_do_not_signal_model_fallback(self):
        messages = [
            {'type': 'error', 'status': 429, 'error': {'code': 'model_not_found'}},
            {'type': 'error', 'status': 401, 'error': {'code': 'model_not_found'}},
            {'type': 'error', 'status': 400, 'error': {'code': 'invalid_api_key'}},
            {'type': 'error', 'status': '400', 'error': {'code': 'model_not_found'}},
        ]
        events = [{'type': 'error', 'message': json.dumps(value)} for value in messages]
        events.extend([{'type': 'error', 'message': 'model_not_found'},
                       {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(messages[0])}},
                       {'type': 'turn.failed', 'error': {'message': 'timeout'}},
                       'not-json'])
        for event in events:
            with self.subTest(event=event):
                raw = event if isinstance(event, str) else json.dumps(event)
                with self.assertRaises(codex_review.ReviewError) as raised:
                    codex_review._result(raw, 1)
                self.assertNotIsInstance(raised.exception, codex_review.ModelUnavailable)
        unavailable = {'type': 'error', 'message': {'type': 'error', 'status': 404,
                       'error': {'code': 'model_not_found'}}}
        conflicting = '\n'.join(json.dumps(event) for event in (unavailable, events[0]))
        with self.assertRaises(codex_review.ReviewError) as raised:
            codex_review._result(conflicting, 1)
        self.assertNotIsInstance(raised.exception, codex_review.ModelUnavailable)

    def test_codex_rejects_tool_items_or_incomplete_turn(self):
        message = {'type':'item.completed', 'item':{'type':'agent_message','text':'{"ok":true}'}}
        done = {'type':'turn.completed'}
        tool = {'type':'item.started', 'item':{'type':'command_execution'}}
        self.assertEqual(codex_review._result('\n'.join(map(json.dumps, [message, done])), 0), {'ok':True})
        with self.assertRaisesRegex(codex_review.ReviewError, 'unexpected item'):
            codex_review._result('\n'.join(map(json.dumps, [tool, message, done])), 0)
        with self.assertRaisesRegex(codex_review.ReviewError, 'final answer'):
            codex_review._result(json.dumps(message), 0)
