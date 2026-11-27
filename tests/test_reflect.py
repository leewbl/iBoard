import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, reflect


class ReflectTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root/'project'
        self.repo.mkdir()
        cli.git(self.repo,'init','-q')
        self.cache = self.root/'plugins/cache/fixture/skills'
        self.source = []
        for name in ('release','resume'):
            p = self.cache/name
            p.mkdir(parents=True)
            skill = p/'SKILL.md'
            skill.write_text(f'---\nname: {name}\ndescription: Original trigger.\n---\n\n# Guide\n\nOriginal rule.\n')
            doc = p/'guide.txt'
            doc.write_text('Accompanying file.\n')
            doc.chmod(0o750)
            self.source.append(skill)
        self.session = self.root/'session.jsonl'
        self.write_session(self.repo)

    def write_session(self,cwd):
        messages = [('user','/release Review this.'),
                    ('assistant',[{'type':'tool_use','name':'Bash','id':'run-fixture',
                                   'input':{'command':'check-tool --token=fixture-secret'}}]),
                    ('user','Include the verified commit. Use resume when the run was interrupted.')]
        rows = [{'type':role,'message':{'role':role,'content':content},
                 'uuid':f'event-{index}','sessionId':'fixture','cwd':str(cwd)}
                for index,(role,content) in enumerate(messages)]
        self.session.write_text(''.join(json.dumps(row)+'\n' for row in rows))

    def collect(self):
        return reflect.collect(self.repo,self.session,self.source,self.root/'evidence.json')

    @staticmethod
    def response():
        return {
            'accepted':[
                {'skill':'release','kind':'guidance','problem':'Commit is missing.',
                 'line':8,'replacement':'Original rule.\nInclude the verified commit.','evidence':['E3.1']},
                {'skill':'resume','kind':'trigger','problem':'Trigger is too narrow.',
                 'line':3,'replacement':'description: Recover an interrupted run.','evidence':['E3.1']},
            ],
            'rejected':[{'reason':'One-off terminal request.','evidence':['E1.1']}],
            'backlog':[{'problem':'Check evidence identity.','check':'Add a receipt assertion.','evidence':['E2.1']}],
        }

    def propose(self, capture, result=None):
        def review(prompt, model, timeout):
            self.assertNotIn('fixture-secret',prompt)
            return result if result is not None else self.response()
        return reflect.propose(capture['path'],capture['sha256'],self.root/'proposal.json',
                               model='gpt-fixture',reviewer=review)

    def apply(self, proposal, ids, name='receipt.json'):
        return reflect.apply(proposal['path'],proposal['sha256'],ids,self.root/name)

    def test_approved_copy_preserves_related_files_and_cache(self):
        packet = self.collect()
        proposal = self.propose(packet)
        dst = self.repo/'.claude/skills'
        self.assertFalse(dst.exists())
        applied = self.apply(proposal,['R1'])
        report = cli.read(applied['path'])
        self.assertEqual(report['status'],'applied')
        self.assertEqual(report['approved'],['R1'])
        self.assertIn('Include the verified commit.',(dst/'release/SKILL.md').read_text())
        self.assertEqual((dst/'release/guide.txt').read_bytes(),(self.source[0].parent/'guide.txt').read_bytes())
        self.assertEqual((dst/'release/guide.txt').stat().st_mode & 0o777,0o750)
        self.assertFalse((dst/'resume').exists())
        self.assertNotIn('Include the verified commit.',self.source[0].read_text())

    def test_refuses_outside_workspace_and_body_edits_to_unused_skill(self):
        self.write_session(self.root)
        with self.assertRaisesRegex(ValueError,'different workspace'):
            self.collect()
        self.write_session(self.repo)
        packet = self.collect()
        answer = self.response()
        answer['accepted'][0]['skill'] = 'resume'
        with self.assertRaisesRegex(ValueError,'invoked skill'):
            self.propose(packet,answer)

    def test_a_wrong_path_or_failed_skill_call_does_not_authorize_a_body_edit(self):
        args = [
            ('assistant',[{'type':'tool_use','name':'Read','id':'read-1',
                           'input':{'file_path':str(self.root/'other/release/SKILL.md')}}]),
            ('user',[{'type':'tool_result','tool_use_id':'read-1','content':'a different skill'}]),
            ('assistant',[{'type':'tool_use','name':'Skill','id':'skill-1','input':{'skill':'release'}}]),
            ('user',[{'type':'tool_result','tool_use_id':'skill-1','is_error':True,'content':'unavailable'}]),
        ]
        rows = [{'type':role,'message':{'role':role,'content':content},
                 'sessionId':'fixture','cwd':str(self.repo)} for role,content in args]
        self.session.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        captured = self.collect()
        self.assertEqual(cli.read(captured['path'])['used'],[])
        plan = self.response()
        plan['accepted'][0]['evidence']=['E4.1']
        with self.assertRaisesRegex(ValueError,'invoked skill'):
            self.propose(captured,plan)

    def test_existing_local_copy_takes_precedence_and_drift_blocks_apply(self):
        dest = self.repo/'.claude/skills/release'
        dest.mkdir(parents=True)
        original = self.source[0].read_text().replace('Original rule.', 'Locally customized rule.')
        loaded = dest/'SKILL.md'
        loaded.write_text(original)
        p = self.collect()
        plan = self.propose(p)
        self.assertEqual(cli.read(plan['path'])['accepted'][0]['before'],'Locally customized rule.')
        loaded.write_text(original+'Later edit.\n')
        with self.assertRaisesRegex(ValueError,'changed; collect again'):
            self.apply(plan,['R1'])
        self.assertTrue(loaded.read_text().endswith('Later edit.\n'))
        self.assertFalse((self.root/'receipt.json').exists())

    def test_partial_application_retains_receipt_and_can_apply_remaining_selection(self):
        plan = self.propose(self.collect())
        rename = Path.rename

        def fail_second(source,destination):
            if destination.name=='resume':
                raise OSError('fixture copy failure')
            return rename(source,destination)

        with patch.object(Path,'rename',fail_second):
            with self.assertRaisesRegex(OSError,'fixture copy failure'):
                self.apply(plan,['R1','R2'])
        receipt = cli.read(self.root/'receipt.json')
        self.assertEqual(receipt['status'],'applying')
        self.assertEqual((len(receipt['skills']),len(receipt['planned'])),(1,2))
        second = self.apply(plan,['R2'],'retry.json')
        self.assertEqual(second['status'],'applied')
        self.assertIn('description: Recover an interrupted run.',(self.repo/'.claude/skills/resume/SKILL.md').read_text())
