from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_publish import FakeGitHub
from workbench import cli, project_source_review, project_verify, project_verify_delivery, publish


IMAGE = 'sha256:' + 'a' * 64
SKILL = 'skills/verify-fixture'


class ProjectVerificationDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q', '-b', 'main')
        self.write('app.txt', 'registered doctor\nregistered first\nregistered second\n')
        self.helpers = self.root / 'helpers'
        self.helpers.mkdir()
        self.checks = []
        for name in ('doctor', 'first', 'second'):
            marker = f'JOURNEY_{name.upper()}_PASS'
            helper = f'{name}.sh'
            (self.helpers / helper).write_text(f'printf "%s\\n" {marker}\n', encoding='utf-8')
            self.checks.append({'id': name, 'helper': helper, 'marker': marker,
                                'reach': 'Open a local app.', 'drive': 'Start the named journey.',
                                'observe': 'Check the visible result.', 'gotchas': 'Use disposable state.',
                                'source': [{'path': 'app.txt', 'contains': f'registered {name}'}]})
        self.checks[2].update(browser=True, screenshot=True)
        self.contract = {'project': 'fixture', 'surface': 'Fixture application.', 'launch': 'Use a scratch directory.',
                         'cleanup': 'Remove test state.', 'doctor': self.checks[0], 'features': self.checks[1:]}
        self.install(self.contract)
        self.base = self.commit('Record the verification map shipped with the fixture')
        self.correction = deepcopy(self.contract)
        self.correction['features'][0]['observe'] = 'Check the first result and record it.'
        self.install(self.correction)
        self.head = self.commit('Make the first observed result explicit')
        self.runs = []
        self.attestations = []
        self.failures = set()

    def write(self, path, value):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding='utf-8')

    def install(self, contract):
        target = self.repo / SKILL
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        project_verify._write_bundle(target, project_verify._artifacts(contract, self.helpers))

    def commit(self, intent):
        cli.git(self.repo, 'add', '-A')
        cli.git(self.repo, '-c', 'user.name=Verification fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '-qm', intent, '-m', 'Constraint: Temporary Git history for verification regression tests')
        return cli.sha(self.repo, 'HEAD')

    def attest(self, repo, image, timeout, *, commit):
        self.assertEqual(repo, self.repo)
        self.assertEqual(image, IMAGE)
        self.assertEqual(commit, self.base)
        self.assertGreater(timeout, 0)
        self.attestations.append(commit)
        return IMAGE

    def sandbox(self, scratch, image, command, timeout, **kwargs):
        helper = Path(command[1]).name
        name = Path(helper).stem
        check = next(check for check in self.checks if check['id'] == name)
        self.runs.append(name)
        self.assertEqual(command, project_verify._command(check))
        self.assertEqual((Path(scratch) / helper).read_bytes(), (self.helpers / helper).read_bytes())
        self.assertEqual(image, IMAGE)
        self.assertTrue(kwargs['workspace_read_only'])
        self.assertEqual(kwargs['source_root'], '/app')
        self.assertGreater(timeout, 0)
        extra = {}
        stdout = check['marker'] + '\n'
        if check.get('screenshot'):
            picture = kwargs['screenshot_output']
            self.assertEqual((Path(scratch) / 'expected-commit.txt').read_text(), self.base + '\n')
            picture.write_bytes(b'fixture screenshot')
            extra = {'screenshot_sha256': cli.digest(picture), 'screenshot_path': str(picture)}
            stdout = json.dumps({'phase': 'ui', 'servedCommit': self.base, 'screenshot': True}) + '\n' + stdout
        return {'image': image, 'command': command, 'source_root': '/app', 'workspace_read_only': True,
                'started_at': cli.now(), 'elapsed_seconds': 0.25, 'timeout_seconds': timeout,
                'memory_mb': kwargs['memory_mb'], 'create_exit_code': 0, 'container_created': True,
                'create_stdout': f'{len(self.runs):064x}\n', 'create_stdout_truncated': False,
                'workspace': str(scratch), 'cleanup_confirmed': True, 'exit_code': int(name in self.failures),
                'timed_out': False, 'stdout': stdout, 'stdout_truncated': False, 'stderr': '',
                'stderr_truncated': False, 'stdout_sha256': sha256(stdout.encode()).hexdigest(),
                'stderr_sha256': sha256(b'').hexdigest(), **extra}

    def verify(self, name='proof', *, head=None, skill=SKILL):
        return project_verify_delivery.check(repo=self.repo, base=self.base, candidate=head or self.head,
                                             skill=skill, image=IMAGE, out=self.root / name, memory_mb=512,
                                             sandbox=self.sandbox, attester=self.attest)

    def task_data(self):
        return {'schema': cli.SCHEMA, 'repo': str(self.repo), 'base_commit': self.base, 'kind': 'feature',
                'id': 'fixture', 'status': 'running', 'started_at': cli.now(), 'iterations': 0,
                'limits': {'seconds': 600, 'iterations': 8},
                'surface': 'non-ui', 'criteria': [], 'criterion_changes': [], 'evidence': [], 'implementer': 'author'}

    def proof_data(self, receipt):
        return {**self.task_data(), 'project_verification_maintenance':
                {key: receipt[key] for key in ('path', 'sha256')}}

    def maintenance_report(self, receipt):
        proof = cli.read(receipt['path'])
        return Path(receipt['path']).parent / proof['maintenance']['path']

    def test_exact_skill_correction_goes_through_normal_pr_gate(self):
        receipt = self.verify()
        self.assertEqual(receipt['status'], 'passed', receipt)
        self.assertGreaterEqual(len(self.attestations), 1)
        self.assertEqual(self.runs, ['doctor', 'first', 'second'] * len(self.attestations))
        report = cli.read(self.maintenance_report(receipt))
        self.assertEqual(report['outcome'], 'changed')
        self.assertEqual(report['commit'], self.base)
        self.assertEqual(report['features'], {'first': 'passed', 'second': 'passed'})
        task_path = self.root / 'task.json'
        cli.write(task_path, self.task_data())
        with patch('workbench.publish.publish', return_value={'pr': 7}) as opening:
            pinned = project_verify_delivery.publish_checked(task_path, receipt['path'], receipt['sha256'], 'owner/fork')
        self.assertEqual(pinned, {'pr': 7})
        opening.assert_called_once()
        data = cli.read(task_path)
        project_verify_delivery.require(data, self.head)
        self.assertFalse(any('verification maintenance' in msg for msg in cli.evidence_gate(data, self.head)[1]))
        self.write(f'{SKILL}/SKILL.md', '# Moved after acceptance.\n')
        new_head = self.commit('Advance the candidate after proof')
        with self.assertRaises(cli.WorkbenchError):
            project_verify_delivery.require(data, new_head)

    def test_verified_correction_creates_and_reuses_one_pr(self):
        receipt = self.verify()
        self.assertEqual(receipt['status'], 'passed')
        data = self.task_data()
        data['criteria'] = [{'id': 'map', 'text': 'Verify both existing feature journeys.', 'revision': 1}]
        criteria = cli.criteria_digest(data)
        runtime = self.root / 'candidate-test.json'
        cli.write(runtime, {
            'actor': 'reviewer', 'kind': 'test-result', 'commit': self.head, 'container_created': True,
            'cleanup_confirmed': True, 'timed_out': False, 'exit_code': 0, 'expected_exit': 0,
            'workspace': str(self.repo), 'source_mode': 'image', 'source_verified_commit': self.head,
            'verified_image_id': IMAGE, 'image': IMAGE, 'stdout': '2 passed', 'stderr': '',
        })
        review = self.root / 'review.json'
        cli.write(review, {
            'verdict': 'PASS', 'candidate_commit': self.head, 'baseline_commit': self.base,
            'criteria': ['map'], 'criterion_digest': criteria,
            'reports': {'TEST': {'path': str(runtime), 'sha256': cli.digest(runtime)}},
            'assessments': [{'id': 'map', 'verdict': 'PASS', 'evidence': ['TEST'], 'reason': 'Both journeys passed.'}],
        })
        documents = self.repo / SKILL / 'features' / 'first.md'
        for index, (kind, path) in enumerate((('test-result', runtime), ('runtime-log', runtime),
                                               ('feature-doc', documents), ('independent-review', review)), 1):
            data['evidence'].append({'id': index, 'kind': kind, 'commit': self.head, 'path': str(path),
                                     'sha256': cli.digest(path), 'actor': 'reviewer', 'criteria': ['map'],
                                     'criteria_digest': criteria})
        task = self.root / 'ready-task.json'
        cli.write(task, data)
        destination = 'alice/fork'
        github = FakeGitHub()
        github.base = self.base
        first = project_verify_delivery.publish_checked(task, receipt['path'], receipt['sha256'], destination, github)
        again = project_verify_delivery.publish_checked(task, receipt['path'], receipt['sha256'], destination, github)
        self.assertEqual(first, again)
        self.assertEqual(first['pr'], 7)
        self.assertEqual(github.events, [('push', 'workbench/fixture', self.head), ('create', 'workbench/fixture')])
        body = github.existing[0]['body']
        self.assertIn('## Verification skill maintenance', body)
        self.assertIn(receipt['sha256'], body)
        self.assertNotIn(str(self.root), body)

    def test_rejects_product_edits_even_if_later_commit_reverts_them(self):
        app = (self.repo / 'app.txt').read_text()
        self.write('app.txt', app + 'changed product\n')
        mixed = self.commit('Change product behavior inside a verification revision')
        self.write('app.txt', app)
        reverted = self.commit('Revert the mixed-scope product change')
        for name, head in (('product-change', mixed), ('reverted-product-change', reverted), ('no-op', self.base)):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.verify(name, head=head)
        self.assertEqual(self.runs, [])

    def test_reads_committed_bundle_and_rejects_non_generated_candidate(self):
        self.write(f'{SKILL}/features/first.md', '# Working-copy replacement text.\n')
        proof = self.verify('unchanged-by-working-copy')
        self.assertEqual(proof['status'], 'passed', proof)
        project_verify_delivery.require(self.proof_data(proof), self.head)
        different = self.commit('Publish unreconciled manual edits to a generated map')
        try:
            receipt = self.verify('non-generated', head=different)
        except ValueError:
            return
        self.assertEqual(receipt['status'], 'failed', receipt)

    def test_changed_run_evidence_blocks_reattaching_and_ordinary_publication(self):
        receipt = self.verify()
        self.assertEqual(receipt['status'], 'passed', receipt)
        task_path = self.root / 'task.json'
        cli.write(task_path, self.task_data())
        project_verify_delivery.attach(task_path, receipt['path'], receipt['sha256'])
        report = self.maintenance_report(receipt)
        raw = report.parent / cli.read(report)['runs'][0]['report']
        raw.write_text('{}', encoding='utf-8')
        with self.assertRaises(cli.WorkbenchError):
            project_verify_delivery.require(cli.read(task_path), self.head)
        self.assertTrue(any('verification maintenance' in msg for msg in cli.evidence_gate(cli.read(task_path), self.head)[1]))
        with patch('workbench.publish.publish') as opening:
            with self.assertRaises(cli.WorkbenchError):
                project_verify_delivery.publish_checked(task_path, receipt['path'], receipt['sha256'], 'owner/fork')
        opening.assert_not_called()
        github = Mock()
        with self.assertRaisesRegex(publish.PublishError, 'verification maintenance evidence'):
            publish.publish(task_path, 'owner/fork', github=github)
        self.assertEqual(github.mock_calls, [])

    def test_source_review_is_rechecked_during_delivery(self):
        def invoke(engine, prompt, seconds):
            evidence = json.loads(prompt.splitlines()[-1])['evidence']['S1']
            _, _, quote = evidence['text'].splitlines()[0].partition(': ')
            answer = {
                'status': 'aligned', 'summary': 'The feature registration is present.', 'note': None,
                'citations': [{'id': 'S1', 'line': evidence['start'], 'quote': quote}],
                'recipe': {'entry': 'CLI', 'action': 'Open the feature.', 'expect': 'The command is registered.'},
            }
            return {'observed_model': engine['model'], 'answer': '```json\n' + json.dumps(answer) + '\n```'}

        source_proof = project_source_review.review(
            repo=self.repo, revision=self.base, skill=self.repo / SKILL, provider='glm', model='glm-fixture',
            out=self.root / 'source-review', invoke=invoke,
        )
        receipt = project_verify_delivery.check(
            repo=self.repo, base=self.base, candidate=self.head, skill=SKILL, image=IMAGE,
            out=self.root / 'verified-with-source', memory_mb=512, sandbox=self.sandbox, attester=self.attest,
            source_review=source_proof['path'], source_review_sha256=source_proof['sha256'],
        )
        self.assertEqual(receipt['status'], 'passed')
        data = self.proof_data(receipt)
        project_verify_delivery.require(data, self.head)
        Path(source_proof['path']).write_text('{}')
        with self.assertRaisesRegex(cli.WorkbenchError, 'source review evidence'):
            project_verify_delivery.require(data, self.head)

    def test_mapped_browser_requires_original_screenshot(self):
        receipt = self.verify()
        self.assertEqual(receipt['status'], 'passed', receipt)
        report = self.maintenance_report(receipt)
        (report.parent / 'second.png').write_bytes(b'replaced capture')
        with self.assertRaises(cli.WorkbenchError):
            project_verify_delivery.require(self.proof_data(receipt), self.head)

    def test_runtime_mismatches_block_even_with_consistent_report_hashes(self):
        receipt = self.verify()
        self.assertEqual(receipt['status'], 'passed', receipt)
        proof_path = Path(receipt['path'])
        proof = cli.read(proof_path)
        report_path = self.maintenance_report(receipt)
        report = cli.read(report_path)
        doctor, first, _ = (report_path.parent / row['report'] for row in report['runs'])
        run = cli.read(first)
        changes = {'wrong-image': {'image': 'sha256:' + 'b' * 64},
                   'wrong-revision': {'commit': self.head},
                   'unclean': {'cleanup_confirmed': False},
                   'reused-container': {'create_stdout': cli.read(doctor)['create_stdout']}}
        for case, fields in changes.items():
            with self.subTest(case=case):
                cli.write(first, {**run, **fields})
                report['runs'][1]['sha256'] = cli.digest(first)
                cli.write(report_path, report)
                proof['maintenance']['sha256'] = cli.digest(report_path)
                cli.write(proof_path, proof)
                with self.assertRaises(cli.WorkbenchError):
                    project_verify_delivery.require(self.proof_data({**receipt, 'sha256': cli.digest(proof_path)}),
                                                    self.head)

    def test_failed_journey_never_reaches_remote_write(self):
        self.failures.add('first')
        receipt = self.verify()
        self.assertEqual(receipt['status'], 'failed', receipt)
        report = cli.read(self.maintenance_report(receipt))
        self.assertIn('first', report['features'])
        self.assertEqual(report['features']['first'], 'behavior-unverified')
        task_path = self.root / 'task.json'
        cli.write(task_path, self.task_data())
        with patch('workbench.publish.publish') as opening:
            with self.assertRaises(cli.WorkbenchError):
                project_verify_delivery.publish_checked(task_path, receipt['path'], receipt['sha256'], 'owner/fork')
        opening.assert_not_called()
        self.assertNotIn('project_verification_maintenance', cli.read(task_path))


if __name__ == '__main__':
    unittest.main()
