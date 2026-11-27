import json
import os
from pathlib import Path
import tempfile
import unittest

from workbench import cli, delivery, recall


class RecallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q', '-b', 'main')
        cli.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                'commit', '--allow-empty', '-qm', 'Initial verification record')
        self.one = self.chats('first', [
            ('user', '2026-11-26', 'Verify the Interrogate coverage requirement.'),
            ('assistant', '2026-11-26', 'Interrogate PR #5 already merged.'),
            ('user', '2026-10-01', 'Old Interrogate note'),
            ('assistant', '2026-11-26', 'Interrogate from another repo', self.root),
            ('user', '2026-11-26', [{'type': 'tool_result', 'content': 'Interrogate ignored tool output'}]),
        ])
        self.two = self.chats('second', [
            ('user', '2026-11-26', 'Interrogate needs a second offline run.'),
            ('assistant', '2026-11-26', 'Use the same tested commit.'),
        ])
        os.utime(self.one, (100, 100))
        os.utime(self.two, (200, 200))

    def chats(self, session, messages):
        path = self.root / f'{session}.jsonl'
        events = [{'uuid': f'line-{index}', 'sessionId': session, 'cwd': str(cwd[0] if cwd else self.repo),
                   'timestamp': f'{day}T12:00:00Z', 'type': role,
                   'message': {'role': role, 'content': content}}
                  for index, (role, day, content, *cwd) in enumerate(messages)]
        path.write_text('\n'.join(map(json.dumps, events)), encoding='utf-8')
        return path

    def collect(self, **options):
        return recall.collect(self.repo, self.root / 'recall.json', [self.one, self.two],
                              topic='Interrogate', until='2026-11-27T00:00:00Z',
                              forge='example/project', branch=['main'], **options)

    def test_scope_window_order_and_implicit_artifact(self):
        packet = cli.read(self.collect()['path'])
        self.assertEqual([row['id'] for row in packet['sessions']], ['second', 'first'])
        self.assertEqual(packet['artifacts'], ['branch:main', 'pr:5'])
        content = json.dumps(packet['history'])
        for value in ('Old Interrogate note', 'another repo', 'ignored tool output'):
            self.assertNotIn(value, content)

    def test_all_history_and_skip_current(self):
        packet = cli.read(self.collect(all_history=True, current_session='second')['path'])
        self.assertEqual([row['id'] for row in packet['sessions']], ['first'])
        self.assertIn('Old Interrogate note', json.dumps(packet['history']))

    def test_runtime_state_comes_from_github_and_digest_is_required(self):
        packet = self.collect()

        class GitHub:
            def pull(self, repo, number):
                if (repo, number) != ('example/project', 5):
                    raise delivery.DeliveryError('wrong lookup')
                return {'number': 5, 'state': 'OPEN', 'isDraft': True,
                        'headRefName': 'feature/verify', 'headRefOid': 'd' * 40}

        def review(prompt, model, timeout):
            self.assertIn('open PR #5 (draft)', prompt)
            self.assertIn('Interrogate PR #5 already merged.', prompt)
            return {'capsule': [{'text': 'A second run was requested.', 'citations': ['T1']}],
                    'threads': [{'text': 'Continue the coverage review.', 'citations': ['T3', 'L2'], 'live': 'L2'}],
                    'problems': [], 'next': {'text': 'Check both offline receipts.', 'citations': ['T1']}}

        options = {'model': 'gpt-test', 'reviewer': review, 'github': GitHub()}
        with self.assertRaisesRegex(ValueError, 'digest changed'):
            recall.summarize(packet['path'], '0' * 64, self.root / 'report.json', **options)
        report = recall.summarize(packet['path'], packet['sha256'], self.root / 'report.json', **options)
        rendered = recall.markdown(report['path'], report['sha256'])
        self.assertIn('[open PR #5 (draft)] Continue the coverage review.', rendered)
        self.assertIn('chat first:1', rendered)
        self.assertIn('## Next action', rendered)
        self.assertNotIn('already merged', rendered)

    def test_unknown_citations_and_forged_status_tags_are_refused(self):
        for message, refs, pattern in (
            ('Verify the work.', ['T2'], 'unknown or duplicate'),
            ('[merged #5] Verify the work.', ['T1'], 'may not supply'),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, pattern):
                recall._claims([{'text': message, 'citations': refs}], {'T1': {}}, 1)

    def test_multiple_chats_keep_source_citations_after_mining(self):
        third = self.chats('third', [('user', '2026-11-26', 'Interrogate still needs a test receipt.')])
        info = recall.collect(self.repo, self.root / 'evidence.json', [self.one, self.two, third],
                              topic='Interrogate', until='2026-11-27T00:00:00Z')

        def review(prompt, model, timeout):
            ids = json.loads(prompt.split('\n', 1)[1])
            if 'one chat' in prompt:
                return {'findings': [{'text': 'Receipt requested.', 'citations': [next(iter(ids))]}]}
            self.assertEqual(len(ids['findings']), 3)
            return {'capsule': [], 'threads': [], 'problems': [], 'next': ids['findings'][0]}

        result = recall.summarize(info['path'], info['sha256'], self.root / 'report.json',
                                  model='gpt-test', reviewer=review)
        packet = cli.read(result['path'])
        self.assertEqual({row['session'] for row in packet['calls'][:-1]}, {'first', 'second', 'third'})
        self.assertIn(packet['report']['next']['citations'][0], packet['citations'])


if __name__ == '__main__':
    unittest.main()
