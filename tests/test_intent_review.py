import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from model import codex_review
from workbench import cli, intent_review, public_reference


class IntentReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.task = self.root / 'task.json'
        self.request = self.root / 'issue.txt'
        self.request.write_bytes(b'Resume a failed job.\nAllow 3 tries and preserve its results.\n')
        self.claim = {'text': 'Resume the job after a failure.', 'citations': ['U1']}

    def run_review(self, mode, answer, **options):
        evidence = intent_review.collect(mode=mode, request=self.request, out=self.root / 'evidence.json', **options)
        return intent_review.review(evidence=evidence['path'], sha256=evidence['sha256'], model='gpt-test',
                                    out=self.root / 'review.json', reviewer=lambda *_: answer)

    def test_sharpen_keeps_exact_request_and_existing_task_criteria(self):
        answer = {key: [self.claim] for key in intent_review.SHARPEN}
        answer.update(experiments=[{'choice': 'Avoid duplicate work', 'setup': 'Interrupt and resume.',
                                    'observation': 'Count starts.', 'decision_rule': 'One start.',
                                    'citations': ['U2']}], draft='Use a persisted completion receipt.')
        report = self.run_review('sharpen', answer)
        shown = intent_review.show(report['path'], report['sha256'])
        self.assertEqual(shown['original'].encode(), self.request.read_bytes())
        self.assertIn(answer['draft'], shown['prompt'])
        data = {'schema': cli.SCHEMA, 'repo': str(self.root), 'base_commit': 'a' * 40,
                'criteria': [{'id': 'one', 'text': 'No duplicate work'}]}
        cli.write(self.task, data)
        with patch.object(cli, 'sha', return_value=data['base_commit']):
            binding = intent_review.attach(self.task, report['path'], report['sha256'])
            self.assertEqual(cli.read(self.task), {**data, 'intent_review': binding})
            self.task.with_name('autopilot.json').touch()
            with self.assertRaisesRegex(ValueError, 'before starting'):
                intent_review.attach(self.task, report['path'], report['sha256'])
        self.request.write_text('A different request.')
        with self.assertRaisesRegex(ValueError, 'differs from the original'):
            intent_review.task_context(cli.read(self.task), self.task)

    def test_bro_rejects_a_different_retry_count(self):
        with self.assertRaisesRegex(ValueError, 'retain the original numbers'):
            self.run_review('bro', {'restatement': 'Retry twice.'})

    def test_goal_restatement_uses_only_original_input_citations(self):
        answer = {key: [self.claim] for key in intent_review.WORDS}
        answer['unknowns'] = [{**self.claim, 'citations': ['S1']}]
        with self.assertRaisesRegex(ValueError, 'exact evidence IDs'):
            self.run_review('in-your-own-words', answer)

    def reframe_fixture(self):
        terms = ['receipt', 'journal', 'restart', 'attempt', 'checkpoint', 'replay', 'timeout', 'state']
        quotes = {term: f'A {term} records one part of the operation.' for term in terms}
        url = 'https://example.com/docs'
        refs = self.root / 'references.json'
        refs.write_text(json.dumps([{'url': url, 'anchors': terms}]))
        rows = [
            {'term': term, 'meaning': f'The meaning of {term}.', 'use': 'Check the user request.',
             'citations': ['U1'], 'reference': 'R1', 'quote': quotes[term]}
            for term in terms
        ]
        rows[-1]['quote'] = 'The state is always final.'
        answer = {'interpretations': [], 'focus': [self.claim], 'terms': rows, 'omissions': [],
                  'neighboring_fields': [{'field': 'Storage', 'transfer': 'Record completion.',
                                          'limit': 'A receipt may be absent.', 'terms': ['receipt']},
                                         {'field': 'Scheduling', 'transfer': 'Check the retry count.',
                                          'limit': 'Some outcomes are unknown.', 'terms': ['attempt']}],
                  'draft': 'Use a state guarantee.'}
        def fetch(link):
            return {'requested_url': link, 'url': link,
                    'text': ' '.join(quotes.values()).replace('receipt', 'receipt\u200b')}

        return answer, refs, fetch, url, quotes

    def test_reframe_withholds_draft_and_unverified_links(self):
        answer, refs, fetch, url, _ = self.reframe_fixture()
        report = self.run_review('reframe', answer, references=refs, fetch=fetch)
        shown = intent_review.show(report['path'], report['sha256'])
        self.assertEqual(shown['status'], 'partial')
        self.assertNotIn('prompt', shown)
        self.assertNotIn('draft', shown['answer'])
        self.assertEqual(shown['answer']['omitted_terms'], ['state'])
        self.assertEqual(len(shown['answer']['terms']), 7)
        self.assertTrue(all(term['url'] == url for term in shown['answer']['terms']))
        cli.write(self.task, {'schema': cli.SCHEMA, 'repo': str(self.root), 'base_commit': 'a' * 40})
        with patch.object(cli, 'sha', return_value='a' * 40):
            with self.assertRaisesRegex(ValueError, 'partial terminology evidence cannot be attached'):
                intent_review.attach(self.task, report['path'], report['sha256'])

    def test_reframe_retries_an_unverified_term_against_the_same_excerpts(self):
        answer, refs, fetch, _, quotes = self.reframe_fixture()
        evidence = intent_review.collect(mode='reframe', request=self.request, out=self.root / 'evidence.json',
                                         references=refs, fetch=fetch)
        calls = []

        def reviewer(prompt, model, remaining):
            calls.append(prompt)
            if len(calls) == 1:
                return answer
            last = {**answer['terms'][-1], 'quote': quotes['state']}
            return {**answer, 'terms': [*answer['terms'][:-1], last]}

        report = intent_review.review(evidence=evidence['path'], sha256=evidence['sha256'], model='gpt-test',
                                      out=self.root / 'review.json', reviewer=reviewer)
        self.assertEqual(len(calls), 2)
        self.assertIn('following terms lacked an exact quote: ["state"]', calls[1])
        self.assertTrue(calls[1].endswith(calls[0]))
        self.assertEqual(report['status'], 'complete')
        shown = intent_review.show(report['path'], report['sha256'])
        self.assertEqual(len(shown['answer']['terms']), 8)
        self.assertIn(answer['draft'], shown['prompt'])

    def test_public_references_filter_ineligible_urls_and_local_addresses(self):
        for url in ('http://example.com', 'https://user@example.com', 'https://example.com:8080'):
            with self.subTest(url=url), self.assertRaises(public_reference.ReferenceError):
                public_reference._target(url)
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('127.0.0.1', 443))]
        with patch.object(socket, 'getaddrinfo', return_value=addresses):
            with self.assertRaisesRegex(public_reference.ReferenceError, 'public addresses'):
                public_reference._addresses('example.com')

    def test_discovery_opens_only_observed_pages_and_keeps_verified_anchors(self):
        good, missing, invented = [f'https://example.com/{name}' for name in ('one', 'two', 'three')]
        alias = 'https://docs.example.com/one'
        opened = []

        def search(prompt, model, timeout):
            self.assertIn('public queue recovery', prompt)
            self.assertNotIn('Allow 3 tries', prompt)
            self.assertEqual((model, timeout), ('gpt-test', 9))
            return {'searches': [{'query': 'public queue recovery', 'urls': [good, missing, alias]}],
                    'answer': {'references': [
                        {'url': good + '#terms', 'anchors': ['delivery receipt', 'imaginary anchor']},
                        {'url': missing, 'anchors': ['acknowledgement']},
                        {'url': invented, 'anchors': ['delivery receipt']},
                        {'url': alias, 'anchors': ['delivery receipt']},
                    ]}}

        def fetch(url):
            opened.append(url)
            return {'url': good if url == alias else url, 'requested_url': url,
                    'text': 'Replay a delivery receipt.' if url != missing else 'This entry changed.'}

        output = self.root / 'evidence.json'
        result = intent_review.collect(mode='reframe', request=self.request, out=output,
                                       search_topic='public queue recovery', search_model='gpt-test',
                                       search_timeout=9, search=search, fetch=fetch)
        packet = json.loads(output.read_bytes())
        self.assertEqual(opened, [good, missing, alias])
        self.assertEqual(result['references_opened'], 1)
        self.assertEqual(list(packet['references']['R1']['excerpts']), ['delivery receipt'])
        self.assertEqual([row['status'] for row in packet['discovery']['candidates']],
                         ['opened', 'unverified', 'not-in-search-results', 'duplicate-source'])
        self.assertEqual(packet['user_lines']['U2'], self.request.read_text().splitlines()[1])

    def test_search_and_explicit_manifest_are_alternatives(self):
        defaults = {'mode': 'reframe', 'request': self.request, 'out': self.root / 'evidence.json'}
        with self.assertRaisesRegex(ValueError, 'either a primary reference manifest'):
            intent_review.collect(**defaults, references='some.json', search_topic='public topic')
        with self.assertRaisesRegex(ValueError, 'requires a public topic and a model'):
            intent_review.collect(**defaults, search_model='gpt-test')

    def test_search_requires_a_completed_search_with_actual_result_urls(self):
        url = 'https://example.com/reference'
        event = {'type': 'item.completed', 'item': {'type': 'web_search', 'query': 'some search',
                 'action': {'type': 'search'}, 'results': [{'url': url}, {'url': url}]}}
        message = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"references": []}'}}
        done = {'type': 'turn.completed'}

        def jsonl(*rows):
            return '\n'.join(map(json.dumps, rows))

        with self.assertRaisesRegex(codex_review.ReviewError, 'did not run a web search'):
            codex_review._result(jsonl(message, done), 0, search=True)
        with self.assertRaisesRegex(codex_review.ReviewError, 'unexpected item'):
            codex_review._result(jsonl(event, message, done), 0)
        found = codex_review._result(jsonl(event, message, done), 0, search=True)
        self.assertEqual(found['searches'], [{'query': 'some search', 'urls': [url]}])

    def test_quote_has_to_contain_the_term_as_a_word(self):
        packet = {'user_lines': {'U1': 'Preserve a durable lock.'}, 'code': {},
                  'references': {'R1': {'url': 'https://example.com/clock',
                                        'excerpts': {'clock': 'A clock may drift.'}}}}
        answer = {'interpretations': [], 'focus': [self.claim], 'omissions': [], 'draft': 'Add a lock.',
                  'neighboring_fields': [], 'terms': [{'term': 'lock', 'meaning': 'A held exclusion.',
                  'use': 'Guard delivery.', 'citations': ['U1'], 'reference': 'R1', 'quote': 'A clock may drift.'}]}
        checked = intent_review._reframe(packet, answer)
        self.assertEqual(checked['terms'], [])
        self.assertEqual(checked['omitted_terms'], ['lock'])
