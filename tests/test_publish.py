from pathlib import Path
import hashlib
import base64
import json
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import zlib

from workbench import publish, cli
from test_runner import tiny_png


def png_chunk(kind, payload):
    return (len(payload).to_bytes(4, 'big') + kind + payload +
            zlib.crc32(kind + payload).to_bytes(4, 'big'))


def palette_png_with_metadata():
    header = (2).to_bytes(4, 'big') + (1).to_bytes(4, 'big') + bytes([8, 3, 0, 0, 0])
    profile = bytearray(132); profile[:4] = (132).to_bytes(4, 'big'); profile[36:40] = b'acsp'
    chunks = [(b'IHDR', header), (b'iCCP', b'sRGB\x00\x00' + zlib.compress(bytes(profile))),
              (b'gAMA', (45455).to_bytes(4, 'big')), (b'PLTE', b'\xff\x00\x00\x00\xff\x00'),
              (b'tRNS', b'\x00\x80'), (b'tEXt', b'Author\x00private-name'),
              (b'zTXt', b'Comment\x00\x00' + zlib.compress(b'private comment')),
              (b'iTXt', b'Description\x00\x00\x00\x00\x00private description'),
              (b'eXIf', b'II\x2a\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00private-exif'),
              (b'tIME', b'\x07\xe8\x01\x02\x03\x04\x05'),
              (b'IDAT', zlib.compress(b'\x00\x00\x01')), (b'IEND', b'')]
    return b'\x89PNG\r\n\x1a\n' + b''.join(png_chunk(kind, payload) for kind, payload in chunks)


class FakeGitHub:
    def __init__(self):
        self.events = []
        self.remote = None
        self.base = 'base'
        self.existing = []
    def login(self): return 'alice'
    def repository(self, repo): return {'fork': True, 'owner': {'login': 'alice'}, 'default_branch': 'main'}
    def branch(self, repo, branch): return self.base
    def remote_head(self, repo, branch): return self.remote
    def push_new(self, workspace, repo, branch, head): self.events.append(('push', branch, head)); self.remote = head
    def push_update(self, workspace, repo, branch, old, head):
        self.events.append(('update', branch, old, head)); self.remote = head
        self.existing[0]['headRefOid'] = head
    def update_body(self, repo, number, body):
        self.events.append(('body', number))
        self.existing[0]['body'] = body
    def pulls(self, repo, branch): return self.existing
    def create(self, repo, branch, task_id, data):
        self.events.append(('create', branch))
        owner, name = repo.split('/')
        head = self.remote_head(repo, branch)
        self.existing = [{'number': 7, 'headRefName': branch, 'headRefOid': head,
                          'baseRefName': 'main', 'baseRefOid': self.base, 'isCrossRepository': False,
                          'headRepository': {'name': name}, 'headRepositoryOwner': {'login': owner},
                          'body': data['_prepared_pr_body']}]
        return 7


class PublishTests(unittest.TestCase):
    @staticmethod
    def pull(number, head):
        return {'number': number, 'headRefName': 'workbench/case', 'headRefOid': head,
                'baseRefName': 'main', 'baseRefOid': 'base', 'isCrossRepository': False,
                'headRepository': {'name': 'fork'}, 'headRepositoryOwner': {'login': 'alice'}, 'body': ''}
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.task = Path(self.tmp.name) / "task.json"
        self.task.touch()
        body = patch.object(publish, '_pr_body', side_effect=lambda data, head: '\n'.join(
            str(item['sha256']) for item in data.get('evidence', []) if item.get('commit') == head))
        body.start()
        self.addCleanup(body.stop)

    def test_publishes_only_after_local_evidence_and_fork_checks(self):
        fake = FakeGitHub()
        record = {'id': 'case', 'repo': '/tmp/work', 'base_commit': 'base'}
        with patch.object(publish.cli, 'read', return_value=record), patch.object(publish.cli, 'gate', return_value=('head', [])), patch.object(publish.delivery, '_independent_verification'):
            result = publish.publish(self.task, 'alice/fork', fake)
        self.assertEqual(result['pr'], 7)
        self.assertEqual(fake.events, [('push', 'workbench/case', 'head'), ('create', 'workbench/case')])

    def test_rejects_stale_base_without_writes(self):
        fake = FakeGitHub(); fake.base = 'different'
        record = {'id': 'case', 'repo': '/tmp/work', 'base_commit': 'base'}
        with patch.object(publish.cli, 'read', return_value=record), patch.object(publish.cli, 'gate', return_value=('head', [])), patch.object(publish.delivery, '_independent_verification'):
            with self.assertRaises(publish.PublishError): publish.publish(self.task, 'alice/fork', fake)
        self.assertEqual(fake.events, [])

    def test_never_overwrites_existing_remote_branch(self):
        fake = FakeGitHub(); fake.remote = 'other'
        record = {'id': 'case', 'repo': '/tmp/work', 'base_commit': 'base'}
        with patch.object(publish.cli, 'read', return_value=record), patch.object(publish.cli, 'gate', return_value=('head', [])), patch.object(publish.delivery, '_independent_verification'):
            with self.assertRaises(publish.PublishError): publish.publish(self.task, 'alice/fork', fake)
        self.assertEqual(fake.events, [])

    def test_reuses_matching_pr(self):
        fake = FakeGitHub(); fake.remote = 'head'; fake.existing = [self.pull(8, 'head')]
        record = {'id': 'case', 'repo': '/tmp/work', 'base_commit': 'base'}
        with patch.object(publish.cli, 'read', return_value=record), patch.object(publish.cli, 'gate', return_value=('head', [])), patch.object(publish.delivery, '_independent_verification'):
            result = publish.publish(self.task, 'alice/fork', fake)
        self.assertEqual(result['pr'], 8); self.assertEqual(fake.events, [])

    def test_republish_updates_only_the_expected_pr_and_head(self):
        old, head = 'a' * 40, 'b' * 40
        fake = FakeGitHub(); fake.remote = old
        fake.existing = [self.pull(8, old)]
        record = {'id': 'case', 'repo': '/tmp/work', 'base_commit': 'base', 'evidence': [
            {'kind': 'test-result', 'commit': old, 'sha256': 'old'},
            {'kind': 'test-result', 'commit': head, 'sha256': 'new'}]}
        with patch.object(publish.cli, 'read', return_value=record), patch.object(publish.cli, 'gate', return_value=(head, [])), patch.object(publish.delivery, '_independent_verification'):
            result = publish.republish(self.task, 'alice/fork', 8, old, fake)
        self.assertEqual(result['head'], head)
        self.assertEqual(fake.events, [('update', 'workbench/case', old, head), ('body', 8)])
        self.assertIn('new', fake.existing[0]['body'])
        self.assertNotIn('old', fake.existing[0]['body'])

    def test_republish_refuses_changed_remote_before_update(self):
        old, head = 'a' * 40, 'b' * 40
        fake = FakeGitHub(); fake.remote = 'c' * 40
        fake.existing = [self.pull(8, old)]
        record = {'id': 'case', 'repo': '/tmp/work', 'base_commit': 'base'}
        with patch.object(publish.cli, 'read', return_value=record), patch.object(publish.cli, 'gate', return_value=(head, [])), patch.object(publish.delivery, '_independent_verification'):
            with self.assertRaises(publish.PublishError): publish.republish(self.task, 'alice/fork', 8, old, fake)
        self.assertEqual(fake.events, [])

    def test_republish_rejects_cross_repository_pr(self):
        old, head = 'a' * 40, 'b' * 40
        fake = FakeGitHub(); fake.remote = old; fake.existing = [self.pull(8, old)]
        fake.existing[0]['isCrossRepository'] = True
        record = {'id': 'case', 'repo': '/tmp/work', 'base_commit': 'base', 'evidence': []}
        with patch.object(publish.cli, 'read', return_value=record), patch.object(publish.cli, 'gate', return_value=(head, [])), patch.object(publish.delivery, '_independent_verification'):
            with self.assertRaises(publish.PublishError): publish.republish(self.task, 'alice/fork', 8, old, fake)
        self.assertEqual(fake.events, [])

    def test_republish_resumes_description_after_push_succeeded(self):
        old, head = 'a' * 40, 'b' * 40
        fake = FakeGitHub(); fake.remote = head; fake.existing = [self.pull(8, head)]
        record = {'id': 'case', 'repo': '/tmp/work', 'base_commit': 'base', 'evidence': [
            {'kind': 'test-result', 'commit': head, 'sha256': 'new'}]}
        with patch.object(publish.cli, 'read', return_value=record), patch.object(publish.cli, 'gate', return_value=(head, [])), patch.object(publish.delivery, '_independent_verification'):
            publish.republish(self.task, 'alice/fork', 8, old, fake)
        self.assertEqual(fake.events, [('body', 8)])
        self.assertIn('new', fake.existing[0]['body'])

    def test_push_update_requires_ancestry_and_exact_lease(self):
        old, head = 'a' * 40, 'b' * 40
        ok = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(publish.subprocess, 'run', side_effect=[ok, ok]) as run:
            publish.Publisher().push_update('/tmp/repo', 'alice/fork', 'workbench/case', old, head)
        self.assertIn('--is-ancestor', run.call_args_list[0].args[0])
        self.assertIn(f'--force-with-lease=refs/heads/workbench/case:{old}', run.call_args_list[1].args[0])
        with patch.object(publish.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')) as run:
            with self.assertRaisesRegex(publish.PublishError, 'descend'):
                publish.Publisher().push_update('/tmp/repo', 'alice/fork', 'workbench/case', old, head)
        self.assertEqual(run.call_count, 1)

    def test_budget_expiry_after_remote_read_prevents_branch_push(self):
        fake = FakeGitHub()
        record = {'id': 'case', 'repo': '/tmp/work', 'base_commit': 'base'}
        with patch.object(publish.cli, 'read', return_value=record), \
             patch.object(publish.cli, 'gate', side_effect=[('head', []), ('head', ['time limit reached'])]), \
             patch.object(publish.delivery, '_independent_verification'):
            with self.assertRaisesRegex(publish.PublishError, 'budget changed before branch push'):
                publish.publish(self.task, 'alice/fork', fake)
        self.assertEqual(fake.events, [])

    def test_budget_expiry_before_pr_creation_preserves_pushed_branch_without_creating_pr(self):
        fake = FakeGitHub()
        record = {'id': 'case', 'repo': '/tmp/work', 'base_commit': 'base'}
        checks = [('head', []), ('head', []), ('head', []), ('head', ['time limit reached'])]
        with patch.object(publish.cli, 'read', return_value=record), \
             patch.object(publish.cli, 'gate', side_effect=checks), \
             patch.object(publish.delivery, '_independent_verification'):
            with self.assertRaisesRegex(publish.PublishError, 'budget changed before PR creation'):
                publish.publish(self.task, 'alice/fork', fake)
        self.assertEqual(fake.events, [('push', 'workbench/case', 'head')])


class PublicEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.base, self.head = 'a' * 40, 'b' * 40
        self.data = {'id': 'case', 'repo': str(self.repo), 'base_commit': self.base,
                     'kind': 'bug', 'surface': 'non-ui', 'implementer': 'implementer',
                     'delivery_repo': 'alice/fork', 'criteria': [], 'evidence': []}

    def add_report(self, kind, commit, output, *, actor='verifier', stderr=''):
        image = 'sha256:' + 'f' * 64
        report = {'kind': kind, 'actor': actor, 'commit': commit, 'source_mode': 'image',
                  'workspace': str(self.repo.resolve()), 'source_verified_commit': commit,
                  'verified_image_id': image, 'image': image, 'container_created': True,
                  'cleanup_confirmed': True, 'timed_out': False, 'exit_code': 0,
                  'expected_exit': 0, 'stdout': output, 'stderr': stderr, 'expected_marker': 'MARKER'}
        path = self.root / f'{len(self.data["evidence"])}.json'
        path.write_text(json.dumps(report))
        item = {'kind': kind, 'actor': actor, 'commit': commit, 'path': str(path),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        self.data['evidence'].append(item)
        return path

    def test_bug_body_reports_verified_results_without_raw_secrets_or_paths(self):
        self.add_report('reproduction', self.base, 'MARKER database is locked secret-token-ABCD user@example.com /private/file')
        self.add_report('resolution', self.head, 'MARKER LOCK_RESOLUTION_OBSERVED secret-token-ABCD')
        self.add_report('test-result', self.head, 'MARKER RETRY_BEHAVIOR_PASS 12 passed secret-token-ABCD',
                        stderr='distinct warning, Authorization: Bearer abc123')
        body = publish._pr_body(self.data, self.head)
        for expected in ('database-locked error observed', 'lock-recovery scenario completed', '12 passed', self.head):
            self.assertIn(expected, body)
        self.assertIn('[stdout]', body)
        self.assertIn('[stderr]', body)
        self.assertIn('distinct warning', body)
        self.assertNotIn('abc123', body)
        for secret in ('secret-token-ABCD', 'user@example.com', '/private/file', str(self.root)):
            self.assertNotIn(secret, body)

    def test_modified_or_uninformative_evidence_cannot_be_published(self):
        path = self.add_report('reproduction', self.base, 'MARKER database is locked')
        self.add_report('resolution', self.head, 'MARKER LOCK_RESOLUTION_OBSERVED')
        self.add_report('test-result', self.head, 'MARKER 3 passed')
        path.write_text('{}')
        with self.assertRaises(publish.PublishError): publish._pr_body(self.data, self.head)

    def test_unknown_logs_are_omitted_without_blocking_other_projects(self):
        self.add_report('reproduction', self.base, 'MARKER account=private-secret')
        self.add_report('resolution', self.head, 'MARKER account=private-secret')
        self.add_report('test-result', self.head, 'MARKER 3 passed')
        body = publish._pr_body(self.data, self.head)
        self.assertIn('no fixed log signal recognized', body)
        self.assertIn('3 passed', body)
        self.assertNotIn('private-secret', body)

    def test_non_sqlite_error_has_a_readable_safe_summary(self):
        self.add_report('reproduction', self.base, 'MARKER AssertionError expected private-secret')
        self.add_report('resolution', self.head, 'MARKER fixed private-secret')
        self.add_report('test-result', self.head, 'MARKER 4 passed')
        body = publish._pr_body(self.data, self.head)
        self.assertIn('assertion error observed', body)
        self.assertIn('no fixed log signal recognized', body)
        self.assertIn('4 passed', body)
        self.assertNotIn('private-secret', body)
        self.assertIn('AssertionError expected', body)

    def test_log_excerpt_shows_unique_errors_and_redacts_common_secrets(self):
        text = ('AssertionError: widget result was incorrect\n'
                'Authorization: Bearer abcdef123456789\n'
                'api_key=secret-value-123\n'
                '{"token": "abcd123456789"}\n'
                '{"Authorization": "Basic c2VjcmV0"}\n'
                'Cookie: session=abcdefgh\n'
                'AWS_ACCESS_KEY_ID=AKIA1234567890123456\n'
                'AWS_ACCESS_KEY_ID=shortaccess\n'
                'C:\\Users\\person\\private.txt\n'
                'fetch https://user:password@example.com/path?token=xyz\n'
                '\x1b[31mName: widget\x1b[0m\n'
                '```\n')
        excerpt = publish._public_log_excerpt(text)
        self.assertIn('widget result was incorrect', excerpt)
        for secret in ('abcdef123456789', 'secret-value-123', 'abcd123456789', 'user:password', '\x1b',
                       'c2VjcmV0', 'abcdefgh', 'AKIA1234567890123456', 'shortaccess', 'C:\\Users\\person'):
            self.assertNotIn(secret, excerpt)

    def test_untrusted_markdown_stays_inside_indented_log_block(self):
        self.add_report('reproduction', self.base, 'MARKER AssertionError\n```\n# fake heading\n[link](https://bad.invalid)')
        self.add_report('resolution', self.head, 'MARKER fixed')
        self.add_report('test-result', self.head, 'MARKER 2 passed')
        body = publish._pr_body(self.data, self.head)
        self.assertIn('\n      ```\n      # fake heading\n', body)
        self.assertNotIn('https://bad.invalid', body)

    def test_ui_without_accessible_verified_image_fails_before_remote_write(self):
        self.data['surface'] = 'ui'
        fake = FakeGitHub(); fake.base = self.base
        task = self.root / 'task.json'; task.touch()
        with patch.object(publish.cli, 'read', return_value=self.data), patch.object(publish.cli, 'gate', return_value=(self.head, [])), patch.object(publish.delivery, '_independent_verification'):
            with self.assertRaisesRegex(publish.PublishError, 'screenshot'):
                publish.publish(task, 'alice/fork', fake)
        self.assertEqual(fake.events, [])

    def test_feature_document_links_exact_verified_commit(self):
        for args in (['init', '-q'], ['config', 'user.email', 'a@example.invalid'], ['config', 'user.name', 'Test']):
            subprocess.run(['git', '-C', str(self.repo), *args], check=True)
        doc = self.repo / 'feature.md'; doc.write_text('# Functional description\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', 'feature.md'], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-qm', 'feature'], check=True)
        head = subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], text=True).strip()
        self.data['kind'] = 'feature'
        self.data['evidence'].append({'kind': 'feature-doc', 'commit': head, 'path': str(doc),
                                      'sha256': hashlib.sha256(doc.read_bytes()).hexdigest()})
        self.add_report('test-result', head, 'MARKER 3 passed')
        self.assertIn(f'https://github.com/alice/fork/blob/{head}/feature.md', publish._pr_body(self.data, head))

    def test_ui_screenshot_publishes_separate_reproducible_verified_evidence_branch(self):
        self.data['surface'] = 'ui'
        self.data['criteria'] = [{'id': 'display', 'text': 'display works'}]
        self.add_report('reproduction', self.base, 'MARKER database is locked')
        self.add_report('resolution', self.head, 'MARKER LOCK_RESOLUTION_OBSERVED')
        self.add_report('test-result', self.head, 'MARKER 3 passed')
        image_path = self.root / 'shot.png'
        original = tiny_png()[:33] + png_chunk(b'tEXt', b'Author\x00private-name') + tiny_png()[33:]
        image_path.write_bytes(original)
        image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
        image_id = 'sha256:' + 'f' * 64
        execution = {'kind': 'screenshot', 'actor': 'verifier', 'commit': self.head,
                     'source_mode': 'image', 'workspace': str(self.repo.resolve()),
                     'source_verified_commit': self.head, 'verified_image_id': image_id, 'image': image_id,
                     'container_created': True, 'cleanup_confirmed': True, 'timed_out': False,
                     'exit_code': 0, 'expected_exit': 0, 'stdout': 'PASS', 'stderr': '', 'expected_marker': 'PASS',
                     'screenshot_path': str(image_path), 'screenshot_sha256': image_hash}
        report_path = self.root / 'screenshot-report.json'; report_path.write_text(json.dumps(execution))
        report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
        screenshot = {'kind': 'screenshot', 'actor': 'verifier', 'commit': self.head,
                      'path': str(image_path), 'sha256': image_hash, 'execution_report': str(report_path),
                      'execution_report_sha256': report_hash}
        digest = cli.criteria_digest(self.data)
        review = {'verdict': 'PASS', 'candidate_commit': self.head, 'baseline_commit': self.base,
                  'criteria': ['display'], 'criterion_digest': digest, 'screenshot_hashes': [image_hash],
                  'reports': {'R1': {'path': str(report_path), 'sha256': report_hash}},
                  'assessments': [{'id': 'display', 'verdict': 'PASS', 'reason': 'Screenshot confirms display', 'evidence': ['R1']}]}
        review_path = self.root / 'review.json'; review_path.write_text(json.dumps(review))
        self.data['evidence'].extend([screenshot, {'kind': 'independent-review', 'actor': 'verifier',
          'commit': self.head, 'path': str(review_path), 'sha256': hashlib.sha256(review_path.read_bytes()).hexdigest(),
          'criteria_digest': digest}])
        class GitHub(FakeGitHub):
            def __init__(self):
                super().__init__(); self.branches = {}; self.blobs = {}; self.parents = None; self.files = []
            def remote_head(self, repo, branch): return self.branches.get(branch)
            def push_new(self, workspace, repo, branch, head):
                if not branch.startswith('workbench-evidence/'):
                    self.events.append(('source-push', branch, head)); self.branches[branch] = head
                    return
                self.events.append(('evidence-push', branch, head)); self.branches[branch] = head
                self.parents = subprocess.check_output(['git', '-C', workspace, 'log', '-1', '--format=%P'], text=True).strip()
                self.files = subprocess.check_output(['git', '-C', workspace, 'ls-tree', '--name-only', 'HEAD'], text=True).splitlines()
                for name in ('screenshot.png', 'manifest.json'):
                    blob = subprocess.check_output(['git', '-C', workspace, 'rev-parse', f'HEAD:{name}'], text=True).strip()
                    self.blobs[blob] = (Path(workspace) / name).read_bytes()
            def _call(self, *args):
                blob = args[-1].split('/')[-1]
                return {'sha': blob, 'encoding': 'base64', 'content': base64.b64encode(self.blobs[blob]).decode()}
        github = GitHub(); task = self.root / 'task.json'; task.touch()
        with patch.object(publish.cli, 'read', return_value=self.data), patch.object(publish.cli, 'gate', return_value=(self.head, [])):
            body, artifact = publish._screenshot_body(task, self.data, self.head, 'alice/fork', github)
            body2, artifact2 = publish._screenshot_body(task, self.data, self.head, 'alice/fork', github)
        self.assertEqual(artifact, artifact2)
        self.assertEqual(body, body2)
        self.assertEqual(len(github.events), 1)
        github2 = GitHub(); github2.base = self.base
        with patch.object(publish.cli, 'read', return_value=self.data), patch.object(publish.cli, 'gate', return_value=(self.head, [])):
            result = publish.publish(task, 'alice/fork', github2)
        self.assertEqual(result['pr'], 7)
        self.assertEqual([event[0] for event in github2.events], ['evidence-push', 'source-push', 'create'])
        self.assertEqual(github.parents, '')
        self.assertEqual(github.files, ['manifest.json', 'screenshot.png'])
        self.assertTrue(github.events[0][1].startswith('workbench-evidence/'))
        self.assertIn(f"https://github.com/alice/fork/blob/{artifact['commit']}/screenshot.png", body)
        self.assertEqual(artifact['sha256'], hashlib.sha256(tiny_png()).hexdigest())
        self.assertNotEqual(artifact['sha256'], image_hash)
        manifest = json.loads(next(value for value in github.blobs.values() if value.startswith(b'{')))
        self.assertEqual(manifest['original_screenshot_sha256'], image_hash)
        self.assertEqual(manifest['published_screenshot_sha256'], artifact['sha256'])
        self.assertEqual(image_path.read_bytes(), original)
        blob = next(key for key, value in github.blobs.items() if value == tiny_png())
        github.blobs[blob] = b'changed remotely'
        with patch.object(publish.cli, 'read', return_value=self.data), patch.object(publish.cli, 'gate', return_value=(self.head, [])):
            with self.assertRaisesRegex(publish.PublishError, 'differs'):
                publish._screenshot_body(task, self.data, self.head, 'alice/fork', github)
        self.assertEqual(len(github.events), 1)
        branch = github.events[0][1]
        github.branches[branch] = 'c' * 40
        with patch.object(publish.cli, 'read', return_value=self.data), patch.object(publish.cli, 'gate', return_value=(self.head, [])):
            with self.assertRaisesRegex(publish.PublishError, 'refusing to overwrite'):
                publish._screenshot_body(task, self.data, self.head, 'alice/fork', github)
        self.assertEqual(len(github.events), 1)

    def test_screenshot_without_independent_review_cannot_publish(self):
        self.data['surface'] = 'ui'
        with self.assertRaisesRegex(publish.PublishError, 'screenshot'):
            publish._reviewed_screenshot(self.data, self.head)

    def test_invalid_png_is_not_accepted_as_a_screenshot(self):
        self.assertTrue(publish._valid_png(tiny_png()))
        self.assertFalse(publish._valid_png(b'\x89PNG\r\n\x1a\nnot-a-real-image'))
        corrupt = bytearray(tiny_png()); corrupt[-1] ^= 1
        self.assertFalse(publish._valid_png(bytes(corrupt)))

    def test_png_metadata_is_removed_without_changing_palette_transparency_or_color_profile(self):
        original = palette_png_with_metadata()
        published = publish._publication_png(original)
        self.assertTrue(publish._valid_png(published))
        self.assertNotIn(b'private-name', published)
        self.assertNotIn(b'private-exif', published)
        self.assertNotIn(b'tEXt', published)
        self.assertNotIn(b'zTXt', published)
        self.assertNotIn(b'iTXt', published)
        self.assertNotIn(b'eXIf', published)
        self.assertNotIn(b'tIME', published)
        for chunk in (b'iCCP', b'gAMA', b'PLTE', b'tRNS', b'IDAT'):
            self.assertIn(chunk, published)
        profile = b'sRGB\x00\x00' + zlib.compress(bytes(bytearray((132).to_bytes(4, 'big') + bytes(32) + b'acsp' + bytes(92))))
        self.assertIn(png_chunk(b'iCCP', profile), published)

    def test_png_alpha_pixels_survive_metadata_removal(self):
        header = (1).to_bytes(4, 'big') * 2 + bytes([8, 6, 0, 0, 0])
        pixels = png_chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00\x20'))
        original = (b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', header) +
                    png_chunk(b'tEXt', b'Author\x00private') + pixels + png_chunk(b'IEND', b''))
        published = publish._publication_png(original)
        self.assertTrue(publish._valid_png(published))
        self.assertIn(pixels, published)
        self.assertNotIn(b'private', published)

    def test_png_interlace_and_animation_visual_chunks_are_preserved(self):
        header = (1).to_bytes(4, 'big') + (1).to_bytes(4, 'big') + bytes([8, 6, 0, 0, 1])
        frame = bytes(4) + (1).to_bytes(4, 'big') * 2 + bytes(8) + b'\x00\x01\x00\x0a\x00\x00'
        second_frame = (1).to_bytes(4, 'big') + frame[4:]
        second_pixels = (2).to_bytes(4, 'big') + zlib.compress(b'\x00\x55\x66\x77\x88')
        original = (b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', header) +
                    png_chunk(b'acTL', (2).to_bytes(4, 'big') + bytes(4)) + png_chunk(b'fcTL', frame) +
                    png_chunk(b'tEXt', b'Author\x00private') + png_chunk(b'IDAT', zlib.compress(b'\x00\x11\x22\x33\x44')) +
                    png_chunk(b'fcTL', second_frame) + png_chunk(b'fdAT', second_pixels) +
                    png_chunk(b'IEND', b''))
        published = publish._publication_png(original)
        self.assertTrue(publish._valid_png(published))
        self.assertIn(png_chunk(b'acTL', (2).to_bytes(4, 'big') + bytes(4)), published)
        self.assertIn(png_chunk(b'fcTL', frame), published)
        self.assertIn(png_chunk(b'fcTL', second_frame), published)
        self.assertIn(png_chunk(b'fdAT', second_pixels), published)
        self.assertIn(png_chunk(b'IDAT', zlib.compress(b'\x00\x11\x22\x33\x44')), published)
        self.assertNotIn(b'private', published)

    def test_png_exif_orientation_change_is_rejected(self):
        exif = (b'II\x2a\x00\x08\x00\x00\x00\x01\x00' +
                b'\x12\x01\x03\x00\x01\x00\x00\x00\x06\x00\x00\x00' + b'\x00\x00\x00\x00')
        original = tiny_png()[:33] + png_chunk(b'eXIf', exif) + tiny_png()[33:]
        with self.assertRaisesRegex(publish.PublishError, 'orientation'):
            publish._publication_png(original)


if __name__ == '__main__': unittest.main()
