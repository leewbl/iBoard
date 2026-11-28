from pathlib import Path
import tempfile
import unittest

from workbench import technical_writing as writing, writing_rules


ORIGINAL = """# Configure the worker

It is important to note that this crucial step uses `systemctl`. Do not start 2 workers.
The default timeout is 350 ms. Read the [worker configuration](docs/worker.md).

```sh
worker up --isolated
```
"""
REVISED = ORIGINAL.replace('It is important to note that this crucial step uses', 'Use')


class TechnicalWritingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.file = self.root / 'in.md'
        self.file.write_text(ORIGINAL)

    def edit(self, draft=REVISED, *, mode='technical-writing', action='rewrite', format='doc', structure='how-to'):
        evidence = writing.collect(mode=mode, action=action, input=self.file, out=self.root / 'input.json',
                                   structure=structure, format=format)
        answer = {'structure': structure, 'draft': draft,
                  'findings': [{'rule': 'U23', 'location': 'L3', 'detail': 'Start with the action.'}]}
        return writing.review(evidence=evidence['path'], sha256=evidence['sha256'], model='gpt-test',
                              out=self.root / 'review.json', reviewer=lambda *_: answer)

    def verify(self, report, *, reviewer=None):
        def pass_review(*_args):
            return {'checks': [{'id': name, 'verdict': 'pass', 'reason': 'Source confirms this.',
                                'citations': ['L3']} for name in writing.CRITERIA]}
        return writing.verify(report=report['path'], sha256=report['sha256'], model='gpt-test',
                              out=self.root / 'verification.json', reviewer=reviewer or pass_review)

    def test_edit_export_preserves_exact_input_and_records_rule(self):
        report = self.edit()
        output = writing.show(report=report['path'], sha256=report['sha256'])
        self.assertIn('important to note', output['diff'])
        self.assertEqual(output['findings'][0]['rule'], 'U23')
        self.assertEqual(output['structure'], 'how-to')
        receipt = self.verify(report)
        target = self.root / 'revised.md'
        writing.export(verification=receipt['path'], sha256=receipt['sha256'], out=target)
        self.assertEqual(self.file.read_text(), ORIGINAL)
        self.assertEqual(target.read_text(), REVISED)
        with self.assertRaises(FileExistsError):
            writing.export(verification=receipt['path'], sha256=receipt['sha256'], out=target)

    def test_rewrite_cannot_change_commands_links_values_or_flags(self):
        changed = REVISED.replace('worker up --isolated', 'worker up --unsafe')
        changed = changed.replace('docs/worker.md', 'docs/other.md').replace('350 ms', '350 MB')
        changed = changed.replace('2 workers', '3 workers')
        report = self.edit(changed)
        self.assertEqual(report['status'], 'blocked')
        self.assertTrue({'changed code blocks and indents', 'changed numbers', 'changed link destinations'} <=
                        set(report['problems']))
        verification = self.verify(report, reviewer=lambda *_: self.fail('static violations should block model review'))
        self.assertEqual(verification['status'], 'blocked')
        with self.assertRaisesRegex(ValueError, 'all independent fidelity checks'):
            writing.export(verification=verification['path'], sha256=verification['sha256'], out=self.root / 'new.md')

    def test_independent_verifier_blocks_semantic_reversal(self):
        edit = self.edit(REVISED.replace('Do not start 2 workers', 'Start 2 workers'))
        self.assertEqual(edit['status'], 'reviewed')
        def reviewer(prompt, *_):
            self.assertIn('Do not start 2 workers', prompt)
            self.assertIn('Start 2 workers', prompt)
            return {'checks': [{'id': name, 'verdict': 'fail' if name == 'meaning' else 'pass',
                                'reason': 'Original instruction forbids a second worker.', 'citations': ['L3']}
                               for name in writing.CRITERIA]}
        result = self.verify(edit, reviewer=reviewer)
        self.assertEqual(result['status'], 'blocked')

    def test_audit_does_not_create_an_edit(self):
        result = self.edit(None, mode='unslop', action='audit', format='prose', structure=None)
        shown = writing.show(report=result['path'], sha256=result['sha256'])
        self.assertIsNone(shown['draft'])
        with self.assertRaisesRegex(ValueError, 'no replacement'):
            self.verify(result)

    def test_drafting_requires_a_single_document_purpose_but_pr_skips_diataxis(self):
        malformed_doc = '# Configure the worker\n\n### Gap\n\nInstructions'
        self.assertIn('an article needs one H1 and no skipped heading levels',
                      writing_rules.problems(ORIGINAL, malformed_doc, preserve=False, article=True))
        report = self.edit('Fix duplicate worker startup. Configure the worker before starting it.',
                           action='draft', format='pr', structure=None)
        self.assertEqual(report['status'], 'reviewed')
        self.assertEqual(self.verify(report)['status'], 'passed')

    def test_lint_reports_lines_and_ignores_inline_code_and_fences(self):
        result = writing_rules.style('A crucial check.\nRun `tool delve`.\n```text\ncrucial\n```\n')
        self.assertEqual(result['U7'], [1])
        self.assertIn('changed numbers', writing_rules.problems('Last seen at 2.', 'Last seen at 3.', preserve=True))
        self.assertIn('changed link destinations',
                      writing_rules.problems('[doc](guide.md)', '![doc](guide.md)', preserve=True))
        self.assertIn('changed numbers',
                      writing_rules.problems('需要两份报告。', '需要一份报告。', preserve=True))
        self.assertIn('introduced a semicolon in prose',
                      writing_rules.problems('启动服务。保存日志。', '启动服务；保存日志。', preserve=True))
