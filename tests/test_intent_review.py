import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

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

    def test_reframe_withholds_draft_and_unverified_links(self):
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
        report = self.run_review('reframe', answer, references=refs,
                                 fetch=lambda link: {'requested_url': link, 'url': link,
                                                     'text': ' '.join(quotes.values()).replace('receipt', 'receipt\u200b')})
        shown = intent_review.show(report['path'], report['sha256'])
        self.assertEqual(shown['status'], 'partial')
        self.assertNotIn('prompt', shown)
        self.assertNotIn('draft', shown['answer'])
        self.assertEqual(shown['answer']['omitted_terms'], ['state'])
        self.assertEqual(len(shown['answer']['terms']), 7)
        self.assertTrue(all(term['url'] == url for term in shown['answer']['terms']))
        cli.write(self.task, {'schema': cli.SCHEMA, 'repo': str(self.root), 'base_commit': 'a' * 40})
        with patch.object(cli, 'sha', return_value='a' * 40):
            with self.assertRaisesRegex(ValueError, 'differs from the original'):
                intent_review.attach(self.task, report['path'], report['sha256'])

    def test_public_references_filter_ineligible_urls_and_local_addresses(self):
        for url in ('http://example.com', 'https://user@example.com', 'https://example.com:8080'):
            with self.subTest(url=url), self.assertRaises(public_reference.ReferenceError):
                public_reference._target(url)
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('127.0.0.1', 443))]
        with patch.object(socket, 'getaddrinfo', return_value=addresses):
            with self.assertRaisesRegex(public_reference.ReferenceError, 'public addresses'):
                public_reference._addresses('example.com')
