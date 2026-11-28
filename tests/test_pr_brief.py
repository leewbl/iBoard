import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_delivery
import test_publish
from workbench import cli, pr_brief, publish


class ReadyGitHub(test_publish.FakeGitHub):
    def create(self, repo, branch, task_id, data):
        number = super().create(repo, branch, task_id, data)
        self.existing[0].update(title=data['_prepared_pr_title'], isDraft=False,
                                baseRefName=data['_prepared_pr_base'])
        return number

    def update_body(self, repo, number, body, *, title=None):
        super().update_body(repo, number, body)
        self.existing[0]['title'] = title


class PrBriefTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_delivery.DeliveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.task, self.head = self.fixture.task, self.fixture.head
        self.root = self.task.parent
        self.repo = 'alice/project'
        data = cli.read(self.task)
        data['delivery_repo'] = self.repo
        data['impact_review'] = 'runtime-v1'
        review = next(row for row in data['evidence'] if row['kind'] == 'independent-review')
        report = Path(review['path'])
        details = json.loads(report.read_text())
        details['impact'] = {'surface': 'Application read', 'invariant': 'Existing reads continue to return a result.',
                             'observed': 'The verified path completed.\nThe independent test returned PASS.',
                             'path': 'app', 'line': 1, 'evidence': ['R1']}
        report.write_text(json.dumps(details), encoding='utf-8')
        review['sha256'] = cli.digest(report)
        cli.write(self.task, data)
        self.manifest = self.root / 'manifest.json'
        self.spec = {'type': 'fix', 'scope': 'reader', 'subject': 'Preserve fixed output',
                     'why': 'The reader returned the wrong value.',
                     'changes': ['Return the verified value and keep its callers unchanged.'],
                     'tradeoff': 'Keeping the old value would leave the observed failure unresolved.',
                     'symbol': {'path': 'app', 'name': 'fixed'}}
        self.manifest.write_text(json.dumps(self.spec), encoding='utf-8')
        self.github = ReadyGitHub()
        self.github.base = data['base_commit']

    def prepare(self):
        return pr_brief.prepare(self.task, self.repo, self.manifest, self.root / 'brief.json')

    def test_ready_pr_contains_the_verified_short_brief(self):
        receipt = self.prepare()
        result = pr_brief.open_verified(self.task, self.repo, receipt['path'], receipt['sha256'], self.github)
        self.assertEqual(result['pr'], 7)
        pr = self.github.existing[0]
        self.assertEqual(pr['title'], 'fix(reader): Preserve fixed output')
        self.assertFalse(pr['isDraft'])
        body = pr['body']
        self.assertEqual([line for line in body.splitlines() if line.startswith('## ')],
                         ['## Why', '## What changed', '## Scope', '## Tradeoffs', '## Blast Radius', '## Verification'])
        self.assertLess(len(body.splitlines()), 40)
        self.assertIn(f'/blob/{self.head}/app#L1', body)
        self.assertIn('First report SHA-256', body)
        self.assertIn('Resolves #42', body)
        self.assertNotIn('[stdout]', body)
        self.assertNotIn(str(self.root), body)
        self.assertEqual(self.github.events, [('push', 'workbench/issue-1', self.head), ('create', 'workbench/issue-1')])

    def test_retry_repairs_the_existing_ready_title_without_creating_a_duplicate(self):
        receipt = self.prepare()
        args = self.task, self.repo, receipt['path'], receipt['sha256'], self.github
        pr_brief.open_verified(*args)
        self.github.existing[0]['title'] = 'Old title'
        self.github.events.clear()
        result = pr_brief.open_verified(*args)
        self.assertEqual(result['pr'], 7)
        self.assertEqual(self.github.events, [('body', 7)])
        self.assertEqual(self.github.existing[0]['title'], receipt['title'])

    def test_existing_draft_is_not_changed_by_a_ready_handoff(self):
        receipt = self.prepare()
        args = self.task, self.repo, receipt['path'], receipt['sha256'], self.github
        pr_brief.open_verified(*args)
        self.github.existing[0]['isDraft'] = True
        self.github.events.clear()
        with self.assertRaisesRegex(publish.PublishError, 'is a draft'):
            pr_brief.open_verified(*args)
        self.assertEqual(self.github.events, [])

    def test_scope_and_review_are_bound_before_any_remote_write(self):
        dishonest = {**self.spec, 'symbol': {'path': 'app', 'name': 'missing'}, 'subject': 'Add missing'}
        self.manifest.write_text(json.dumps(dishonest), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'symbol was not observed'):
            self.prepare()
        self.manifest.write_text(json.dumps(self.spec), encoding='utf-8')
        receipt = self.prepare()
        path = Path(receipt['path'])
        path.write_text(path.read_text().replace('Preserve fixed output', 'Alter fixed output'), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'evidence digest changed'):
            pr_brief.open_verified(self.task, self.repo, receipt['path'], receipt['sha256'], self.github)
        self.assertEqual(self.github.events, [])

    def test_stacked_pr_binds_the_parent_and_refuses_base_drift(self):
        class StackGithub(ReadyGitHub):
            parent_head = None

            def pull(self, repo, number):
                return {'number': 3, 'state': 'OPEN', 'isCrossRepository': False, 'headRefName': 'workbench/parent',
                        'headRefOid': self.parent_head or self.base}

            def branch(self, repo, branch):
                if branch != 'workbench/parent':
                    raise AssertionError('stacked PR checked the wrong base')
                return self.parent_head or self.base

            def pulls(self, repo, branch, *, base='main'):
                if base is not None:
                    raise AssertionError('stacked PR lookup hid branches with other bases')
                return super().pulls(repo, branch)

        forge = StackGithub()
        forge.base = self.github.base
        self.manifest.write_text(json.dumps({**self.spec, 'parent_pr': 3}), encoding='utf-8')
        receipt = pr_brief.prepare(self.task, self.repo, self.manifest, self.root / 'stack.json', github=forge)
        args = self.task, self.repo, receipt['path'], receipt['sha256'], forge
        result = pr_brief.open_verified(*args)
        self.assertEqual(result['pr'], 7)
        self.assertEqual(forge.existing[0]['baseRefName'], 'workbench/parent')
        self.assertIn('[PR #3](https://github.com/alice/project/pull/3)', forge.existing[0]['body'])
        forge.events.clear()
        forge.parent_head = '0' * 40
        with self.assertRaisesRegex(publish.PublishError, 'PR stack base'):
            pr_brief.open_verified(*args)
        self.assertEqual(forge.events, [])
        forge.parent_head = None
        forge.existing[0]['baseRefName'] = 'main'
        with self.assertRaisesRegex(publish.PublishError, 'different base branch'):
            pr_brief.open_verified(*args)
        self.assertEqual(forge.events, [])

    def test_cli_uses_ready_default_and_writes_the_proposed_title(self):
        command = []
        body = []

        def capture(args, **_):
            command.extend(args)
            body.append(Path(args[args.index('--body-file') + 1]).read_text())
            return subprocess.CompletedProcess(args, 0, 'https://github.com/alice/project/pull/7', '')

        with patch.object(publish.subprocess, 'run', side_effect=capture):
            result = publish.Publisher().create(self.repo, 'workbench/issue-1', 'issue-1',
                               {'_prepared_pr_body': '## Why\nUse the verified result.', '_prepared_pr_title': 'fix(reader): Read',
                                '_prepared_pr_base': 'workbench/parent'})
        self.assertEqual(result, 7)
        self.assertEqual(command[command.index('--title') + 1], 'fix(reader): Read')
        self.assertEqual(command[command.index('--base') + 1], 'workbench/parent')
        self.assertNotIn('--draft', command)
        self.assertEqual(body, ['## Why\nUse the verified result.'])


if __name__ == '__main__':
    unittest.main()
