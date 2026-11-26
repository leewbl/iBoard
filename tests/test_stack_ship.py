import json
from contextlib import ExitStack
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, delivery, stack_rebase, stack_ship


REPO = 'alice/project'


class ForkFixture:
    def __init__(self, checkout, remote, members):
        self.checkout, self.remote, self.members = checkout, remote, members
        self.writes = []
        self.opened_after_push = set()
        self.landed = set()
        self.retargeted = set()
        self.extra_merge_commit = False
        self.squash = {}
        self.squash_next = False
        self.rebased_heads = {}

    def login(self):
        return 'alice'

    def repository(self, _):
        return {'fork':True, 'default_branch':'main', 'owner':{'login':'alice'}}

    def branch(self, _, branch):
        return cli.sha(self.remote, f'refs/heads/{branch}')

    def contains(self, _, head, trunk):
        return subprocess.run(['git','-C',str(self.checkout),'merge-base','--is-ancestor',head,trunk],
                              check=False).returncode == 0

    def squash_result(self, repo, number, head, base, trunk):
        sha = self.squash.get(number)
        if not sha or not self.contains(repo,sha,trunk):
            return False
        parents = cli.git(self.checkout,'show','-s','--format=%P',sha).split()
        return (parents==[base] and cli.git(self.checkout,'rev-parse',f'{sha}^{{tree}}')
                == cli.git(self.checkout,'rev-parse',f'{head}^{{tree}}'))

    def pull(self, repo, number):
        item = self.members[number-1]
        main = number == 1 or number in self.retargeted
        return {'number':number, 'url':f'https://github.com/{repo}/pull/{number}',
                'state':'MERGED' if number in self.landed else 'OPEN', 'isDraft':False, 'isCrossRepository':False,
                'headRefOid':self.rebased_heads.get(number,item['head']), 'headRefName':item['head_branch'],
                'baseRefOid':self.branch(repo,'main') if main else item['base'],
                'baseRefName':'main' if main else item['base_branch'],
                'mergeable':'MERGEABLE','mergeStateStatus':'CLEAN','reviewDecision':'',
                'statusCheckRollup':[{'status':'COMPLETED','conclusion':'SUCCESS'}]}

    def thread_page(self, repo, number, cursor):
        return {**self.pull(repo, number),
                'reviewThreads':{'pageInfo':{'hasNextPage':False},'nodes':[]}}

    def retarget(self, repo, number):
        self.writes.append(('retarget',number))
        self.retargeted.add(number)

    def push_rebased(self, checkout, repo, branch, old, head):
        number = next(item['number'] for item in self.members if item['head_branch']==branch)
        ref = 'refs/heads/'+branch
        cli.git(checkout,'push',f'--force-with-lease={ref}:{old}',str(self.remote),f'{head}:{ref}')
        self.rebased_heads[number] = cli.sha(self.remote,ref)
        self.writes.append(('update',number))

    def merge(self, task, repo, number, github, *, stack_binding, revalidate, deadline):
        revalidate()
        self.writes.append(('land',number))
        head = stack_binding['head']
        if self.extra_merge_commit:
            tree = cli.git(self.checkout,'rev-parse',f'{head}^{{tree}}')
            head = cli.git(self.checkout,'commit-tree',tree,'-p',stack_binding['base'],'-p',head,'-m','Merge on GitHub')
        elif self.squash_next:
            tree = cli.git(self.checkout,'rev-parse',f'{head}^{{tree}}')
            head = cli.git(self.checkout,'commit-tree',tree,'-p',stack_binding['base'],'-m','Squash on GitHub')
            self.squash[number] = head
        delivery._fast_forward_push(self.checkout,str(self.remote),stack_binding['base'],head)
        if number not in self.opened_after_push:
            self.landed.add(number)
        return {'merged':number in self.landed, 'verified_head':stack_binding['head']}


class StackShippingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        checkout, remote = self.root/'repo', self.root/'fork.git'
        subprocess.run(['git','init','-q',str(checkout)],check=True)
        subprocess.run(['git','init','--bare','-q',str(remote)],check=True)
        cli.git(checkout,'config','user.name','Fixture')
        cli.git(checkout,'config','user.email','fixture@example.invalid')
        hashes = []
        for i in range(3):
            (checkout/'file').write_text(str(i))
            cli.git(checkout,'add','file')
            cli.git(checkout,'commit','-qm',str(i))
            hashes.append(cli.sha(checkout,'HEAD'))
        cli.git(checkout,'push',str(remote),f'{hashes[0]}:refs/heads/main')
        members = []
        for i in (1,2):
            task = self.root/f'task-{i}.json'
            task.write_text('{}')
            members.append({'number':i,'base':hashes[i-1],'head':hashes[i],
                            'base_branch':'main' if i==1 else 'first', 'head_branch':'first' if i==1 else 'second',
                            'task':str(task),'ci_workflows':['CI'],'proof':{'status':'verified'}})
        self.github = ForkFixture(checkout,remote,members)
        self.plan = self.root/'plan.json'
        cli.write(self.plan,{'schema':cli.SCHEMA,'repo':REPO,'trunk':'main','members':members,'ceiling':2})
        isolation = patch.object(stack_ship,'_proof',return_value={})
        isolation.start()
        self.addCleanup(isolation.stop)

    def execute(self):
        return stack_ship.execute(self.plan,github=self.github,merge=self.github.merge)

    def test_lands_parent_and_retargets_child_at_the_same_commit_once(self):
        result = self.execute()
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['landed'],[1,2])
        self.assertEqual(self.github.branch(REPO,'main'),self.github.members[1]['head'])
        expected = [('land',1),('retarget',2),('land',2)]
        self.assertEqual(self.github.writes,expected)
        self.assertEqual(self.execute()['status'],'complete')
        self.assertEqual(self.github.writes,expected)
        events = cli.read(Path(result['journal']))['events']
        self.assertEqual([e['phase'] for e in events],
                         ['merge-intent','landed','retarget-intent','retargeted','merge-intent','landed'])

    def test_uncertain_merge_is_never_repeated_and_can_resume_after_confirmation(self):
        self.github.opened_after_push.add(1)
        result = self.execute()
        self.assertEqual(result['status'],'awaiting-confirmation')
        self.assertEqual(self.execute()['reason'],'merge-outcome-unconfirmed')
        self.assertEqual(self.github.writes,[('land',1)])
        self.github.landed.add(1)
        self.assertEqual(self.execute()['status'],'complete')
        self.assertEqual(self.github.writes,[('land',1),('retarget',2),('land',2)])

    def test_uncertain_retarget_is_confirmed_by_the_remote_before_landing(self):
        change = self.github.retarget

        def uncertain(*args):
            change(*args)
            raise delivery.DeliveryError('retarget response was lost')

        self.github.retarget = uncertain
        result = self.execute()
        self.assertEqual(result['status'],'awaiting-confirmation')
        self.assertEqual(result['landed'],[1])
        self.assertEqual(self.github.writes,[('land',1),('retarget',2)])
        self.assertEqual(self.execute()['status'],'complete')
        self.assertEqual(self.github.writes,[('land',1),('retarget',2),('land',2)])

    def test_distinct_merge_result_requires_reverification_before_child_changes(self):
        self.github.extra_merge_commit = True
        result = self.execute()
        self.assertEqual(result['reason'],'fresh-base-verification-required')
        self.assertEqual(result['landed'],[1])
        self.assertEqual(result['pr'],2)
        self.assertNotEqual(result['current_main'],result['verified_base'])
        self.assertEqual(self.github.writes,[('land',1)])

    def test_restack_replays_the_child_and_dispatches_the_original_acceptance_on_new_main(self):
        self.github.squash_next = True
        shipment = self.execute()
        self.assertEqual(shipment['reason'], 'fresh-base-verification-required')
        child = self.github.members[1]
        old_task = Path(child['task']).resolve()
        request = b'Check the requirement again after the merge.\n'
        request_file = self.root/'request.txt'
        request_file.write_bytes(request)
        criterion = [{'id':'behavior','text':'preserve the required output','revision':2}]
        data = {'repo':str(self.github.checkout),'kind':'feature','surface':'non-ui','criteria':criterion,
                'criterion_changes':[{'id':'behavior','action':'change','reason':'clarified scope'}]}
        stage = {'run_settings':{'image':'local-image','source_mode':'workspace','memory_mb':512}}
        state = {'plan':{'status':'ready'},'issue_digest':cli.digest(request_file)}
        calls = []

        def launch(**incoming):
            calls.append(incoming)
            target = incoming['verification_target']
            self.assertEqual(incoming['retained_contract']['criteria'], criterion)
            self.assertEqual(incoming['retained_contract']['changes'], data['criterion_changes'])
            self.assertEqual(incoming['issue_file'].read_bytes(), request)
            self.assertEqual(target['base'],shipment['current_main'])
            self.assertNotEqual(target['head'],child['head'])
            self.assertEqual((incoming['repo']/'file').read_text(),'2')
            self.assertEqual(cli.git(incoming['repo'],'merge-base',target['base'],target['head']),target['base'])
            return {'phase':'complete','verification':{'verdict':'PASS','base':target['base'],'head':target['head'],
                                                       'criterion_digest':cli.criteria_digest(data)}}

        with patch.object(stack_rebase,'_retained',return_value=(old_task,data,stage,state,request)):
            replayed = stack_rebase.prepare(self.plan,task_id='new',tasks_root=self.root/'tasks',
                                            workspaces_root=self.root/'worktrees',seconds=45,
                                            reader=self.github,launch=launch)
        self.assertEqual(replayed['status'],'verified')
        self.assertTrue(replayed['patch_id_matches'])
        self.assertEqual(len(calls),1)
        self.assertEqual(self.github.writes,[('land',1)])
        self.assertEqual(cli.sha(self.github.checkout,'HEAD'),child['head'])

        task = Path(replayed['task'])
        task.parent.mkdir()
        target = calls[0]['verification_target']
        target['request_digest'] = state['issue_digest']
        cli.write(task,{'schema':cli.SCHEMA,'criteria':criterion, 'repo':replayed['worktree'],
                        'verification_target':target,'required_ci_workflows':['CI'],
                        'kind':data['kind'],'surface':data['surface'],'criterion_changes':data['criterion_changes']})
        receipt_path = Path(replayed['receipt'])
        receipt = cli.read(receipt_path)
        receipt['verification'].update(review='fixture-review',review_sha256='fixture-hash')
        cli.write(receipt_path,receipt)
        cli.git(self.github.checkout,'push',str(self.github.remote),child['head']+':refs/heads/second')
        push = self.github.push_rebased

        def lost_response(*args):
            push(*args)
            raise delivery.DeliveryError('lost push response')

        self.github.push_rebased=lost_response
        with ExitStack() as patches:
            patches.enter_context(patch.object(stack_rebase,'_retained',
                                               return_value=(old_task,data,stage,state,request)))
            patches.enter_context(patch.object(stack_rebase.stack_verify,'_proof_locked',
                                               return_value={'status':'verified','review':'fixture-review',
                                                             'review_sha256':'fixture-hash'}))
            patches.enter_context(patch.object(cli,'gate',return_value=(target['head'],[])))
            uncertain = stack_rebase.publish_rebased(receipt_path,reader=self.github)
            self.assertEqual(uncertain['status'],'awaiting-confirmation')
            settled = stack_rebase.publish_rebased(receipt_path,reader=self.github)
            self.assertEqual(settled['status'],'updated')
        self.assertEqual(self.github.writes,[('land',1),('retarget',2),('update',2)])
        self.assertEqual(cli.sha(self.github.remote,'refs/heads/second'),target['head'])
        self.assertEqual(self.github.branch(REPO,'main'),target['base'])

    def test_changed_plan_cannot_resume_a_started_shipping_run(self):
        self.assertEqual(self.execute()['status'],'complete')
        original = json.loads(self.plan.read_text())
        original['frozen_at'] = 'later'
        cli.write(self.plan,original)
        with self.assertRaisesRegex(stack_ship.ShippingError,'changed plan'):
            self.execute()
