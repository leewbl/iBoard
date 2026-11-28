from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from workbench import cli, investigate, teach_lesson
from workbench.prompt_profiles import PSTACK_COMMIT


class LessonMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.diagrams = []
        self.connections = []
        self.details = []
        self._open_details = []
        self._diagram = None

    def handle_starttag(self, tag, attrs):
        if tag == 'details':
            self._open_details.append('open' in dict(attrs))
        elif tag == 'svg':
            self._diagram = []
            self.connections.append(0)
            self.details.append(self._open_details[-1] if self._open_details else None)
        elif tag == 'path' and self._diagram is not None and 'marker-end' in dict(attrs):
            self.connections[-1] += 1

    def handle_data(self, data):
        if self._diagram is not None:
            self._diagram.append(data)

    def handle_endtag(self, tag):
        if tag == 'details':
            self._open_details.pop()
        elif tag == 'svg':
            self.diagrams.append(' '.join(self._diagram))
            self._diagram = None


class TeachLessonTests(unittest.TestCase):
    def setUp(self):
        self.history_commit = 'c28ea4f3b61d70ae9e204f2e091cb19a3ef940b5'
        self.prior = {
            'how': {'findings': [{'text': 'The request goes to the handler.', 'basis': 'observed',
                                  'citations': [{'id': 'S1', 'line': 47}]}], 'unknowns': []},
            'why': {'findings': [{'text': 'The return path may have been added to confirm completion.',
                                  'basis': 'inferred', 'citations': [{'id': 'H1'}]}],
                    'unknowns': ['The history gives no reason for the threshold.']},
        }
        self.lesson = {
            'summary': 'A handler processes a request and returns its result. <b>Literal markup</b>',
            'steps': [
                {'text': 'The router passes the request to the handler.', 'refs': ['how:1'],
                 'nodes': [{'id': 'router', 'label': 'Router'}, {'id': 'handler', 'label': 'Handler'}],
                 'edge': {'from': 'router', 'to': 'handler', 'label': 'request'}},
                {'text': 'The handler sends the result to persistence.', 'refs': ['how:1'],
                 'nodes': [{'id': 'store', 'label': 'Storage'}],
                 'edge': {'from': 'handler', 'to': 'store', 'label': 'persist'}},
                {'text': 'An acknowledgement returns to the router.', 'refs': ['how:1', 'why:1'], 'nodes': [],
                 'edge': {'from': 'store', 'to': 'router', 'label': 'acknowledge'}},
            ],
        }

    def test_accepts_a_progressive_lesson_and_a_simple_text_lesson(self):
        self.assertEqual(teach_lesson.validate(self.lesson, self.prior), self.lesson)
        first, second, third = self.lesson['steps']
        cumulative = {**self.lesson, 'steps': [first, {**second, 'nodes': [*first['nodes'], *second['nodes']]}, third]}
        self.assertEqual(teach_lesson.validate(cumulative, self.prior), self.lesson)
        cumulative['steps'][1]['nodes'][0] = {**first['nodes'][0], 'label': 'Changed'}
        with self.assertRaisesRegex(teach_lesson.LessonError, 'earlier nodes unchanged'):
            teach_lesson.validate(cumulative, self.prior)
        text_only = {'summary': 'The handler returns the result.', 'steps': []}
        self.assertEqual(teach_lesson.validate(text_only, self.prior), text_only)

    def test_rejects_invalid_steps_and_unavailable_findings(self):
        first, second, third = self.lesson['steps']
        for label, steps in (
            ('too few starting nodes', [{**first, 'nodes': first['nodes'][:1]}]),
            ('adds two nodes at once', [first, {**second, 'nodes': [*second['nodes'],
                                                                   {'id': 'other', 'label': 'Other'}]}]),
            ('reuses a node id', [first, {**second, 'nodes': [first['nodes'][0]]}]),
            ('leaves the new node disconnected', [first, {**second, 'edge': {'from': 'handler', 'to': 'router',
                                                                              'label': 'return'}}]),
            ('points to a future node', [first, {**second, 'edge': {**second['edge'], 'to': 'future'}}]),
            ('repeats a directed edge', [first, second, {**third, 'edge': {**first['edge'], 'label': 'again'}}]),
            ('contains a self loop', [first, {**third, 'edge': {**third['edge'], 'from': 'router'}}]),
            ('cites an unavailable finding', [{**first, 'refs': ['why:2']}]),
            ('uses a zero-based reference', [{**first, 'refs': ['how:0']}]),
        ):
            with self.subTest(label=label), self.assertRaises(teach_lesson.LessonError):
                teach_lesson.validate({**self.lesson, 'steps': steps}, self.prior)

    def test_renderer_builds_the_diagram_and_displays_original_uncertainty(self):
        report = {
            'schema': cli.SCHEMA, 'pstack_commit': PSTACK_COMMIT, 'mode': 'teach', 'status': 'complete',
            'question': 'How does the request flow?', 'commit': 'b' * 40,
            'evidence': {'S1': {'kind': 'source', 'path': 'src/flow.py', 'commit': 'b' * 40,
                                'start': 47, 'end': 47, 'blob_sha256': 'e' * 64, 'text': '47: run_handler()'},
                         'H1': {'kind': 'history', 'commit': self.history_commit, 'text': 'Keep the return path.'}},
            'stages': {**self.prior, 'teach': {'lesson': self.lesson, 'unknowns': [],
                                              'findings': self.prior['how']['findings']}},
        }
        document = teach_lesson.render(report)
        markup = LessonMarkup()
        markup.feed(document)
        self.assertEqual(len(markup.diagrams), 3)
        self.assertEqual(markup.details, [True, False, False])
        self.assertEqual(markup.connections, [1, 2, 3])
        for diagram in markup.diagrams:
            self.assertIn('Router', diagram)
            self.assertIn('Handler', diagram)
        self.assertNotIn('Storage', markup.diagrams[0])
        self.assertIn('Storage', markup.diagrams[1])
        self.assertIn('Storage', markup.diagrams[2])
        self.assertRegex(document, r'Storage[^<]*Router[^<]*acknowledge')
        self.assertIn('&lt;b&gt;Literal markup&lt;/b&gt;', document)
        self.assertNotIn('<b>Literal markup</b>', document)
        self.assertIn(self.prior['why']['findings'][0]['text'], document)
        self.assertIn('inferred', document.lower())
        self.assertIn(self.prior['why']['unknowns'][0], document)
        self.assertIn('src/flow.py:47', document)
        self.assertIn(self.history_commit[:7], document)

    def test_investigation_opt_in_persists_only_a_valid_lesson(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()

            git('init', '-q')
            source = repo / 'flow.py'
            source.write_text('run_handler()\n')
            git('add', 'flow.py')
            git('-c', 'user.email=test@example.invalid', '-c', 'user.name=Test',
                'commit', '-qm', 'Add the request handler')
            head = git('rev-parse', 'HEAD')
            source.write_text('uncommitted_change()\n')

            def result(presentation):
                def model(workspace, prompt, timeout, *, read_only, file_tools_only):
                    self.assertTrue(read_only and file_tools_only)
                    self.assertFalse(any(workspace.iterdir()))
                    self.assertNotIn('uncommitted_change', prompt)
                    inputs = json.loads(prompt.rsplit('\n', 1)[-1])
                    if inputs['prior']:
                        if 'Also return lesson' in prompt:
                            self.assertEqual(set(inputs['lesson_refs']), {'how:1', 'why:1'})
                            self.assertEqual(inputs['lesson_refs']['how:1'], inputs['prior']['how']['findings'][0])
                        answer = {'findings': [{'text': 'The handler processes the request.', 'basis': 'observed',
                                               'citations': [{'id': 'S1', 'line': 1}]}], 'unknowns': [],
                                  'lesson': presentation}
                    else:
                        name = 'how' if 'S1' in inputs['evidence'] else 'why'
                        answer = deepcopy(self.prior[name])
                        if name == 'how':
                            answer['findings'][0]['citations'][0]['line'] = 1
                    answer['status'] = 'ready'
                    return {'exit_code': 0, 'timed_out': False, 'stdout': json.dumps(answer)}

                return model

            options = (repo, head, 'How does the request flow?', ['flow.py:1'], 'teach', 30)
            regular = investigate.investigate(*options, model=result(self.lesson))
            self.assertEqual(regular['status'], 'complete', regular.get('reason'))
            self.assertNotIn('lesson', regular['stages']['teach'])

            with_lesson = investigate.investigate(*options, lesson=True, model=result(self.lesson))
            self.assertEqual(with_lesson['status'], 'complete', with_lesson.get('reason'))
            self.assertEqual(with_lesson['stages']['teach']['lesson'], self.lesson)

            invalid = {**self.lesson, 'summary': ''}
            rejected = investigate.investigate(*options, lesson=True, model=result(invalid))
            self.assertEqual(rejected['status'], 'blocked')
            self.assertNotIn('teach', rejected['stages'])
            self.assertEqual(set(rejected['model_runs']), {'how', 'why', 'teach', 'teach-retry'})

            def repair(workspace, prompt, timeout, **kwargs):
                presentation = self.lesson if 'Correct this diagram validation error:' in prompt else invalid
                return result(presentation)(workspace, prompt, timeout, **kwargs)

            repaired = investigate.investigate(*options, lesson=True, model=repair)
            self.assertEqual(repaired['status'], 'complete', repaired.get('reason'))
            self.assertIn('rejected_output', repaired['model_runs']['teach'])
            self.assertEqual(repaired['stages']['teach']['lesson'], self.lesson)

            def repair_source(workspace, prompt, timeout, **kwargs):
                response = result(self.lesson)(workspace, prompt, timeout, **kwargs)
                inputs = json.loads(prompt.rsplit('\n', 1)[-1])
                if not inputs['prior'] and 'S1' in inputs['evidence'] and 'Correct this' not in prompt:
                    answer = json.loads(response['stdout'])
                    answer['findings'][0]['citations'][0]['line'] = 999
                    response['stdout'] = json.dumps(answer)
                return response

            source_repaired = investigate.investigate(*options, lesson=True, model=repair_source)
            self.assertEqual(source_repaired['status'], 'complete', source_repaired.get('reason'))
            self.assertIn('how-retry', source_repaired['model_runs'])
            self.assertNotIn('lesson', source_repaired['stages']['how'])
            self.assertEqual(source.read_text(), 'uncommitted_change()\n')
