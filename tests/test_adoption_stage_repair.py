import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from tests.test_adopt import AdoptionTests
from workbench import adoption_stage_repair as repair, cli, autopilot, workflow
from workbench.task_lock import task_lock

class StageRepairTests(AdoptionTests):
    def prepared(self):
        info=self.create(); task=Path(info['task']); stage=task.parent/'input-stage.json'
        expected=json.loads(stage.read_text()); stage.unlink(); return task,stage,expected
    def test_restores_idempotently_and_binds_settings(self):
        task,stage,expected=self.prepared()
        self.assertEqual(repair.repair(task),'restored')
        self.assertEqual(json.loads(stage.read_text()),expected)
        self.assertEqual(repair.repair(task),'already present')
    def test_issue_snapshot_criteria_and_workspace_tampering_rejected(self):
        task,stage,_=self.prepared(); data=cli.read(task)
        for name in ('issue.txt','input-base.txt','legacy-task.json'):
            path=task.parent/name; original=path.read_bytes(); path.write_bytes(b'bad')
            with self.assertRaises(repair.RepairError): repair.repair(task)
            path.write_bytes(original)
        data['criteria'][0]['text']='weakened'; cli.write(task,data)
        with self.assertRaises(repair.RepairError): repair.repair(task)
        data['criteria'][0]['text']='fix the failure'; cli.write(task,data)
        (Path(data['repo'])/'code').write_text('dirty')
        with self.assertRaises(repair.RepairError): repair.repair(task)
        self.assertFalse(stage.exists())
    def test_symlink_and_conflicting_stage_not_overwritten(self):
        task,stage,_=self.prepared(); foreign=task.parent/'foreign'; foreign.write_text('keep')
        stage.symlink_to(foreign)
        with self.assertRaises(repair.RepairError): repair.repair(task)
        self.assertEqual(foreign.read_text(),'keep'); stage.unlink(); stage.write_text('{}')
        with self.assertRaises(repair.RepairError): repair.repair(task)
        self.assertEqual(stage.read_text(),'{}')
    def test_write_interruption_before_and_after_exclusive_publish(self):
        task,stage,expected=self.prepared(); real_link=os.link
        with patch.object(repair.os,'link',side_effect=OSError('before publish')):
            with self.assertRaises(OSError): repair.repair(task)
        self.assertFalse(stage.exists())
        def after_link(*a,**kw): real_link(*a,**kw); raise OSError('after publish')
        with patch.object(repair.os,'link',side_effect=after_link):
            with self.assertRaises(OSError): repair.repair(task)
        self.assertEqual(json.loads(stage.read_text()),expected)
        self.assertEqual(repair.repair(task),'already present')
    def test_concurrent_process_cannot_repair(self):
        task,stage,_=self.prepared()
        with task_lock(task):
            p=subprocess.run([sys.executable,'-c',
               'import sys; from workbench.adoption_stage_repair import repair; repair(sys.argv[1])',str(task)],capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0); self.assertIn('already being operated',p.stderr)
            self.assertFalse(stage.exists())
        self.assertEqual(repair.repair(task),'restored')
    def test_incomplete_task_fails_closed(self):
        task,stage,_=self.prepared(); data=cli.read(task); del data['adoption']; cli.write(task,data)
        with self.assertRaises(repair.RepairError): repair.repair(task)
        self.assertFalse(stage.exists())

    def test_changed_and_dirty_legacy_checkout_keeps_bound_candidate(self):
        task,stage,_=self.prepared()
        (self.workspace/'code').write_text('new candidate')
        self.git('add','code',repo=self.workspace)
        self.git('commit','-qm','new candidate',repo=self.workspace)
        (self.workspace/'code').write_text('uncommitted work')
        self.assertEqual(repair.repair(task),'restored')
        self.assertTrue(stage.exists())
        self.assertEqual(cli.read(task)['adoption']['candidate_commit'], self.candidate)

    def test_missing_candidate_object_rejected(self):
        task,stage,_=self.prepared(); data=cli.read(task)
        data['adoption']['candidate_commit']='1'*40; cli.write(task,data)
        with self.assertRaisesRegex(repair.RepairError,'candidate object unavailable'):
            repair.repair(task)
        self.assertFalse(stage.exists())

    def test_candidate_object_not_descending_baseline_rejected(self):
        task,stage,_=self.prepared(); data=cli.read(task)
        tree=cli.git(self.workspace,'rev-parse','HEAD^{tree}')
        orphan=subprocess.run(['git','-C',str(self.workspace),'commit-tree',tree,'-m','orphan'],
                              capture_output=True,text=True,check=True).stdout.strip()
        data['adoption']['candidate_commit']=orphan; cli.write(task,data)
        with self.assertRaisesRegex(repair.RepairError,'candidate is not a baseline descendant'):
            repair.repair(task)
        self.assertFalse(stage.exists())

    def test_actual_resume_uses_restored_settings(self):
        from workbench import start
        task,stage,_=self.prepared(); repair.repair(task); seen={}
        def fake_run(task, issue, image, **options):
            seen.update(image=image, issue=issue.read_text(), **options)
            return {'phase':'plan'}
        start.resume_task(task,run=fake_run)
        self.assertEqual(seen['image'],'sha256:'+'a'*64)
        self.assertEqual(seen['issue'],'reported bug and logs')
        self.assertEqual(seen['source_mode'],'image')
        self.assertTrue(seen['build_source_image'])

    def test_real_sigkill_immediately_before_and_after_publication(self):
        task,stage,expected=self.prepared()
        code = '''import os,signal,sys
from workbench import adoption_stage_repair as r
real=os.link
def kill(*args,**kwargs):
    if sys.argv[2]=='after': real(*args,**kwargs)
    os.kill(os.getpid(),signal.SIGKILL)
r.os.link=kill
r.repair(sys.argv[1])
'''
        for when in ('before','after'):
            p=subprocess.run([sys.executable,'-c',code,str(task),when])
            self.assertEqual(p.returncode,-9)
            if when=='before': self.assertFalse(stage.exists())
            else: self.assertEqual(json.loads(stage.read_text()),expected)
        self.assertEqual(repair.repair(task),'already present')

    def test_create_holds_legacy_lock_against_concurrent_repair(self):
        ready=threading.Event(); release=threading.Event(); errors=[]; real=cli.write
        def pause_stage(path,data):
            if Path(path).name=='input-stage.json':
                ready.set()
                if not release.wait(10): raise RuntimeError('test timed out')
            return real(path,data)
        def create():
            try: self.create()
            except BaseException as e: errors.append(e)
        with patch('workbench.adopt.cli.write',side_effect=pause_stage):
            thread=threading.Thread(target=create); thread.start()
            try:
                self.assertTrue(ready.wait(10))
                task=(self.root/'tasks/new/task.json').resolve()
                with self.assertRaisesRegex(OSError,'already being operated'):
                    repair.repair(task)
                self.assertFalse((task.parent/'input-stage.json').exists())
            finally:
                release.set(); thread.join(10)
            self.assertFalse(thread.is_alive()); self.assertEqual(errors,[])
            self.assertTrue((task.parent/'input-stage.json').is_file())

    def test_symlink_in_task_ancestor_is_rejected(self):
        task,stage,_=self.prepared(); alias=task.parent.parent/'alias'
        alias.symlink_to(task.parent, target_is_directory=True)
        with self.assertRaisesRegex(repair.RepairError,'symbolic link in path'):
            repair.repair(alias/'task.json')
        self.assertFalse(stage.exists())

    def test_trusted_root_alias_is_normalized_but_below_root_is_checked(self):
        task,stage,_=self.prepared(); alias=self.root/'trusted-tasks'
        alias.symlink_to(task.parent.parent,target_is_directory=True)
        self.assertEqual(repair.repair(alias/'new/task.json',trusted_tasks_root=alias),'restored')
        self.assertTrue(stage.is_file())

    def test_git_common_dir_normalizes_main_and_linked_checkout(self):
        self.assertEqual(repair._common_dir(self.repo.resolve()),repair._common_dir(self.workspace.resolve()))

    def test_directory_replaced_during_validation_never_writes_replacement(self):
        task,stage,_=self.prepared(); moved=task.parent.parent/'moved'
        expected_fn=repair._expected
        def swap(path,data):
            value=expected_fn(path,data)
            task.parent.rename(moved); task.parent.mkdir()
            return value
        try:
            with patch.object(repair,'_expected',side_effect=swap):
                with self.assertRaisesRegex(repair.RepairError,'task directory changed'):
                    repair.repair(task)
            self.assertFalse(stage.exists())
            self.assertFalse((moved/'input-stage.json').exists())
        finally:
            if moved.exists():
                if task.parent.exists(): task.parent.rmdir()
                moved.rename(task.parent)

    def test_directory_replaced_after_publish_does_not_write_replacement(self):
        task,stage,expected=self.prepared(); moved=task.parent.parent/'moved'; real=os.link
        def swap_after(*args,**kwargs):
            real(*args,**kwargs)
            task.parent.rename(moved); task.parent.mkdir()
        try:
            with patch.object(repair.os,'link',side_effect=swap_after):
                with self.assertRaisesRegex(repair.RepairError,'task directory changed'):
                    repair.repair(task)
            self.assertFalse(stage.exists())
            self.assertEqual(json.loads((moved/'input-stage.json').read_text()),expected)
        finally:
            if moved.exists():
                if task.parent.exists(): task.parent.rmdir()
                moved.rename(task.parent)

    def test_cli_recovers_actual_interrupted_create_then_resume_still_requires_reproduction(self):
        from workbench import start
        real_write=cli.write
        def fail_before_stage(path,data):
            if Path(path).name=='input-stage.json': raise OSError('injected stage failure')
            return real_write(path,data)
        with patch('workbench.adopt.cli.write',side_effect=fail_before_stage):
            with self.assertRaisesRegex(OSError,'injected stage failure'): self.create()
        task=(self.root/'tasks/new/task.json').resolve(); before=cli.read(task)
        self.assertFalse((task.parent/'input-stage.json').exists())
        args=[sys.executable,'-m','workbench.adoption_stage_repair',
              '--task',str(task),'--tasks-root',str(task.parent.parent)]
        result=subprocess.run(args,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'restored')
        self.assertEqual(subprocess.run(args,capture_output=True,text=True).stdout.strip(),'already present')
        after=cli.read(task)
        self.assertEqual(after['criteria'],before['criteria'])
        self.assertEqual(after['base_commit'],before['base_commit'])
        self.assertEqual(after['required_ci_workflows'],before['required_ci_workflows'])
        self.assertFalse((task.parent/'autopilot.json').exists())
        calls=[]
        plan={'status':'ready','reproduction':{'command':['node','/workspace/repro.js'],'marker':'BROKEN','exit_code':0},
              'resolution':{'command':['node','/workspace/repro.js'],'marker':'FIXED','exit_code':0},
              'tests':{'command':['node','/workspace/test.js'],'marker':'PASS','exit_code':0}}
        def fake_model(task,phase,prompt,**kwargs):
            calls.append(phase); return json.dumps(plan)
        def stop_repro(task,image,spec,kind,actor,check,**kwargs):
            calls.append((kind,actor,cli.sha(cli.read(task)['repo'],'HEAD')))
            raise workflow.WorkflowError('injected baseline reproduction failure')
        with patch.object(autopilot,'_model',side_effect=fake_model), patch.object(autopilot,'_check',side_effect=stop_repro):
            with self.assertRaisesRegex(start.StartError,'baseline reproduction failure'):
                start.resume_task(task)
        self.assertEqual(calls[0],'plan')
        self.assertEqual(calls[1],('reproduction',before['implementer'],before['base_commit']))
        self.assertEqual(cli.sha(before['repo'],'HEAD'),before['base_commit'])
        self.assertEqual(json.loads((task.parent/'autopilot.json').read_text())['phase'],'reproduce')
        self.assertEqual(cli.read(task)['status'],'paused')

    def test_cli_rejects_git_partial_creation_without_modifying_workspace(self):
        with patch('workbench.workflow.cli.init',side_effect=OSError('after worktree add')):
            with self.assertRaisesRegex(OSError,'after worktree add'): self.create()
        task=self.root/'tasks/new/task.json'; workspace=self.root/'workspaces/new'
        self.assertFalse(task.exists()); self.assertTrue(workspace.is_dir())
        oldhead=cli.sha(workspace,'HEAD')
        result=subprocess.run([sys.executable,'-m','workbench.adoption_stage_repair',
             '--task',str(task),'--tasks-root',str(self.root/'tasks')],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('unsafe or missing task',result.stderr)
        self.assertTrue(workspace.is_dir()); self.assertEqual(cli.sha(workspace,'HEAD'),oldhead)

    def test_cli_concurrent_legacy_creation_lock_is_respected(self):
        task,stage,_=self.prepared()
        args=[sys.executable,'-m','workbench.adoption_stage_repair',
              '--task',str(task),'--tasks-root',str(task.parent.parent)]
        with task_lock(self.old):
            result=subprocess.run(args,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('already being operated',result.stderr)
            self.assertFalse(stage.exists())
        result=subprocess.run(args,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(stage.exists())

if __name__=='__main__': unittest.main()
