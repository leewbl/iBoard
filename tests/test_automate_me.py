import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from model import codex_review
from workbench import automate_me, cli


class AutomateMeTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.repo = self.root/'project'
        self.repo.mkdir()
        cli.git(self.repo, 'init', '-q')

    def session(self, name, stamp, *messages, cwd=None):
        out = self.root/f'{name}.jsonl'
        rows = [
            {'type':'user', 'sessionId':name, 'uuid':f'{name}-{index}', 'cwd':str(cwd or self.repo),
             'timestamp':stamp, 'message':{'role':'user','content':message}}
            for index, message in enumerate(messages)
        ]
        out.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        return out

    def files(self):
        return [self.session('original','2026-11-02T10:00:00Z','Keep baseline evidence.',
                             [{'type':'tool_result','content':'Do not copy this'}], 'x'*2401,
                             'User row <system-reminder>host extra</system-reminder>'),
                self.session('another','2026-11-10T10:00:00Z','Keep the original and candidate evidence.')]

    def collect(self, paths=(), **kwargs):
        return automate_me.collect(self.repo, self.root/'evidence.json', paths, **kwargs)

    def approve(self, mode, selected=None, direct=None, remove=None):
        out=self.root/'choices.json'
        out.write_text(json.dumps({'mode':mode,'selected':selected or [],'direct':direct or [],'remove':remove or []}))
        return out

    def test_repeated_conventions_need_cross_session_support_then_explicit_selection(self):
        captured=self.collect(self.files())
        data=cli.read(captured['path'])
        self.assertEqual(data['sources'][0]['omitted_long_turns'],1)
        self.assertEqual(captured['turns'],2)

        def judge(prompt, model, timeout):
            if '"signals":[' in prompt and 'week of user messages' in prompt:
                self.assertNotIn(str(self.repo), prompt)
                self.assertNotIn('tool_result', prompt)
                self.assertNotIn('system-reminder', prompt)
                ident='U1' if '"U1":' in prompt else 'U2'
                return {'signals':[{'area':'verification','direction':'supports',
                                    'statement':'Retain the execution evidence.', 'evidence':[ident]}]}
            return {'patterns':[
                {'area':'verification','rule':'Keep the baseline and candidate evidence.', 'signals':['S1','S2']},
                {'area':'verification','rule':'Adopt a sample that appears only once.', 'signals':['S1']},
            ]}

        proposal=automate_me.mine(captured['path'],captured['sha256'],self.root/'mined.json',
                                  model='gpt-fixture',reviewer=judge)
        self.assertEqual(proposal['patterns'],1)
        selected=self.approve('example-mode',['P1'],[{'area':'response','rule':'Write a short summary.'}])
        drafted=automate_me.draft(proposal['path'],proposal['sha256'],selected,self.root/'draft')
        contents=Path(drafted['path']).read_text()
        self.assertIn('disable-model-invocation: true',contents)
        self.assertIn('- Keep the baseline and candidate evidence.', contents)
        self.assertIn('- Write a short summary.', contents)
        self.assertNotIn('appears only once',contents)
        self.assertEqual(cli.git(self.repo,'status','--porcelain'),'')

    def test_foreign_workspace_and_invented_citations_are_refused(self):
        outside=self.session('other','2026-11-03T10:00:00Z','Outside workspace.',cwd=self.root)
        with self.assertRaisesRegex(ValueError,'different workspace'):
            self.collect([outside])
        captured=self.collect(self.files())

        def judge(*_):
            return {'signals':[{'area':'verification','direction':'supports','statement':'Check output.',
                                'evidence':['U99']}]}
        with self.assertRaisesRegex(ValueError,'unknown or duplicate evidence'):
            automate_me.mine(captured['path'],captured['sha256'],self.root/'proposals.json',
                             model='gpt-fixture',reviewer=judge)
        with self.assertRaisesRegex(ValueError,'digest changed'):
            automate_me.mine(captured['path'],'0'*64,self.root/'proposals.json',model='gpt-fixture',reviewer=judge)

    def test_provider_failure_stops_dispatching_later_weeks(self):
        paths = [self.session(f'session-{index}', f'2026-11-{day:02d}T10:00:00Z', 'A durable rule.')
                 for index, day in enumerate((2, 9, 16, 23))]
        evidence = self.collect(paths)
        calls = []

        def fail(prompt, model, timeout):
            calls.append(prompt)
            raise codex_review.ReviewError('provider failed')

        with self.assertRaisesRegex(codex_review.ReviewError, 'provider failed'):
            automate_me.mine(evidence['path'], evidence['sha256'], self.root/'not-exported.json',
                             model='gpt-fixture', reviewer=fail)
        self.assertLessEqual(len(calls), 2)
        self.assertFalse((self.root/'not-exported.json').exists())

    def test_tracked_update_uses_the_git_cutoff_and_keeps_unrelated_sections(self):
        original=self.repo/'SKILL.md'
        initial=('---\nname: example-mode\ndescription: "Use when requested."\n'
                 'disable-model-invocation: true\n---\n\n# Example\n\n## Delivery\n'
                 '\n- Replace this rule.\n\n## Custom\n\nUnrelated paragraph.\n')
        original.write_text(initial)
        cli.git(self.repo,'add','SKILL.md')
        date='2026-11-05T00:00:00Z'
        subprocess.run(['git','-C',str(self.repo),'-c','user.name=Fixture','-c','user.email=test@example.invalid',
                        '-c','commit.gpgsign=false','commit','-qm','mode fixture'],
                       env={**os.environ,'GIT_AUTHOR_DATE':date,'GIT_COMMITTER_DATE':date},check=True)
        captured=self.collect(self.files(),existing=original)
        self.assertEqual(captured['turns'],1)

        def no_model(*_):
            self.fail('one week does not need a model call')

        proposal=automate_me.mine(captured['path'],captured['sha256'],self.root/'mined.json',
                                  model='gpt-fixture',reviewer=no_model)
        confirmation=self.approve('example-mode',direct=[{'area':'delivery','rule':'Include the tested revision.'}],
                                  remove=['Replace this rule.'])
        drafted=automate_me.draft(proposal['path'],proposal['sha256'],confirmation,self.root/'draft')
        contents=Path(drafted['path']).read_text()
        self.assertIn('## Custom\n\nUnrelated paragraph.',contents)
        self.assertIn('- Include the tested revision.',contents)
        self.assertEqual(contents.count('## Delivery'),1)
        self.assertNotIn('Replace this rule.',contents)
        self.assertEqual(original.read_text(),initial)
        original.write_text(initial+'Later edit\n')
        with self.assertRaisesRegex(ValueError,'digest changed'):
            automate_me.draft(proposal['path'],proposal['sha256'],confirmation,self.root/'lost-edit')
