import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from workbench import arena, cli


class ArenaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.repo = self.root/'repo'
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        cli.git(self.repo,'config','user.name','Fixture')
        cli.git(self.repo,'config','user.email','fixture@example.invalid')
        (self.repo/'app.js').write_text('exports.normalize = (name) => name;\n')
        (self.repo/'test.js').write_text('// frozen test\n')
        (self.repo/'.gitignore').write_text('scratch.txt\n')
        cli.git(self.repo,'add','.')
        cli.git(self.repo,'commit','-qm','fixture')
        self.baseline = cli.sha(self.repo,'HEAD')
        self.rubric = [{'id':f'criterion{i}','text':f'criterion {i}'} for i in range(3)]
        self.config = {'repo':str(self.repo),'request':'Trim and lowercase a name.','files':['app.js'],
                       'runners':[{'provider':'glm'},{'provider':'glm'}],'judge_model':'gpt-fixture',
                       'rubric':self.rubric,'seconds':90,'image':'image',
                       'check':{'command':['node','/workspace/test.js'],'marker':'PASS','exit_code':0}}
        self.manifest = self.root/'manifest.json'
        self.manifest.write_text(json.dumps(self.config))

    def edit(self, workspace, prompt, timeout, **kwargs):
        name = workspace.parent.name
        if name=='synthesis':
            choice = prompt.rsplit('Selected base: ',1)[1].splitlines()[0]
            other = 'A2' if choice=='A1' else 'A1'
            out = {'grafts':[],'rejected':[{'from':other,'why':'Same behavior; no change warranted.'}]}
        else:
            (workspace/'app.js').write_text(f'exports.normalize = (name) => name.trim().toLowerCase(); // {name}\n')
            out = {'rationale':'Use standard string operations; avoid a regexp.'}
        return {'exit_code':0,'stdout':json.dumps({'status':'ready',**out}),'timed_out':False}

    def score(self, prompt, winner):
        content = json.loads(prompt.split('\n',1)[1])
        self.assertEqual([item['id'] for item in content['candidates']],['A1','A2'])
        self.assertTrue(all(set(item)=={'id','patch','sandbox_check'} for item in content['candidates']))
        return {'candidates':[{'id':label,'scores':[
            {'criterion':item['id'],'score':4 if label==winner else 3,'why':'Observed the proposed behavior.'}
            for item in self.rubric]} for label in ('A1','A2')],'recommendation':winner}

    def judge(self, prompt, model, timeout):
        self.assertEqual(model,'gpt-fixture')
        if prompt.startswith('Independently verify the synthesized'):
            context = json.loads(prompt.split('\n',1)[1])
            self.assertEqual(context['acceptance'],{'path':'test.js','source':'// frozen test\n'})
            self.assertEqual(context['editable_paths'],['app.js'])
            self.assertEqual(context['verified_changed_paths'],['app.js'])
            self.assertEqual(context['baseline']['commit'],self.baseline)
            self.assertFalse(context['baseline']['passed'])
            self.assertTrue(context['synthesis']['passed'])
            self.assertTrue(context['independent_rerun']['passed'])
            self.assertEqual(context['independent_rerun']['commit'],context['synthesis']['commit'])
            return {'verdict':'PASS','reason':'The scoped patch and frozen behavior check agree.'}
        return self.score(prompt,'A2')

    def picker(self, prompt, timeout):
        if prompt.startswith('Compare these'):
            return json.dumps({'base':'A1','reason':'The two rationales favor the smaller boundary.'})
        return json.dumps(self.score(prompt,'A1'))

    def runcheck(self, workspace, image, command, timeout, **kwargs):
        self.assertTrue(kwargs['workspace_read_only'])
        passed = 'trim()' in (workspace/'app.js').read_text()
        return {'stdout':'PASS' if passed else '', 'container_created':True,'cleanup_confirmed':True,
                'exit_code':0 if passed else 1}

    def run_arena(self, editor=None, judge=None):
        return arena.run(self.manifest,self.root/'out',editor=editor or self.edit,judge=judge or self.judge,
                         picker=self.picker,sandbox=self.runcheck)

    def test_blind_scoring_arbitration_and_independent_synthesis_check(self):
        report = self.run_arena()
        self.assertEqual(report['status'],'verified')
        self.assertEqual(report['base_candidate'],'A1')
        self.assertFalse(report['baseline_verification']['passed'])
        self.assertEqual(report['scorecards']['openai']['answer']['recommendation'],'A2')
        self.assertEqual(report['independent_review']['verdict'],'PASS')
        self.assertEqual(Path(report['patch']).is_file(),True)
        self.assertEqual(cli.sha(self.repo,'HEAD'),self.baseline)
        self.assertEqual(cli.git(self.repo,'status','--porcelain'),'')

    def test_verification_failure_withholds_the_synthesized_patch(self):
        def bad_edit(workspace,*args,**kwargs):
            result = self.edit(workspace,*args,**kwargs)
            if workspace.parent.name=='synthesis':
                (workspace/'app.js').write_text('exports.normalize = (name) => name.toLowerCase();\n')
            return result
        report = self.run_arena(bad_edit)
        self.assertEqual(report['status'],'blocked')
        self.assertIn('synthesized artifact failed',report['reason'])
        self.assertFalse((self.root/'out'/'result.patch').exists())

    def test_independent_rejection_withholds_patch_despite_passing_execution(self):
        def judge(prompt, model, timeout):
            if prompt.startswith('Independently verify the synthesized'):
                return {'verdict':'FAIL','reason':'The provided script has no behavioral assertions.'}
            return self.judge(prompt,model,timeout)
        report = self.run_arena(judge=judge)
        self.assertTrue(report['verification']['passed'])
        self.assertEqual(report['independent_review']['verdict'],'FAIL')
        self.assertEqual(report['status'],'blocked')
        self.assertFalse((self.root/'out'/'result.patch').exists())

    def test_identical_scores_can_declare_a_tie_without_skipping_a_candidate(self):
        report = self.run_arena()
        answer = report['scorecards']['glm']['answer']
        answer['recommendation'] = 'tie'
        with self.assertRaisesRegex(arena.ArenaError,'contradicts'):
            arena._scores(answer,report['candidates'],self.rubric)
        for candidate in answer['candidates']:
            for score in candidate['scores']:
                score['score'] = 4
        self.assertEqual(arena._scores(answer,report['candidates'],self.rubric)[0],'A1')

    def test_ignored_out_of_scope_data_is_rejected(self):
        def ignored_edit(workspace,*args,**kwargs):
            result = self.edit(workspace,*args,**kwargs)
            if workspace.parent.name=='A1':
                (workspace/'scratch.txt').write_text('cannot enter the comparison')
            return result
        report = self.run_arena(ignored_edit)
        self.assertEqual(report['status'],'blocked')
        self.assertEqual(report['dropouts'][0]['error'],'a model edited outside the frozen scope')
        self.assertFalse((self.root/'out'/'result.patch').exists())
