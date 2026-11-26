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

    def run_arena(self, editor=None, judge=None, sandbox=None):
        return arena.run(self.manifest,self.root/'out',editor=editor or self.edit,judge=judge or self.judge,
                         picker=self.picker,sandbox=sandbox or self.runcheck)

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

    def test_recovers_one_invalid_scorecard_and_retains_attempts(self):
        for invalid in ('extra-prose', 'missing-candidate'):
            with self.subTest(invalid=invalid):
                calls = []

                def picker(prompt, timeout):
                    if prompt.startswith('Compare these'):
                        return self.picker(prompt, timeout)
                    calls.append(prompt)
                    packet = prompt.rsplit('\n', 1)[-1]
                    score = self.score('Score\n' + packet, 'A1')
                    if len(calls) == 1:
                        if invalid == 'extra-prose':
                            return json.dumps(score) + '\nNotes after the score.'
                        score['candidates'].pop()
                    return '```json\n' + json.dumps(score) + '\n```'

                output = self.root / invalid
                report = arena.run(self.manifest, output, editor=self.edit, judge=self.judge,
                                   picker=picker, sandbox=self.runcheck)
                self.assertEqual(report['status'], 'verified', report.get('reason'))
                self.assertEqual(len(calls), 2)
                self.assertIn('invalid or ambiguous', calls[1])
                first = json.loads((output / 'scorecard-glm-1.json').read_text())
                second = json.loads((output / 'scorecard-glm-2.json').read_text())
                self.assertNotEqual(first['response'], second['response'])

    def test_recounts_openai_hunks_and_retries_an_inapplicable_patch(self):
        sandbox_root = self.root / 'edition'
        sandbox_root.mkdir()
        calls = []

        def judge(prompt, model, timeout):
            calls.append(prompt)
            if len(calls) == 1:
                patch = 'invalid patch data'
            else:
                patch = ('diff --git a/app.js b/app.js\n--- a/app.js\n+++ b/app.js\n@@ -1,3 +1,7 @@\n'
                         '-exports.normalize = (name) => name;\n'
                         '+exports.normalize = (name) => name.trim().toLowerCase();')
            return {'patch': patch, 'rationale': 'Normalize the existing export.'}

        run = arena.Arena(self.config, sandbox_root, self.edit, judge, self.picker, self.runcheck)
        worktree = run.workspace('model', self.baseline)
        run.edit(worktree, 'Implement this check.', {'provider': 'openai', 'model': 'gpt-fixture'})
        self.assertIn('name.trim().toLowerCase()', (worktree / 'app.js').read_text())
        self.assertEqual(len(calls), 2)
        self.assertIn('did not apply cleanly', calls[1])
        first = json.loads((worktree.parent / 'patch-attempt-1.json').read_text())
        self.assertTrue(first['git_apply_error'])

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

    def configure_designs(self):
        def shape(title,owner):
            return {'title':title,'modules':[{'name':owner,'duty':'Own the normalization algorithm.'}],
                    'state':[{'name':'name','owner':owner}],
                    'interfaces':[{'signature':'normalize(name)','owner':owner,'caller':'public API'}]}
        designs = {'A':shape('Inline normalization','Public export'),
                   'B':shape('Use an internal normalizer','Internal helper')}
        self.config.update(revision=self.baseline,design_alternatives=designs)
        self.manifest.write_text(json.dumps(self.config))
        return designs

    def test_design_comparison_pins_revision_and_preserves_the_exact_winner(self):
        designs = self.configure_designs()
        advanced = 'exports.normalize = () => "advanced branch";\n'
        (self.repo/'app.js').write_text(advanced)
        cli.git(self.repo,'commit','-qam','Advance independently after planning')
        new_head = cli.sha(self.repo,'HEAD')
        implementation = {
            'A':'exports.normalize = (name) => name.trim().toLowerCase();\n',
            'B':'const clean = (value) => value.trim().toLowerCase();\nexports.normalize = clean;\n',
        }
        assigned = {}
        checked_worktrees = []

        def edit(workspace,prompt,timeout,**kwargs):
            self.assertNotEqual(workspace.parent.name,'synthesis')
            self.assertEqual(cli.sha(workspace,'HEAD'),self.baseline)
            received = json.JSONDecoder().raw_decode(prompt.split('ASSIGNED DESIGN:\n',1)[1])[0]
            design = next(label for label,shape in designs.items() if shape==received)
            other = 'A' if design=='B' else 'B'
            self.assertNotIn(designs[other]['title'],prompt)
            assigned[workspace.parent.name] = design
            (workspace/'app.js').write_text(implementation[design])
            return {'exit_code':0,'stdout':json.dumps({'status':'ready','rationale':'Use the assigned owner.'})}

        def judge(prompt, model, timeout):
            if not prompt.startswith('Independently verify the selected'):
                return self.judge(prompt,model,timeout)
            context = json.loads(prompt.split('\n',1)[1])
            self.assertEqual(context['approved_design'],designs[assigned['A1']])
            self.assertEqual(context['selected_candidate']['commit'],context['independent_rerun']['commit'])
            self.assertTrue(context['independent_rerun']['passed'])
            return {'verdict':'PASS','reason':'The assigned design and separate candidate run agree.'}

        def sandbox(workspace,*args,**kwargs):
            checked_worktrees.append((workspace.parent.name,cli.sha(workspace,'HEAD')))
            return self.runcheck(workspace,*args,**kwargs)

        report = self.run_arena(editor=edit,judge=judge,sandbox=sandbox)
        self.assertEqual(report['status'],'verified')
        self.assertEqual(report['baseline'],self.baseline)
        self.assertEqual(report['base_candidate'],'A1')
        self.assertEqual(report['selected_design'],assigned['A1'])
        self.assertEqual({row['id']:row['design'] for row in report['candidates']},assigned)
        winner = next(row for row in report['candidates'] if row['id']=='A1')
        self.assertEqual((report['head'],report['patch_sha256']),(winner['head'],winner['patch_sha256']))
        self.assertEqual(Path(report['patch']).read_bytes(),Path(winner['path']).read_bytes())
        self.assertEqual(report['verification'],winner['verification'])
        independent = cli.read(Path(report['independent_review']['report']))
        self.assertNotEqual(independent['independent_sandbox']['report'],report['verification']['report'])
        self.assertEqual(checked_worktrees[-1],('independent',winner['head']))
        self.assertEqual(len(checked_worktrees),4)
        self.assertEqual(cli.sha(self.repo,'HEAD'),new_head)
        self.assertEqual((self.repo/'app.js').read_text(),advanced)
        self.assertEqual(cli.git(self.repo,'status','--porcelain'),'')

    def test_design_comparison_blocks_when_candidates_are_indistinguishable(self):
        self.configure_designs()

        def edit(workspace,prompt,timeout,**kwargs):
            (workspace/'app.js').write_text('exports.normalize = (name) => name.trim().toLowerCase();\n')
            return {'exit_code':0,'stdout':json.dumps({'status':'ready','rationale':'Inline normalization.'})}

        def no_judge(*args):
            self.fail('indistinguishable candidates must be rejected before scorecards')

        report = self.run_arena(editor=edit,judge=no_judge)
        self.assertEqual(report['status'],'blocked')
        self.assertIn('two distinct patches',report['reason'])
        self.assertNotIn('selected_design',report)
        self.assertFalse((self.root/'out'/'result.patch').exists())

    def test_design_comparison_requires_execution_and_cleanup_for_both_candidates(self):
        self.configure_designs()

        def sandbox(workspace,*args,**kwargs):
            result = self.runcheck(workspace,*args,**kwargs)
            if workspace.parent.name=='A2':
                result['cleanup_confirmed'] = False
            return result

        def no_judge(*args):
            self.fail('a missing sandbox result must be rejected before scorecards')

        report = self.run_arena(judge=no_judge,sandbox=sandbox)
        self.assertEqual(report['status'],'blocked')
        self.assertIn('executed sandbox checks',report['reason'])
        self.assertNotIn('selected_design',report)
        self.assertFalse((self.root/'out'/'result.patch').exists())

    def test_design_comparison_withholds_selection_after_independent_failure(self):
        self.configure_designs()

        def sandbox(workspace,*args,**kwargs):
            result = self.runcheck(workspace,*args,**kwargs)
            if workspace.parent.name=='independent':
                result.update(exit_code=1,stdout='')
            return result

        report = self.run_arena(sandbox=sandbox)
        self.assertEqual(report['status'],'blocked')
        self.assertIn('independent sandbox rerun failed',report['reason'])
        self.assertNotIn('selected_design',report)
        self.assertNotIn('base_candidate',report)
        self.assertFalse((self.root/'out'/'result.patch').exists())

    def test_design_comparison_rejects_unpinned_and_duplicate_shapes(self):
        self.configure_designs()
        self.config['revision'] = 'HEAD'
        self.manifest.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(arena.ArenaError,'exact commit'):
            self.run_arena()
        self.config['revision'] = self.baseline
        shapes = self.config['design_alternatives']
        shapes['B'] = {**shapes['A'],'title':'The same design under a different name'}
        self.manifest.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(arena.ArenaError,'different architecture boundaries'):
            self.run_arena()
