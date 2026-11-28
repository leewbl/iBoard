import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, stack_autopilot


class Reader:
    def __init__(self):
        self.heads = {4:'4'*40, 5:'5'*40}
        self.draft = False

    def repository(self, repo):
        return {'default_branch':'main'}

    def pull(self, repo, number):
        return {'number':number,'url':f'https://github.com/{repo}/pull/{number}',
                'headRefOid':self.heads[number],'baseRefOid':'0'*40 if number==4 else self.heads[4],
                'headRefName':f'branch-{number}','baseRefName':'main' if number==4 else 'branch-4',
                'state':'OPEN','isDraft':self.draft and number==5,'isCrossRepository':False,
                'mergeable':'MERGEABLE','mergeStateStatus':'CLEAN','reviewDecision':'',
                'statusCheckRollup':[{'status':'COMPLETED','conclusion':'SUCCESS'}]}

    def thread_page(self, repo, number, cursor):
        return {**self.pull(repo,number),'reviewThreads':{'pageInfo':{'hasNextPage':False},'nodes':[]}}


class StackAutopilotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.reader = Reader()
        self.calls = []
        config = {'repo':'alice/fork','checkout':'.','image':'image','seconds':60,'members':[]}
        ids = self.root/'requirements.txt'
        ids.write_bytes(b'Original requirement\r\n')
        for n in (4,5):
            config['members'].append({'number':n,'request':str(ids),'kind':'feature','surface':'non-ui',
                                      'required_ci_workflows':['tests'],'seconds':30,
                                      'criteria':[{'id':str(n),'text':'  Correct behavior  '}]})
        manifest = self.root/'manifest.json'
        manifest.write_text(json.dumps(config))
        self.plan = Path(stack_autopilot.freeze(manifest,self.root/'run',reader=self.reader)['plan'])
        ids.write_text('external input changed after freezing')

    def verify(self, **args):
        self.calls.append(args['number'])
        self.assertLessEqual(args['seconds'],30)
        self.assertEqual(args['request'].read_text(),'Original requirement\n')
        self.assertEqual(args['criteria'][0]['text'],'Correct behavior')
        task = args['tasks_root']/args['task_id']/'task.json'
        task.parent.mkdir(parents=True)
        cli.write(task,{'schema':cli.SCHEMA,'criteria':args['criteria'],'kind':args['kind'],'surface':args['surface'],
                        'required_ci_workflows':args['required_ci_workflows'],'status':'running'})
        file = args['request'].read_bytes()
        for name in ('issue.txt','input-base.txt'):
            task.with_name(name).write_bytes(file)
        digest = cli.digest(args['request'])
        cli.write(task.with_name('input-stage.json'),{'schema':cli.SCHEMA,'phase':'ready','input_digest':None,
                  'base_digest':digest,'request_digest':digest,'run_settings':{
                      key:args[key] for key in ('image','source_mode','candidate_image','build_source_image','memory_mb')
                  }|{'pr_repo':args['repo']}})
        return {'status':'verified'}

    def test_dispatches_in_order_and_reuses_evidence_when_ci_gates_change(self):
        with patch.object(stack_autopilot.stack_verify,'_proof',return_value={'status':'verified'}), patch.object(
                stack_autopilot.delivery,'_required_workflows_pass',return_value=True) as workflows:
            self.reader.draft = True
            result = stack_autopilot.run(self.plan,reader=self.reader,verify=self.verify)
            self.assertEqual(result['status'],'verified-awaiting-gates')
            self.assertEqual(result['gates'],[{'pr':5,'reason':'draft-pr'}])
            self.assertEqual(self.calls,[4,5])
            self.reader.draft = False
            result = stack_autopilot.run(self.plan,reader=self.reader,verify=self.verify)
            self.assertEqual(result['status'],'handoff-ready')
            self.assertEqual(self.calls,[4,5])
            self.assertEqual(workflows.call_args.kwargs['base_branch'],'branch-4')
            self.assertEqual(cli.read(Path(result['report'])),result)

    def test_head_drift_prevents_dispatch_and_cannot_credit_previous_proof(self):
        self.reader.heads[4] = 'a'*40
        result = stack_autopilot.run(self.plan,reader=self.reader,verify=self.verify)
        self.assertEqual(result['status'],'verification-blocked')
        self.assertEqual(result['frontier'],4)
        self.assertEqual(self.calls,[])

    def test_missing_member_ci_yields_handoff_gate_without_reverification(self):
        with patch.object(stack_autopilot.stack_verify,'_proof',return_value={'status':'verified'}), patch.object(
                stack_autopilot.delivery,'_required_workflows_pass',return_value=False):
            result = stack_autopilot.run(self.plan,reader=self.reader,verify=self.verify)
            self.assertEqual(result['status'],'verified-awaiting-gates')
            self.assertEqual([item['reason'] for item in result['gates']],['required-ci-not-proven']*2)
            self.assertEqual(self.calls,[4,5])

    def test_proof_replaced_during_ci_cannot_be_handed_off(self):
        passed = {'status':'verified','review_sha256':'original'}
        changed = {**passed,'review_sha256':'changed'}
        with patch.object(stack_autopilot.stack_verify,'_proof',side_effect=[passed,passed,changed,passed]), patch.object(
                stack_autopilot.delivery,'_required_workflows_pass',return_value=True):
            result = stack_autopilot.run(self.plan,reader=self.reader,verify=self.verify)
        self.assertEqual(result['status'],'verified-awaiting-gates')
        self.assertEqual(result['gates'],[{'pr':4,'reason':'independent-proof-changed-during-handoff'}])
