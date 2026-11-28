import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, skill_authoring


GUIDE = '---\nname: sample\ndescription: "Write notes: preserve evidence."\n---\n\n# Sample\n\n'
IMAGE = 'sha256:' + 'a' * 64


class SkillAuthoringTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q', '-b', 'main')
        self.write('README.md', '# Project\n')
        self.base = self.commit('initial checked-in source')

    def write(self, path, value):
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(value, encoding='utf-8')

    def commit(self, message):
        cli.git(self.repo, 'add', '-A')
        cli.git(self.repo, '-c', 'user.name=Author', '-c', 'user.email=author@example.invalid',
                'commit', '-qm', message)
        return cli.sha(self.repo, 'HEAD')

    def verify(self, base, head, name='proof.json', **options):
        return skill_authoring.check(repo=self.repo, base=base, candidate=head,
                                     out=self.root / name, **options)

    def test_exact_candidate_and_unchanged_skill_cross_links(self):
        self.write('skills/sample/SKILL.md', GUIDE + '[Guide](<reference list.md#stable-title>)\n')
        self.write('skills/sample/reference list.md', '# Stable title\n')
        self.write('.claude/skills/paired/SKILL.md',
                   "---\nname: paired\ndescription: >-\n  Explain the linked\n  release notes.\n---\n\n"
                   "[Checkout the notes][source]\n\n[source]: ../../../skills/sample/SKILL.md#sample\n")
        first = self.commit('add linked guides')
        self.write('skills/sample/reference list.md', '# changed working copy\n')
        good = self.verify(self.base, first)
        self.assertEqual(good['status'], 'passed')
        report = cli.read(good['path'])
        self.assertEqual(report['static']['affected']['skills/sample/SKILL.md']['frontmatter']['description'],
                         'Write notes: preserve evidence.')
        self.assertEqual(report['static']['affected']['.claude/skills/paired/SKILL.md']['references'],
                         ['skills/sample/SKILL.md'])

        self.write('skills/sample/reference list.md', '# Stable title\n')
        self.write('skills/sample/SKILL.md', GUIDE.replace('# Sample', '# Renamed'))
        second = self.commit('rename the target heading')
        bad = self.verify(first, second, 'broken.json')
        self.assertEqual(bad['status'], 'failed')
        self.assertEqual(bad['problems'], [{'skill': '.claude/skills/paired/SKILL.md',
                         'problem': '../../../skills/sample/SKILL.md#sample: missing heading or anchor #sample'}])

    def test_rejects_invalid_metadata_escaping_links_and_untracked_links(self):
        self.write('skills/sample/SKILL.md', GUIDE.replace('name: sample', 'name: other'))
        malformed = self.commit('invalid frontmatter')
        result = self.verify(self.base, malformed)
        self.assertRegex(result['problems'][0]['problem'], 'matching the skill directory')
        self.write('skills/sample/SKILL.md', GUIDE + '[Outside](../../../local.txt) and [missing](notes.md)\n'
                   '[public](https://example.invalid) and '+chr(96)+'[example](ignored.md)'+chr(96)+'\n\n'
                   +chr(96)*3+'md\n[unresolved](example.md)\n'+chr(96)*3+'\n')
        head = self.commit('invalid local links')
        self.write('skills/sample/notes.md', '# exists only in worktree\n')
        result = self.verify(malformed, head, 'links.json')
        self.assertEqual(len(result['problems']), 2)
        self.assertEqual(result['status'], 'failed')

    def test_draft_and_attach_recheck_exact_candidate_through_publish_gate(self):
        root = self.repo / 'skills'
        root.mkdir()
        body = self.root / 'instructions.md'
        body.write_text('# Author\n\nPreserve the evidence.\n', encoding='utf-8')
        drafted = skill_authoring.draft(name='sample', description='Use "quotes" in a note.', body=body, root=root)
        self.assertEqual(Path(drafted['path']).name, 'SKILL.md')
        with self.assertRaises(FileExistsError):
            skill_authoring.draft(name='sample', description='New scope.', body=body, root=root)
        head = self.commit('draft a skill')
        proof = self.verify(self.base, head)
        task = self.root / 'task.json'
        cli.write(task, {'schema': cli.SCHEMA, 'base_commit': self.base, 'repo': str(self.repo),
                         'criteria': [], 'criterion_changes': [], 'evidence': [], 'kind': 'feature',
                         'surface': 'non-ui', 'implementer': 'author'})
        with patch('workbench.publish.publish', return_value={'branch': 'prepared'}) as opening:
            result = skill_authoring.publish_checked(task, proof['path'], proof['sha256'], 'owner/fork')
        self.assertEqual(result['branch'], 'prepared')
        self.assertEqual(opening.call_count, 1)
        record = cli.read(task)
        self.assertEqual(skill_authoring.require(record, head)['sha256'], proof['sha256'])
        self.assertFalse(any('skill authoring evidence' in msg for msg in cli.evidence_gate(record, head)[1]))
        self.write('skills/sample/SKILL.md', GUIDE + 'Another change.\n')
        changed = self.commit('move the candidate')
        with patch('workbench.publish.publish') as opening:
            with self.assertRaisesRegex(cli.WorkbenchError, 'exact task baseline and candidate'):
                skill_authoring.publish_checked(task, proof['path'], proof['sha256'], 'owner/fork')
        opening.assert_not_called()
        self.assertTrue(any('skill authoring evidence' in msg for msg in cli.evidence_gate(record, changed)[1]))

    def test_offline_receipts_are_independent_and_tamper_evident(self):
        self.write('skills/sample/SKILL.md', GUIDE + 'Read the committed guide.\n')
        self.write('skills/sample/scripts/shape.mjs', 'export const version = 1;\n')
        self.write('tests/shape.mjs', 'console.log("shape fixture");\n')
        head = self.commit('validate generated skill structure')
        with self.assertRaisesRegex(ValueError, 'require a committed structural probe'):
            self.verify(self.base, head)
        runs = []

        def sandbox(path, image, command, timeout, **kwargs):
            self.assertEqual((Path(path)/'tests/shape.mjs').read_text(), 'console.log("shape fixture");\n')
            self.assertEqual((Path(path)/'skills/sample/SKILL.md').read_text(), GUIDE + 'Read the committed guide.\n')
            self.assertTrue(kwargs['workspace_read_only'])
            runs.append(command)
            return {
                'command': command, 'image': image, 'source_root': '/workspace', 'memory_mb': 512,
                'workspace_read_only': True, 'container_created': True, 'cleanup_confirmed': True,
                'create_exit_code': 0, 'create_stdout': str(len(runs)) * 64, 'timed_out': False,
                'exit_code': 0, 'stdout_truncated': False, 'stderr_truncated': False,
                'stdout_sha256': 'b' * 64, 'stderr_sha256': 'c' * 64,
            }

        receipt = self.verify(self.base, head, image=IMAGE, probe=['tests/shape.mjs'], sandbox=sandbox)
        self.assertEqual(receipt['status'], 'passed', receipt)
        self.assertEqual(runs, [['node', '/workspace/tests/shape.mjs']] * 2)
        data = {'repo': str(self.repo), 'base_commit': self.base,
                'skill_authoring': {key: receipt[key] for key in ('path', 'sha256')}}
        self.assertEqual(skill_authoring.require(data, head)['probes'], 2)
        logged = cli.read(receipt['path'])['runs'][0]['file']
        (self.root / logged).write_text(json.dumps({'tampered': True}), encoding='utf-8')
        with self.assertRaisesRegex(cli.WorkbenchError, 'evidence digest changed'):
            skill_authoring.require(data, head)


if __name__ == '__main__':
    unittest.main()
