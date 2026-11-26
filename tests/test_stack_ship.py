import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workbench import cli, delivery, stack_ship


REPO = 'alice/project'


class ForkFixture:
    def __init__(self, checkout, remote, members):
        self.checkout, self.remote, self.members = checkout, remote, members
        self.writes = []
        self.opened_after_push = set()
        self.landed = set()
        self.retargeted = set()
        self.extra_merge_commit = False

    def login(self):
        return 'alice'

    def repository(self, _):
        return {'fork':True, 'default_branch':'main', 'owner':{'login':'alice'}}

    def branch(self, _, branch):
        return cli.sha(self.remote, f'refs/heads/{branch}')

    def contains(self, _, head, trunk):
        return subprocess.run(['git','-C',str(self.checkout),'merge-base','--is-ancestor',head,trunk],
                              check=False).returncode == 0

    def pull(self, repo, number):
        item = self.members[number-1]
        main = number == 1 or number in self.retargeted
        return {'number':number, 'url':f'https://github.com/{repo}/pull/{number}',
                'state':'MERGED' if number in self.landed else 'OPEN', 'isDraft':False, 'isCrossRepository':False,
                'headRefOid':item['head'], 'headRefName':item['head_branch'],
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

    def merge(self, task, repo, number, github, *, stack_binding, revalidate, deadline):
        revalidate()
        self.writes.append(('land',number))
        head = stack_binding['head']
        if self.extra_merge_commit:
            tree = cli.git(self.checkout,'rev-parse',f'{head}^{{tree}}')
            head = cli.git(self.checkout,'commit-tree',tree,'-p',stack_binding['base'],'-p',head,'-m','Merge on GitHub')
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

    def test_changed_plan_cannot_resume_a_started_shipping_run(self):
        self.assertEqual(self.execute()['status'],'complete')
        original = json.loads(self.plan.read_text())
        original['frozen_at'] = 'later'
        cli.write(self.plan,original)
        with self.assertRaisesRegex(stack_ship.ShippingError,'changed plan'):
            self.execute()
