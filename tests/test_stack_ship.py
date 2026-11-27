import json
from contextlib import ExitStack
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, delivery, run_control, stack_rebase, stack_ship


REPO = 'alice/project'


class ForkFixture:
    def __init__(self, checkout, remote, members):
        self.checkout, self.remote, self.members = checkout, remote, members
        self.writes = []
        self.opened_after_push = set()
        self.landed = set()
        self.retargeted = set()
        self.auto_retarget = set()
        self.extra_merge_commit = False
        self.squash = {}
        self.squash_next = False
        self.rebased_heads = {}
        self.merge_bindings = []
        self.push_bindings = []
        self.pending_checks = set()

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
        base_branch = 'main' if number in self.retargeted else item['base_branch']
        checks = ([{'status':'IN_PROGRESS','conclusion':None}] if number in self.pending_checks else
                  [{'status':'COMPLETED','conclusion':'SUCCESS'}])
        return {'number':number, 'url':f'https://github.com/{repo}/pull/{number}',
                'state':'MERGED' if number in self.landed else 'OPEN', 'isDraft':False, 'isCrossRepository':False,
                'headRefOid':self.rebased_heads.get(number,item['head']), 'headRefName':item['head_branch'],
                'baseRefOid':self.branch(repo,base_branch), 'baseRefName':base_branch,
                'mergeable':'MERGEABLE','mergeStateStatus':'CLEAN','reviewDecision':'',
                'statusCheckRollup':checks}

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
        self.push_bindings.append((number, old, head))

    def merge(self, task, repo, number, github, *, stack_binding, revalidate, deadline):
        revalidate()
        self.writes.append(('land',number))
        head = stack_binding['head']
        self.merge_bindings.append((number, stack_binding['base'], head, task))
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
            if number in self.auto_retarget:
                merged_branch = self.members[number-1]['head_branch']
                self.retargeted.update(member['number'] for member in self.members
                                       if member['base_branch'] == merged_branch)
                cli.git(self.remote,'update-ref','-d',f'refs/heads/{merged_branch}')
        return {'merged':number in self.landed, 'verified_head':stack_binding['head']}


class StackShippingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
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
        cli.git(checkout,'push',str(remote),f'{hashes[1]}:refs/heads/first',f'{hashes[2]}:refs/heads/second')
        self.github = ForkFixture(checkout,remote,members)
        self.plan = self.root/'plan.json'
        cli.write(self.plan,{'schema':cli.SCHEMA,'repo':REPO,'trunk':'main','members':members,'ceiling':2})
        self.shipping_proof = stack_ship._proof
        isolation = patch.object(stack_ship,'_proof',return_value={})
        isolation.start()
        self.addCleanup(isolation.stop)

    def execute(self):
        return stack_ship.execute(self.plan,github=self.github,merge=self.github.merge)

    def append_member(self, branch, filename):
        previous = self.github.members[-1]
        number = previous['number'] + 1
        checkout = self.github.checkout
        destination = checkout / filename
        destination.write_text(f'Change for PR {number}\n')
        cli.git(checkout,'add',filename)
        cli.git(checkout,'commit','-qm',f'Change for PR {number}')
        head = cli.sha(checkout,'HEAD')
        cli.git(checkout,'push',str(self.github.remote),f'{head}:refs/heads/{branch}')
        task = self.root/f'task-{number}.json'
        cli.write(task,{'schema':cli.SCHEMA,'required_ci_workflows':['CI']})
        member = {'number':number,'base':previous['head'],'head':head,'base_branch':previous['head_branch'],
                  'head_branch':branch,'task':str(task),'ci_workflows':['CI'],'proof':{'status':'verified'}}
        self.github.members.append(member)
        return member

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
        cli.write(old_task,{'schema':cli.SCHEMA,**data})
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
        target['settings'] = stage['run_settings']
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

        def hold_before_write(task, repo, number, github, **bindings):
            self.assertEqual(number,2)
            run_control.hold(old_task,'Pause the original task before merge')
            return self.github.merge(task,repo,number,github,**bindings)

        writes = list(self.github.writes)
        at_write = stack_ship.execute(self.plan,github=self.github,merge=hold_before_write)
        for stopped in (at_write,self.execute()):
            self.assertEqual((stopped['status'],stopped['landed'],stopped['pr']),('blocked',[1],2))
            self.assertIn('Pause the original task before merge',stopped['reason'])
        self.assertEqual(self.github.writes,writes)
        self.assertEqual(run_control.release(old_task)['status'],'released')

        self.github.pending_checks.add(2)
        self.assertIn('pending-checks',self.execute()['reason'])
        current = self.github.branch(REPO,'main')
        tree = cli.git(self.github.checkout,'rev-parse',f'{current}^{{tree}}')
        advanced = cli.git(self.github.checkout,'commit-tree',tree,'-p',current,'-m','Advance main while CI waits')
        delivery._fast_forward_push(self.github.checkout,str(self.github.remote),current,advanced)
        self.github.pending_checks.remove(2)
        for _ in range(2):
            stopped = self.execute()
            self.assertEqual((stopped['status'],stopped['landed'],stopped['pr']),('blocked',[1],2))
            self.assertEqual(stopped['reason'],'published-restack-base-moved')
            self.assertEqual((stopped['verified_base'],stopped['current_main']),(current,advanced))
        self.assertEqual(self.github.writes,writes)

    def test_restack_rejects_original_specialized_evidence_it_cannot_preserve(self):
        member = self.github.members[1]
        task = Path(member['task'])
        for field in ('coverage_swarm','performance_contract','ci_skip_policy'):
            with self.subTest(field=field):
                cli.write(task,{'schema':cli.SCHEMA,field:{'present':True}})
                with patch.object(stack_rebase.stack_verify,'_proof_locked',return_value=member['proof']):
                    with self.assertRaisesRegex(stack_rebase.RebaseError,
                                                'restack cannot preserve required evidence or policy: '+field):
                        stack_rebase._retained(REPO,member)

    def test_seed_image_is_rejected_before_build_or_review_if_main_changed_lockfile(self):
        first,child = self.github.members
        checkout = self.github.checkout
        previous_child = child['head']
        cli.git(checkout,'checkout','--detach',first['head'])
        (checkout/'pnpm-lock.yaml').write_text('lockfileVersion: 9\n')
        cli.git(checkout,'add','pnpm-lock.yaml')
        cli.git(checkout,'commit','-qm','Change preinstalled dependencies')
        first['head'] = cli.sha(checkout,'HEAD')
        cli.git(checkout,'cherry-pick',previous_child)
        child.update(base=first['head'],head=cli.sha(checkout,'HEAD'))
        cli.git(checkout,'push',f'--force-with-lease=refs/heads/second:{previous_child}',str(self.github.remote),
                f"{first['head']}:refs/heads/first",f"{child['head']}:refs/heads/second")
        frozen = cli.read(self.plan)
        frozen['members'] = self.github.members
        cli.write(self.plan,frozen)
        self.github.squash_next = True
        shipping = self.execute()
        self.assertEqual((shipping['landed'],shipping['pr'],shipping['reason']),
                         ([1],2,'fresh-base-verification-required'))
        self.assertEqual(cli.git(checkout,'diff','--name-only',first['base'],shipping['current_main'],'--',
                                 'pnpm-lock.yaml'),'pnpm-lock.yaml')

        data = {'repo':str(checkout),'kind':'feature','surface':'non-ui',
                'criteria':[{'id':'behavior','text':'Keep output','revision':1}],'criterion_changes':[]}
        stage = {'run_settings':{'image':'previous-image','source_mode':'image','memory_mb':512}}
        state = {'plan':{'status':'ready'},'issue_digest':'fixture-request'}
        retained = (Path(child['task']),data,stage,state,b'Original request\n')
        with (patch.object(stack_rebase,'_retained',return_value=retained),
              patch.object(stack_rebase.workflow,'build_source_image') as build_image,
              patch.object(stack_rebase.verify_openclaw,'build_browser_image') as build_browser,
              patch.object(stack_rebase.start,'start') as launch):
            blocked = stack_rebase.prepare(self.plan,task_id='changed-dependencies',tasks_root=self.root/'tasks',
                                            workspaces_root=self.root/'worktrees',seconds=45,reader=self.github,
                                            seed_image='sha256:'+'a'*64,launch=launch)
        self.assertEqual(blocked['status'],'verification-blocked')
        self.assertIn('cannot reuse preinstalled dependencies',blocked['reason'])
        self.assertEqual(cli.read(Path(blocked['receipt']))['status'],'verification-blocked')
        build_image.assert_not_called()
        build_browser.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(self.github.writes,[('land',1)])

    def test_continuation_lands_three_squashed_members_with_fresh_review_at_each_layer(self):
        self.assert_multi_layer_continuation(interrupt_last_push=False)

    def test_continuation_resumes_lost_push_response_with_original_budget_and_roots(self):
        self.assert_multi_layer_continuation(interrupt_last_push=True)

    def test_continuation_replays_again_when_main_advances_during_ci(self):
        self.assert_multi_layer_continuation(frontier_change='advance')

    def test_continuation_reconciles_lost_push_after_main_advances(self):
        self.assert_multi_layer_continuation(frontier_change='lost-push')

    def test_continuation_rejects_unexpected_published_head(self):
        self.assert_multi_layer_continuation(frontier_change='unknown-head')

    def test_continuation_rejects_rewritten_published_base(self):
        self.assert_multi_layer_continuation(frontier_change='rewrite-main')

    def test_continuation_replays_after_confirmed_retarget_and_main_advancement(self):
        self.assert_multi_layer_continuation(frontier_change='retargeted')

    def test_continuation_reconciles_lost_retarget_after_main_advances(self):
        self.assert_multi_layer_continuation(frontier_change='lost-retarget')

    def assert_multi_layer_continuation(self, *, interrupt_last_push=False, frontier_change=None):
        third = self.append_member('third','third.txt')
        outside = self.append_member('fourth','outside-plan.txt')
        frozen = cli.read(self.plan)
        frozen.update(members=self.github.members[:3],ceiling=3)
        cli.write(self.plan,frozen)
        plan_digest = cli.digest(self.plan)
        initial_retarget = frontier_change in ('retargeted','lost-retarget')
        self.github.squash_next = not initial_retarget
        if not initial_retarget:
            self.github.auto_retarget.add(1)
        cli.write(Path(frozen['members'][0]['task']),{'schema':cli.SCHEMA,'required_ci_workflows':['CI']})

        retained, reviews, replayed = {}, {}, {}
        replay_attempts = []
        for member in self.github.members[1:3]:
            number = member['number']
            request = f'Original acceptance input for PR {number}.\n'.encode()
            request_path = self.root/f'request-{number}.txt'
            request_path.write_bytes(request)
            task = Path(member['task'])
            criteria = [{'id':f'behavior-{number}','text':f'Preserve behavior {number}','revision':2}]
            data = {'schema':cli.SCHEMA,'repo':str(self.github.checkout),'kind':'feature','surface':'non-ui',
                    'criteria':criteria,'criterion_changes':[{'id':criteria[0]['id'],'reason':'test correction'}],
                    'required_ci_workflows':['CI']}
            cli.write(task,data)
            stage = {'run_settings':{'image':'offline-image','source_mode':'workspace','memory_mb':512}}
            state = {'plan':{'status':'ready'},'issue_digest':cli.digest(request_path)}
            retained[number] = task,data,stage,state,request

        def retained_task(repo, member):
            self.assertEqual(repo,REPO)
            self.assertEqual(member,self.github.members[member['number']-1])
            return retained[member['number']]

        def launch(**arguments):
            target = arguments['verification_target']
            number = target['pr']
            original = self.github.members[number-1]
            task,data,stage,state,request = retained[number]
            self.assertEqual(arguments['issue_file'].read_bytes(),request)
            self.assertEqual(arguments['retained_contract'],{'criteria':data['criteria'],
                                                             'changes':data['criterion_changes']})
            self.assertEqual(arguments['implementer'],'pr-contributor')
            self.assertEqual(arguments['required_ci_workflows'],['CI'])
            binding = {'shipping_plan_sha256':plan_digest,'old_task':str(task),
                       'old_head':original['head'],'old_base':original['base']}
            if number in replayed:
                previous = replayed[number]
                binding.update(previous_receipt_sha256=cli.digest(Path(previous['receipt'])),
                               lease_head=previous['head'])
            self.assertEqual(target['restacked_from'],binding)
            self.assertEqual(target['base'],self.github.branch(REPO,'main'))
            self.assertNotEqual(target['head'],original['head'])
            worktree = arguments['repo']
            head,base = target['head'],target['base']
            self.assertEqual(cli.git(worktree,'merge-base',base,head),base)
            self.assertEqual(cli.git(worktree,'diff','--name-only',base,head), 'file' if number == 2 else 'third.txt')
            self.assertEqual(cli.git(worktree,'rev-list','--count',f'{base}..{head}'),'1')
            self.assertEqual(cli.git(worktree,'show',f'{head}:file'),'2')
            output = arguments['tasks_root']/arguments['task_id']/'task.json'
            cli.write(output,{**data,'repo':str(worktree),'base_commit':base,
                              'verification_target':{**target,'request_digest':state['issue_digest'],
                                                     'settings':stage['run_settings']}})
            review = output.with_name('independent-review.md')
            review.write_text(f'Independent acceptance for PR {number}, {base}..{head}\n')
            proof = {'status':'verified','review':str(review),'review_sha256':cli.digest(review),
                     'verifier':f'independent-reviewer-{number}','criterion_digest':cli.criteria_digest(data)}
            reviews[output] = (number,base,head,proof)
            return {'phase':'complete','verification':{'verdict':'PASS','repo':REPO,'pr':number,
                                                       'base':base,'head':head,**proof}}

        def independent_proof(repo, member, task):
            self.assertEqual(repo,REPO)
            if task not in reviews:
                return {'status':'verified'}
            number,base,head,proof = reviews[task]
            self.assertEqual((member['number'],member['base'],member['head']),(number,base,head))
            self.assertEqual(cli.digest(Path(proof['review'])),proof['review_sha256'])
            return proof

        seed = 'sha256:'+'a'*64

        def prepare_rebase(path, **options):
            self.assertEqual(options['seed_image'],seed)
            landed_count = len(self.github.landed)
            self.assertEqual(options['expected_base'],self.github.branch(REPO,'main'))
            if landed_count == 1:
                self.assertEqual(self.github.pull(REPO,2)['baseRefName'],'main')
                expected = [('land',1)]
                if initial_retarget:
                    expected.append(('retarget',2))
                expected.extend([('update',2)] * len(replay_attempts))
                self.assertEqual(self.github.writes,expected)
            replay = stack_rebase.prepare(path,launch=launch,**options)
            number = replay['pr']
            self.assertEqual(number,landed_count+1)
            if number in replayed:
                self.assertIn(frontier_change,('advance','lost-push','rewrite-main'))
                self.assertEqual(replayed[number]['old_patch_id'],replay['old_patch_id'])
                self.assertNotEqual(replayed[number]['head'],replay['head'])
            self.assertEqual(replay['status'],'verified')
            self.assertTrue(replay['patch_id_matches'])
            replayed[number] = replay
            replay_attempts.append(replay)
            return replay

        def publish(receipt, **options):
            applied = stack_rebase.publish_rebased(receipt,**options)
            if applied['status'] == 'updated':
                replay = replayed[applied['pr']]
                self.assertEqual((applied['base'],applied['head']),(replay['base'],replay['head']))
            if applied['pr'] == 2 and applied['status'] == 'updated':
                self.assertEqual(self.github.pull(REPO,3)['baseRefOid'],replayed[2]['head'])
                self.assertNotEqual(third['base'],replayed[2]['head'])
                saved = receipt.read_bytes()
                receipt.write_bytes(saved+b'\n')
                try:
                    writes = list(self.github.writes)
                    blocked = self.execute()
                    self.assertEqual((blocked['status'],blocked['landed']),('blocked',[1]))
                    self.assertIn('receipt',blocked['reason'])
                    self.assertEqual(self.github.writes,writes)
                finally:
                    receipt.write_bytes(saved)
            self.assertEqual(cli.digest(self.plan),plan_digest)
            return applied

        options = {'tasks_root':self.root/'tasks','workspaces_root':self.root/'worktrees',
                   'seconds':45,'seed_image':seed,'reader':self.github,
                   'ship':lambda path, **settings: stack_ship.execute(path,merge=self.github.merge,**settings),
                   'prepare_rebase':prepare_rebase,'publish':publish}
        if frontier_change:
            self.github.pending_checks.add(2)
        if frontier_change == 'lost-retarget':
            retarget = self.github.retarget

            def lose_retarget_response(repo, number):
                retarget(repo,number)
                if number == 2:
                    raise delivery.DeliveryError('lost child retarget response')

            self.github.retarget = lose_retarget_response
        if interrupt_last_push or frontier_change == 'lost-push':
            push = self.github.push_rebased

            def lose_push_response(checkout, repo, branch, old, head):
                push(checkout,repo,branch,old,head)
                if interrupt_last_push and branch == third['head_branch']:
                    raise delivery.DeliveryError('lost third branch update response')
                if frontier_change == 'lost-push' and branch == 'second' and old == self.github.members[1]['head']:
                    raise delivery.DeliveryError('lost first child branch update response')

            self.github.push_rebased = lose_push_response

        def main_commit(parent, title):
            checkout = self.github.checkout
            tree = cli.git(checkout,'rev-parse',f'{parent}^{{tree}}')
            return cli.git(checkout,'commit-tree',tree,'-p',parent,'-m',title)

        def assert_still_waiting(result):
            self.assertEqual((result['status'],result['landed'],result['pr'],result['ceiling']),
                             ('blocked',[1],2,3),result)
            self.assertIn('pending-checks',result['reason'])

        with (patch.object(stack_rebase,'_retained',side_effect=retained_task),
              patch.object(stack_ship.stack_verify,'_proof_locked',side_effect=independent_proof),
              patch.object(stack_ship,'_proof',side_effect=self.shipping_proof),
              patch.object(cli,'gate',side_effect=lambda data, head:(head,[]))):
            result = stack_rebase.continue_shipping(self.plan,**options)
            if frontier_change:
                lost_responses = {'lost-push':'lost first child branch update response',
                                  'lost-retarget':'lost child retarget response'}
                if frontier_change in lost_responses:
                    self.assertEqual((result['status'],result['landed'],result['pr'],result['ceiling']),
                                     ('awaiting-confirmation',[1],2,3),result)
                    self.assertEqual(result['reason'],lost_responses[frontier_change])
                else:
                    assert_still_waiting(result)
                first_write = 'retarget' if initial_retarget else 'update'
                initial_writes = [('land',1),(first_write,2)]
                initial_replays = [] if initial_retarget else [2]
                self.assertEqual(self.github.writes,initial_writes)
                self.assertEqual([replay['pr'] for replay in replay_attempts],initial_replays)
                if initial_retarget:
                    child = frozen['members'][1]
                    self.assertEqual(self.github.branch(REPO,'second'),child['head'])
                    self.assertEqual(self.github.pull(REPO,2)['baseRefName'],'main')
                    step = cli.read(Path(result['journal']))['steps']['2']
                    expected_phase = 'retarget-intent' if frontier_change == 'lost-retarget' else 'retargeted'
                    self.assertEqual(step['phase'],expected_phase)
                    verified_base = child['base']
                    self.github.squash_next = True
                else:
                    first_replay = replayed[2]
                    first_receipt = Path(first_replay['receipt'])
                    first_receipt_digest = cli.digest(first_receipt)
                    verified_base = first_replay['base']
                continuation = Path(result['continuation'])
                initial_deadline = cli.read(continuation)['deadline']
                checkout,remote = self.github.checkout,self.github.remote
                upstream = self.root/'upstream-change'
                cli.git(checkout,'worktree','add','--detach',str(upstream),verified_base)
                (upstream/'upstream.txt').write_text('Another change landed while checks were pending.\n')
                cli.git(upstream,'add','upstream.txt')
                cli.git(upstream,'commit','-qm','Advance main while child CI is pending')
                advanced = cli.sha(upstream,'HEAD')
                delivery._fast_forward_push(checkout,str(remote),verified_base,advanced)
                self.assertEqual(self.github.branch(REPO,'main'),advanced)
                if frontier_change == 'unknown-head':
                    replacement = main_commit(first_replay['head'],'External change to the published child head')
                    ref = 'refs/heads/second'
                    cli.git(checkout,'push',f"--force-with-lease={ref}:{first_replay['head']}",str(remote),
                            f'{replacement}:{ref}')
                    self.github.rebased_heads[2] = replacement
                    stopped = stack_rebase.continue_shipping(self.plan,**options)
                    self.assertEqual((stopped['status'],stopped['landed']),('blocked',[1]),stopped)
                    self.assertEqual(self.github.writes,[('land',1),('update',2)])
                    self.assertEqual([replay['pr'] for replay in replay_attempts],[2])
                    self.assertEqual(self.github.branch(REPO,'second'),replacement)
                    return

                waiting = stack_rebase.continue_shipping(self.plan,**options)
                assert_still_waiting(waiting)
                self.assertEqual([replay['pr'] for replay in replay_attempts],initial_replays+[2])
                self.assertEqual(self.github.writes,initial_writes+[('update',2)])
                self.assertEqual((replayed[2]['base'],self.github.branch(REPO,'second')),
                                 (advanced,replayed[2]['head']))
                if not initial_retarget:
                    self.assertEqual(cli.digest(first_receipt),first_receipt_digest)
                    history = cli.read(Path(waiting['journal']))['restack_history']['2']
                    self.assertEqual([(item['receipt'],item['receipt_sha256']) for item in history],
                                     [(str(first_receipt),first_receipt_digest)])
                self.assertEqual(cli.read(continuation)['deadline'],initial_deadline)
                if frontier_change == 'rewrite-main':
                    rewritten = main_commit(verified_base,'Rewrite main instead of advancing it')
                    cli.git(checkout,'push',f'--force-with-lease=refs/heads/main:{advanced}',str(remote),
                            f'{rewritten}:refs/heads/main')
                    try:
                        stopped = stack_rebase.continue_shipping(self.plan,**options)
                    except stack_rebase.RebaseError as error:
                        self.assertIn('main',str(error))
                    else:
                        self.assertEqual((stopped['status'],stopped['landed']),('blocked',[1]),stopped)
                        self.assertNotIn('pending-checks',stopped['reason'])
                    self.assertEqual(self.github.writes,[('land',1),('update',2),('update',2)])
                    self.assertEqual([replay['pr'] for replay in replay_attempts],[2,2])
                    self.assertEqual(self.github.branch(REPO,'main'),rewritten)
                    return
                self.github.pending_checks.remove(2)
                result = stack_rebase.continue_shipping(self.plan,**options)
            self.assertEqual(set(replayed),{2,3},result)
            if interrupt_last_push:
                self.assertEqual((result['status'],result['landed'],result['pr'],result['ceiling']),
                                 ('awaiting-confirmation',[1,2],3,3))
                self.assertEqual(result['reason'],'lost third branch update response')
                continuation = Path(result['continuation'])
                initial_deadline = cli.read(continuation)['deadline']
                writes = list(self.github.writes)
                changes = {'seconds':46, 'seed_image':'sha256:'+'b'*64,
                           'tasks_root':self.root/'other-tasks','workspaces_root':self.root/'other-worktrees'}
                for key,value in changes.items():
                    with self.subTest(changed=key):
                        with self.assertRaisesRegex(stack_rebase.RebaseError,'continue with the original'):
                            stack_rebase.continue_shipping(self.plan,**(options|{key:value}))
                saved_run = cli.read(continuation)
                foreign = self.root/'foreign-restack.json'
                foreign_receipt = cli.read(Path(saved_run['pending_update']['receipt']))
                cli.write(foreign,{**foreign_receipt,'shipping_plan':str(self.root/'other-plan.json')})
                cli.write(continuation,{**saved_run,'pending_update':{
                    'receipt':str(foreign),'sha256':cli.digest(foreign)}})
                with self.assertRaisesRegex(stack_rebase.RebaseError,'different plan or frontier'):
                    stack_rebase.continue_shipping(self.plan,**options)
                cli.write(continuation,saved_run)
                self.assertEqual(self.github.writes,writes)
                result = stack_rebase.continue_shipping(self.plan,**options)
                self.assertEqual(cli.read(continuation)['deadline'],initial_deadline)
            self.assertEqual((result['status'],result['landed'],result['ceiling']),('complete',[1,2,3],3),result)
            self.assertEqual(stack_rebase.continue_shipping(self.plan,**options)['landed'],[1,2,3])

        expected_writes = [('land',1),('update',2),('land',2),('retarget',3),('update',3),('land',3)]
        if initial_retarget:
            expected_writes.insert(1,('retarget',2))
        elif frontier_change:
            expected_writes.insert(2,('update',2))
        self.assertEqual(self.github.writes,expected_writes)
        expected_pushes = [(2,self.github.members[1]['head'],replay_attempts[0]['head'])]
        if frontier_change and not initial_retarget:
            expected_pushes.append((2,replay_attempts[0]['head'],replayed[2]['head']))
        expected_pushes.append((3,self.github.members[2]['head'],replayed[3]['head']))
        self.assertEqual(self.github.push_bindings,expected_pushes)
        for number,base,head,task in self.github.merge_bindings[1:]:
            replay = replayed[number]
            self.assertEqual((base,head,task),(replay['base'],replay['head'],Path(replay['task'])))
        self.assertEqual(set(self.github.squash),{2,3} if initial_retarget else {1,2,3})
        self.assertEqual(self.github.branch(REPO,'main'),self.github.squash[3])
        self.assertEqual(self.github.pull(REPO,outside['number'])['state'],'OPEN')
        self.assertEqual(self.github.branch(REPO,'fourth'),outside['head'])
        merge_tree = 'file\nthird.txt\nupstream.txt' if frontier_change else 'file\nthird.txt'
        self.assertEqual(cli.git(self.github.checkout,'ls-tree','--name-only',self.github.squash[3]),merge_tree)
        self.assertEqual(cli.digest(self.plan),plan_digest)

    def test_changed_plan_cannot_resume_a_started_shipping_run(self):
        self.assertEqual(self.execute()['status'],'complete')
        original = json.loads(self.plan.read_text())
        original['frozen_at'] = 'later'
        cli.write(self.plan,original)
        with self.assertRaisesRegex(stack_ship.ShippingError,'changed plan'):
            self.execute()
