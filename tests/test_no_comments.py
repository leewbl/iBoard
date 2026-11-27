import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, no_comments


class CommentReviewTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        cli.git(self.repo,'init','-q','-b','main')
        cli.git(self.repo,'config','user.name','Fixture')
        cli.git(self.repo,'config','user.email','fixture@example.invalid')
        self.file = self.repo / 'sample.py'
        self.original = '# An unchanged comment.\nvalue = "# stays a string"\n'
        self.file.write_text(self.original, encoding='utf-8')
        cli.git(self.repo,'add','sample.py')
        cli.git(self.repo,'commit','-qm','base')
        self.file.write_text(self.original + '# SPDX-License-Identifier: MIT\n# Explain the assignment.\n'
                             'literal = value  # repeat the name\n'
                             'typed = literal  # type: ignore[assignment]\n'
                             '# Do not remove the known mapping.\n', encoding='utf-8')

    def collect(self):
        return no_comments.collect(repo=self.repo,out=self.root/'scope.json')

    def fixture_answer(self, evidence):
        packet = cli.read(evidence['path'])
        rows = []
        for ident, note in no_comments._reviewable(packet).items():
            actions = {'ordinary': 'delete', 'suppression': 'repair', 'constraint': 'encode'}
            rows.append({'id':ident,'action':actions[note['kind']],'reason':'Scoped fixture review.','evidence':[]})
        return {'decisions':rows,'flags':[{'path':'sample.py','line':6,'symbol':'typed','reason':'Inspect the expected type.'}]}

    def review(self, evidence, answer=None):
        return no_comments.review(evidence=evidence['path'],sha256=evidence['sha256'],model='gpt-fixture',
                                  out=self.root/'review.json',reviewer=lambda *_: answer or self.fixture_answer(evidence))

    def test_only_approved_new_comments_change_in_the_worktree(self):
        cli.git(self.repo,'add','sample.py')
        staged = cli.git(self.repo,'show',':sample.py')
        first = self.collect()
        notes = cli.read(first['path'])['comments']
        self.assertFalse(any('unchanged comment' in row['text'] for row in notes.values()))
        self.assertFalse(any('stays a string' in row['text'] for row in notes.values()))
        review = self.review(first)
        ordinary = [key for key, row in notes.items() if row['kind']=='ordinary']
        result = no_comments.apply(review=review['path'],sha256=review['sha256'],approve=ordinary,
                                   out=self.root/'applied.json')
        modified = self.file.read_text()
        self.assertIn('# SPDX-License-Identifier: MIT', modified)
        self.assertIn('type: ignore[assignment]', modified)
        self.assertIn('Do not remove', modified)
        self.assertNotIn('repeat the name', modified)
        self.assertNotIn('Explain the assignment', modified)
        self.assertEqual(staged, cli.git(self.repo,'show',':sample.py'))
        self.assertEqual(cli.read(result['path'])['removed'],ordinary)

    def test_changed_worktree_and_nonapproved_suppressions_are_refused(self):
        evidence = self.collect()
        review = self.review(evidence)
        notes = cli.read(evidence['path'])['comments']
        suppression = next(key for key,row in notes.items() if row['kind']=='suppression')
        with self.assertRaisesRegex(ValueError,'approve only exact'):
            no_comments.apply(review=review['path'],sha256=review['sha256'],approve=[suppression],
                              out=self.root/'out.json')
        ordinary = next(key for key,row in notes.items() if row['kind']=='ordinary')
        self.file.write_text(self.file.read_text().replace('literal = value','literal = None'))
        with self.assertRaisesRegex(ValueError,'reviewed file changed'):
            no_comments.apply(review=review['path'],sha256=review['sha256'],approve=[ordinary],
                              out=self.root/'out.json')
        self.assertIn('literal = None', self.file.read_text())

    def test_review_gets_one_schema_correction_and_local_flags_stay_scoped(self):
        evidence = self.collect()
        answer = self.fixture_answer(evidence)
        wrong = {**answer,'flags':[{'path':'other.py','line':1,'symbol':'outside','reason':'not scoped'}]}
        responses = iter([wrong, answer])
        prompts = []
        def reviewer(prompt, model, seconds):
            prompts.append(prompt)
            return next(responses)
        result = no_comments.review(evidence=evidence['path'],sha256=evidence['sha256'],model='gpt-fixture',
                                    out=self.root/'review.json',reviewer=reviewer)
        self.assertEqual(len(cli.read(result['path'])['attempts']),2)
        self.assertIn('root-cause flag leaves review scope',prompts[1])

    def test_untracked_source_is_in_scope_and_protected_bytes_survive(self):
        path = self.repo/'new.py'
        path.write_text('#!/bin/python3\n# A redundant banner.\nresult = 1\n', encoding='utf-8')
        evidence = self.collect()
        notes = cli.read(evidence['path'])['comments']
        self.assertEqual([value['kind'] for value in notes.values() if value['path']=='new.py'],
                         ['protected','ordinary'])

    def test_native_glm_review_binds_model_and_recovers_invalid_json(self):
        from model import glm_review
        evidence = self.collect()
        answer = self.fixture_answer(evidence)
        with patch.object(glm_review, 'review', side_effect=[json.dumps(answer) + '\nextra notes', json.dumps(answer)]) as source:
            result = no_comments.review(evidence=evidence['path'],sha256=evidence['sha256'],model='glm-fixture',
                                        provider='glm',out=self.root/'glm.json')
        self.assertTrue(all(call.kwargs['expected_model']=='glm-fixture' for call in source.call_args_list))
        packet = cli.read(result['path'])
        self.assertEqual(packet['provider'],'glm')
        self.assertEqual(len(packet['attempts']),2)


if __name__ == '__main__':
    unittest.main()
