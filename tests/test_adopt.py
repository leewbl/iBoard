import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workbench import adopt, cli, delivery, start, workflow


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        (self.repo / 'code').write_text('broken')
        self.git('add', 'code')
        self.git('commit', '-qm', 'baseline')
        self.base = cli.sha(self.repo, 'HEAD')
        info = workflow.prepare(task_id='old', repo=self.repo, tasks_root=self.root / 'tasks',
            workspaces_root=self.root / 'workspaces', source='reported bug', kind='bug',
            surface='non-ui', iterations=None, seconds=None, implementer='author', required_ci_workflows=['CI'])
        self.old = Path(info['task'])
        self.workspace = Path(info['workspace'])
        (self.workspace / 'code').write_text('fixed')
        self.git('add', 'code', repo=self.workspace)
        self.git('commit', '-qm', 'candidate', repo=self.workspace)
        self.candidate = cli.sha(self.workspace, 'HEAD')
        data = cli.read(self.old)
        data['status'] = 'paused'
        data['criteria'] = [{'id': 'fix', 'text': 'fix the failure', 'revision': 1}]
        cli.write(self.old, data)
        (self.old.parent / 'issue.txt').write_text('reported bug and logs')
        self.original_run = subprocess.run

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, repo=None):
        subprocess.run(['git', '-C', str(repo or self.repo), *args], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def fake_run(self, command, **kwargs):
        if command[:3] == ['docker', 'image', 'inspect']:
            return subprocess.CompletedProcess(command, 0, json.dumps([{'Id': 'sha256:' + 'a' * 64}]), '')
        return self.original_run(command, **kwargs)

    def create(self, **kwargs):
        with patch('workbench.adopt.subprocess.run', side_effect=self.fake_run):
            return adopt.create(legacy_task=self.old, task_id='new', tasks_root=self.root / 'tasks',
                workspaces_root=self.root / 'workspaces', image='baseline', pr_repo='alice/fork', **kwargs)

    def test_rebinds_input_and_preserves_baseline_candidate_and_old_task(self):
        old_bytes = self.old.read_bytes()
        result = self.create()
        self.assertEqual(self.old.read_bytes(), old_bytes)
        data = cli.read(Path(result['task']))
        self.assertEqual(data['status'], 'paused')
        self.assertEqual(data['base_commit'], self.base)
        self.assertEqual(data['adoption']['candidate_commit'], self.candidate)
        self.assertEqual(data['limits']['seconds'], None)
        self.assertEqual(data['limits']['iterations'], None)
        self.assertEqual(data['criteria'], cli.read(self.old)['criteria'])
        self.assertEqual(cli.sha(result['workspace'], 'HEAD'), self.base)
        self.assertFalse((Path(result['task']).parent / 'autopilot.json').exists())
        self.assertEqual((Path(result['task']).parent / 'issue.txt').read_text(), 'reported bug and logs')
        self.assertIn('unavailable', data['adoption']['provenance'])

    def test_seven_gib_is_persisted_and_passed_to_resume(self):
        result = self.create(memory_mb=7168)
        task = Path(result['task'])
        self.assertEqual(cli.read(task)['adoption']['memory_mb'], 7168)
        self.assertEqual(cli.read(task.parent / 'input-stage.json')['run_settings']['memory_mb'], 7168)
        seen = {}
        def run(*args, **kwargs):
            seen.update(kwargs)
            return {'phase': 'plan'}
        start.resume_task(task, run=run)
        self.assertEqual(seen['memory_mb'], 7168)

    def test_invalid_memory_is_rejected_before_creating_task(self):
        for memory in (True, False, 127, 8193, 7168.0, '7168', None):
            with self.subTest(memory=memory):
                with self.assertRaises(adopt.AdoptionError):
                    self.create(memory_mb=memory)
                self.assertFalse((self.root / 'tasks/new').exists())

    def test_dirty_legacy_is_rejected_before_new_task_creation(self):
        (self.workspace / 'code').write_text('uncommitted')
        with self.assertRaisesRegex(adopt.AdoptionError, 'uncommitted'):
            self.create()
        self.assertFalse((self.root / 'tasks/new').exists())

    def test_prepared_task_resumes_with_pinned_isolation_and_fresh_branch(self):
        result = self.create()
        seen = {}
        def run(task, issue, image, **kwargs):
            seen.update(task=task, issue=issue.read_text(), image=image, **kwargs)
            return {'phase': 'plan'}
        start.resume_task(result['task'], run=run)
        self.assertEqual(seen['image'], 'sha256:' + 'a' * 64)
        self.assertEqual(seen['source_mode'], 'image')
        self.assertTrue(seen['build_source_image'])
        self.assertEqual(seen['pr_repo'], 'alice/fork')
        self.assertEqual(seen['issue'], 'reported bug and logs')
        self.assertEqual(result['branch'], 'workbench/new')

    def test_legacy_attachments_are_not_silently_discarded(self):
        (self.old.parent / 'inputs').mkdir()
        with self.assertRaisesRegex(adopt.AdoptionError, 'attachments'):
            self.create()

    def test_rejects_unrelated_candidate(self):
        data = cli.read(self.old)
        data['base_commit'] = 'a' * 40
        cli.write(self.old, data)
        with self.assertRaisesRegex(adopt.AdoptionError, 'descendant'):
            self.create()

    def add_policy(self):
        basis = self.root / 'policy-basis.md'
        basis.write_text('Original policy evidence')
        data = cli.read(self.old)
        data['ci_skip_policy'] = 'openclaw-selected-v1'
        data['events'].append({'at': cli.now(), 'type': 'ci-skip-policy', 'mode': 'existing',
            'policy': data['ci_skip_policy'], 'source': 'user-authorized review', 'reason': 'verified selection',
            'base_commit': data['base_commit'], 'required_ci_workflows': ['CI'],
            'basis': str(basis), 'basis_sha256': cli.digest(basis), 'criteria_sha256': cli.criteria_digest(data)})
        cli.write(self.old, data)

    def test_inherited_policy_basis_is_copied_and_rechecked(self):
        self.add_policy()
        result = self.create()
        data = cli.read(Path(result['task']))
        self.assertTrue(delivery._recorded_skip_policy(data))
        event = next(e for e in data['events'] if e.get('type') == 'ci-skip-policy')
        self.assertEqual(event['mode'], 'existing')
        self.assertEqual(Path(event['basis']).parent, Path(result['task']).parent)
        Path(event['basis']).write_text('tampered')
        self.assertFalse(delivery._recorded_skip_policy(data))

    def test_rejects_tampered_legacy_policy_basis_before_creating_task(self):
        self.add_policy()
        (self.root / 'policy-basis.md').write_text('tampered')
        with self.assertRaisesRegex(adopt.AdoptionError, 'policy evidence is invalid'):
            self.create()
        self.assertFalse((self.root / 'tasks/new').exists())

    def test_inherited_policy_rejects_changed_criteria(self):
        self.add_policy()
        result = self.create()
        data = cli.read(Path(result['task']))
        self.assertTrue(delivery._recorded_skip_policy(data))
        data['criteria'][0]['text'] = 'weakened'
        self.assertFalse(delivery._recorded_skip_policy(data))

    def test_creation_only_legacy_policy_is_not_promoted_without_basis(self):
        data = cli.read(self.old)
        data['ci_skip_policy'] = 'openclaw-selected-v1'
        data['events'].append({'at': cli.now(), 'type': 'ci-skip-policy', 'mode': 'creation',
            'policy': data['ci_skip_policy'], 'source': 'task-creation', 'reason': 'selected',
            'base_commit': data['base_commit'], 'required_ci_workflows': ['CI']})
        cli.write(self.old, data)
        self.assertTrue(delivery._recorded_skip_policy(data))
        with self.assertRaisesRegex(adopt.AdoptionError, 'no revalidatable basis'):
            self.create()


if __name__ == '__main__':
    unittest.main()
