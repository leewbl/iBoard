"""Verify a frozen stack and hand it over without writing to GitHub."""

import argparse
from contextlib import redirect_stdout
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

from workbench import cli, delivery, local_gate, pr_watch, stack_verify, start, verify_pr, workflow
from workbench.task_lock import task_lock


PSTACK_SOURCE = 'skills/poteto-mode/playbooks/autopilot-stack.md'


class StackError(Exception):
    pass


def _positive(value, maximum, label):
    if type(value) is not int or not 1 <= value <= maximum:
        raise StackError(f'{label} must be an integer from 1 to {maximum}')
    return value


def _pull(reader, repo, number):
    raw = pr_watch._facts(reader, repo, number)
    if raw['state'] != 'OPEN' or raw['headRefOid'] == raw['baseRefOid']:
        raise StackError(f'PR #{number} is not open with a distinct head')
    return {'number':number, 'head':raw['headRefOid'], 'head_branch':raw['headRefName'],
            'base':raw['baseRefOid'], 'base_branch':raw['baseRefName'],
            'cross_repository':raw['isCrossRepository']}


def _topology(members, trunk):
    parents = {}
    for index, item in enumerate(members):
        if index == 0 and item['base_branch'] != trunk:
            raise StackError('the first PR must target the default branch')
        if index and parents.get(item['base_branch']) != item['base']:
            raise StackError(f"PR #{item['number']} does not use a selected parent's exact head")
        if not item['cross_repository']:
            branch = item['head_branch']
            if branch in parents:
                raise StackError('the same head branch appears twice')
            parents[branch] = item['head']


def _request(path):
    if not path.is_file() or path.is_symlink():
        raise StackError('select a regular request file')
    with path.open('rb') as stream:
        raw = stream.read(100_001)
    if len(raw) > 100_000 or not raw.strip() or b'\0' in raw:
        raise StackError('each request must be bounded nonempty text')
    return raw.decode('utf-8').replace('\r\n','\n').replace('\r','\n')


def _configuration(item, source, inherited):
    if 'task' in item:
        path = item['task']
        if not isinstance(path,str) or not path.strip():
            raise StackError('select a retained task file')
        if any(key not in ('number','task') for key in item):
            raise StackError('a retained task already identifies the inputs and verification policy')
        return {'task':str((source / Path(path).expanduser()).resolve(strict=True))}
    path = item.get('request')
    if not isinstance(path,str) or not path.strip():
        raise StackError('each new task needs a request file')
    request = _request(source / Path(path).expanduser())
    criteria = [{'id':row['id'],'text':row['text'].strip()} for row in verify_pr._criteria(item.get('criteria'))]
    kind, surface = item.get('kind'), item.get('surface')
    if kind not in ('feature','bug') or surface not in ('ui','non-ui'):
        raise StackError('each new task needs a valid kind and surface')
    image = item.get('image', inherited.get('image'))
    if not isinstance(image, str) or not image:
        raise StackError('each new task needs a local image')
    checkout = item.get('checkout',inherited.get('checkout'))
    if not isinstance(checkout,str) or not checkout:
        raise StackError('each new task needs a local checkout')
    mode = item.get('source_mode', inherited.get('source_mode','workspace'))
    if mode not in ('workspace','image'):
        raise StackError('invalid stack source mode')
    plan = item.get('plan')
    if plan is not None and (not isinstance(plan, dict) or plan.get('status') != 'ready'):
        raise StackError('a frozen verification plan must report ready')
    local = item.get('local_gate')
    ci = workflow.validate_required_ci_workflows(item.get('required_ci_workflows'))
    if local and (mode != 'image' or ci or not isinstance(local, dict)
                  or not isinstance(local.get('files'),list) or not local['files']
                  or not isinstance(local.get('reason'),str) or not local['reason'].strip()):
        raise StackError('a local gate needs image mode, test files and a reason')
    if local:
        local = {'files':local['files'],'reason':local['reason'].strip()}
    candidate, build = item.get('candidate_image'), item.get('build_source_image',False)
    if (type(build) is not bool or (build and candidate) or (mode != 'image' and (candidate or build))
            or (candidate is not None and not isinstance(candidate,str))):
        raise StackError('invalid candidate image settings')
    memory = _positive(item.get('memory_mb',inherited.get('memory_mb',2048)),8192,'memory')
    if memory < 128:
        raise StackError('memory must be at least 128 MB')
    return {'request':request, 'criteria':criteria, 'kind':kind, 'surface':surface,'image':image,
            'checkout':str((source / Path(checkout).expanduser()).resolve(strict=True)),
            'source_mode':mode, 'candidate_image':candidate,
            'build_source_image':build, 'plan':plan, 'memory_mb':memory,
            'seconds':_positive(item['seconds'],86_400,'member time') if 'seconds' in item else None,
            'required_ci_workflows':ci, 'local_gate':local}


def freeze(manifest, destination, *, reader=None):
    manifest = Path(manifest).expanduser().resolve(strict=True)
    data = verify_pr._bounded_json(manifest, 512_000)
    if not isinstance(data,dict):
        raise StackError('the stack manifest must be a JSON object')
    repo, specs = data['repo'], data['members']
    if (not isinstance(repo,str) or not pr_watch.REPO.fullmatch(repo) or not isinstance(specs,list)
            or not 1 <= len(specs) <= 15 or any(not isinstance(item,dict) for item in specs)):
        raise StackError('select a repository and at most 15 explicit PRs')
    numbers = [_positive(item.get('number'),2_147_483_647,'PR number') for item in specs]
    if len(set(numbers)) != len(numbers):
        raise StackError('each selected PR must appear once')
    seconds = _positive(data.get('seconds'),86_400,'batch time')
    reader = reader or pr_watch.GitHubReader()
    if isinstance(reader,pr_watch.GitHubReader):
        reader.set_deadline(time.monotonic()+min(seconds,120))
    repository = reader.repository(repo)
    trunk = repository.get('default_branch')
    if not isinstance(trunk,str) or not trunk:
        raise StackError('the default branch is unavailable')
    members = []
    for item, number in zip(specs,numbers):
        pair = _pull(reader,repo,number)
        members.append({**pair,'input':_configuration(item,manifest.parent,data)})
    _topology(members,trunk)
    out = Path(destination).expanduser().resolve()
    out.mkdir(mode=0o700,parents=True,exist_ok=False)
    path = out/'stack.json'
    cli.write(path,{'schema':cli.SCHEMA,'repo':repo,'trunk':trunk,'seconds':seconds,
                   'frozen_at':cli.now(),'members':members})
    return {'status':'frozen','plan':str(path),'prs':numbers}


def _match(reader, repo, member):
    if _pull(reader,repo,member['number']) != {key:value for key,value in member.items() if key!='input'}:
        raise StackError(f"PR #{member['number']} moved after the verification inputs were frozen")


def _task(root, member):
    inputs = member['input']
    return Path(inputs['task']) if 'task' in inputs else root/'tasks'/f"pr-{member['number']}"/'task.json'


def _generated_binding(task, member):
    inputs = member['input']
    if 'task' in inputs:
        return
    data = cli.read(task)
    stage = start._read_input_stage(task)
    settings = stage['run_settings']
    policy = data.get('local_gate')
    if (stage.get('request_digest') != hashlib.sha256(inputs['request'].encode('utf-8')).hexdigest()
            or any(settings.get(key)!=inputs[key] for key in ('image','memory_mb','source_mode',
                                                             'build_source_image','candidate_image'))
            or any(data.get(key)!=inputs[key] for key in ('kind','surface','required_ci_workflows'))
            or [{key:row[key] for key in ('id','text')} for row in data['criteria']] != inputs['criteria']
            or data.get('verification_target',{}).get('plan') != inputs['plan']
            or bool(policy) != bool(inputs['local_gate'])
            or (policy and any(policy.get(key)!=inputs['local_gate'][key] for key in ('files','reason')))):
        raise StackError('task no longer matches the frozen acceptance inputs')


def _verify_member(root, repo, member, reader, remaining, verify, resume):
    task = _task(root,member)
    inputs = member['input']
    if task.exists():
        _generated_binding(task,member)
        proof = stack_verify._proof(repo,member,task)
        if proof['status']=='verified':
            return proof
        if 'task' in inputs or cli.read(task)['status'] != 'paused':
            raise StackError('retained task needs inspection or fresh verification')
        resume(task)
    else:
        if 'task' in inputs:
            raise StackError('selected retained task is unavailable')
        request = root/f"pr-{member['number']}-request.txt"
        if not request.exists():
            with request.open('x',encoding='utf-8') as stream:
                stream.write(inputs['request'])
        if request.is_symlink() or request.read_text(encoding='utf-8') != inputs['request']:
            raise StackError('prepared stack request changed')
        seconds = remaining()
        if inputs['seconds']:
            seconds = min(seconds,inputs['seconds'])
        outcome = verify(task_id=f"pr-{member['number']}",repo=repo,number=member['number'],
                         checkout=inputs['checkout'],request=request,criteria=inputs['criteria'],
                         kind=inputs['kind'],surface=inputs['surface'],image=inputs['image'],
                         tasks_root=root/'tasks',workspaces_root=root/'workspaces',seconds=seconds,
                         memory_mb=inputs['memory_mb'],source_mode=inputs['source_mode'],
                         candidate_image=inputs['candidate_image'],build_source_image=inputs['build_source_image'],
                         required_ci_workflows=inputs['required_ci_workflows'],local_verification=inputs['local_gate'],
                         expected_head=member['head'],expected_base=member['base'],plan=inputs['plan'],reader=reader)
        if outcome.get('status') != 'verified':
            raise StackError('PR state could not be confirmed after verification')
    _generated_binding(task,member)
    proof = stack_verify._proof(repo,member,task)
    if proof['status'] != 'verified':
        raise StackError('current independent verification could not be confirmed')
    return proof


def _gate(reader, repo, member, task):
    number = member['number']
    frontier = pr_watch.snapshot(reader,repo,number,'check',member['head'],member['base'])
    if frontier['status'] != 'github-ready':
        return frontier['reason']
    data = cli.read(task)
    head = member['head']
    if data.get('local_gate'):
        local_gate.validate(data,head)
        delivery.require_actions_disabled(reader,repo)
        return None
    required = workflow.validate_required_ci_workflows(data.get('required_ci_workflows'))
    if not required:
        return 'required-ci-policy-unavailable'
    raw = pr_watch._facts(reader,repo,number)
    if (raw['headRefOid'],raw['baseRefOid'],raw['baseRefName']) != (head,member['base'],member['base_branch']):
        return 'revision-changed-during-ci-audit'
    if not delivery._checks_pass(raw) or not delivery._required_workflows_pass(
            reader,repo,number,raw,data,head,required,base_branch=member['base_branch']):
        return 'required-ci-not-proven'
    return None


def run(path, *, reader=None, verify=verify_pr.verify, resume=start.resume_task):
    path = Path(path).expanduser().resolve(strict=True)
    root = path.parent
    journal = root/'run.json'
    reader = reader or pr_watch.GitHubReader()
    with task_lock(path,lock_name='.stack-autopilot.lock'):
        frozen = cli.read(path)
        digest = cli.digest(path)
        state = cli.read(journal) if journal.exists() else {
            'schema':cli.SCHEMA,'started_at':cli.now(),'plan_sha256':digest,'events':[],
        }
        if state.get('plan_sha256') != digest:
            raise StackError('the frozen stack changed after execution began')
        cli.write(journal,state)
        repo, members = frozen['repo'], frozen['members']
        _topology(members,frozen['trunk'])
        deadline = datetime.fromisoformat(state['started_at']).timestamp() + frozen['seconds']
        completed = []
        gates = []

        def remaining():
            seconds = int(deadline-time.time())
            if seconds < 1:
                raise StackError('overall stack time limit reached')
            if cli.digest(path) != digest:
                raise StackError('frozen stack changed during verification')
            if isinstance(reader,pr_watch.GitHubReader):
                reader.set_deadline(time.monotonic()+seconds)
            return seconds

        def finish(status, **details):
            report_path = root/'handoff.json'
            report = {'schema':cli.SCHEMA,'status':status,'repo':repo,'observed_at':cli.now(),
                      'plan_sha256':digest,'verified':completed,'gates':gates,
                      'journal':str(journal),'report':str(report_path),**details}
            cli.write(report_path,report)
            return report

        def mark(number, action):
            state['events'].append({'number':number,'action':action,'at':cli.now()})
            cli.write(journal,state)

        for member in members:
            number = member['number']
            try:
                remaining()
                _match(reader,repo,member)
                mark(number,'verify')
                proof = _verify_member(root,repo,member,reader,remaining,verify,resume)
                completed.append({'pr':number,'head':member['head'],'base':member['base'],
                                  'task':str(_task(root,member)),'proof':proof})
                mark(number,'verified')
            except (OSError,ValueError,KeyError,RuntimeError,StackError,verify_pr.VerifyError,start.StartError,
                    cli.WorkbenchError,delivery.DeliveryError,pr_watch.WatchError) as error:
                mark(number,'blocked')
                return finish('verification-blocked',frontier=number,reason=str(error))
        for member, verified in zip(members,completed):
            number = member['number']
            try:
                remaining()
                _match(reader,repo,member)
                task = _task(root,member)
                reason = _gate(reader,repo,member,task)
                if reason is None:
                    _match(reader,repo,member)
                    _generated_binding(task,member)
                    if stack_verify._proof(repo,member,task) != verified['proof']:
                        reason = 'independent-proof-changed-during-handoff'
            except (OSError,ValueError,KeyError,RuntimeError,StackError,cli.WorkbenchError,
                    start.StartError,delivery.DeliveryError,pr_watch.WatchError,local_gate.LocalGateError,
                    workflow.WorkflowError) as error:
                reason = str(error)
            if reason:
                gates.append({'pr':number,'reason':reason})
        return finish('verified-awaiting-gates' if gates else 'handoff-ready')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    prepare = sub.add_parser('freeze')
    prepare.add_argument('--manifest',required=True)
    prepare.add_argument('--out',required=True)
    execute = sub.add_parser('run')
    execute.add_argument('--plan',required=True)
    args = parser.parse_args(argv)
    try:
        with redirect_stdout(sys.stderr):
            result = freeze(args.manifest,args.out) if args.command=='freeze' else run(args.plan)
    except (OSError,ValueError,KeyError,StackError,cli.WorkbenchError,delivery.DeliveryError,
            pr_watch.WatchError,verify_pr.VerifyError,workflow.WorkflowError) as error:
        parser.error(str(error))
    print(json.dumps(result,indent=2,ensure_ascii=False))
    return 0 if result['status'] in ('frozen','handoff-ready') else 1


if __name__=='__main__':
    sys.exit(main())
