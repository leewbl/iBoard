"""Replay a shipping stack's next verified change on the newly landed main."""

import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from workbench import cli, delivery, pr_watch, stack_ship, stack_verify, start, verify_pr
from workbench.task_lock import observe_task_lock, task_lock


class RebaseError(Exception):
    pass


class GitHub(stack_ship.GitHub):
    def push_rebased(self, checkout, repo, branch, previous, head):
        ref = 'refs/heads/' + branch
        _output(_git(checkout,'check-ref-format',ref,seconds=self._bounded(10)))
        pushed = _git(checkout,'push','--porcelain',f'--force-with-lease={ref}:{previous}',
                      f'https://github.com/{repo}.git',f'{head}:{ref}',seconds=self._bounded(120))
        if pushed.returncode:
            raise RebaseError('rebased branch update failed or outcome is unknown; inspect GitHub')


def _git(checkout, *args, seconds, stdin=None):
    try:
        return subprocess.run(['git','--no-replace-objects','-C',str(checkout),
                               '-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',
                               '-c','rebase.updateRefs=false','-c','rerere.enabled=false',
                               *args], input=stdin, capture_output=True, check=False, timeout=seconds,
                              env={**os.environ,'GIT_EDITOR':'true','GIT_SEQUENCE_EDITOR':'true',
                                   'GIT_TERMINAL_PROMPT':'0'})
    except subprocess.TimeoutExpired as error:
        raise RebaseError('Git operation exceeded the restack time limit') from error


def _output(result):
    if result.returncode:
        raise RebaseError(result.stderr.decode('utf-8', 'replace').strip()[-1600:] or 'Git operation failed')
    return result.stdout


def _commit(checkout, ref, remaining):
    return _output(_git(checkout,'rev-parse','--verify',ref + '^{commit}',seconds=remaining())).decode().strip()


def _patch_id(checkout, base, head, remaining):
    raw = _output(_git(checkout,'diff','--no-ext-diff','--binary',base,head,'--',seconds=remaining()))
    if not raw:
        return None
    result = _output(_git(checkout,'patch-id','--stable',seconds=remaining(),stdin=raw)).split()
    return result[0].decode('ascii') if result else None


def _next(plan_path):
    frozen = verify_pr._bounded_json(plan_path, 128_000)
    stack_ship._verify_plan(frozen)
    digest = cli.digest(plan_path)
    journal_path = plan_path.with_suffix(plan_path.suffix + '.journal.json')
    if journal_path.is_symlink():
        raise RebaseError('shipping journal must be a regular local file')
    journal = cli.read(journal_path)
    if journal.get('plan_digest') != digest:
        raise RebaseError('shipping journal does not match its frozen plan')
    landed = []
    for member in frozen['members']:
        step = journal['steps'].get(str(member['number']), {})
        if step.get('phase') == 'landed':
            if step.get('head') != member['head']:
                raise RebaseError('shipping journal identifies a different landed head')
            landed.append(member)
            continue
        if not landed or step:
            raise RebaseError('restacking requires a confirmed parent and a child with no pending write')
        return frozen['repo'], landed, member, digest
    raise RebaseError('the frozen shipping plan has already landed')


def _retained(repo, member):
    task = Path(member['task']).resolve(strict=True)
    with observe_task_lock(task) as lock:
        if lock == 'held' or stack_verify._proof_locked(repo, member, task) != member['proof']:
            raise RebaseError('the child needs its original, unchanged independent proof')
        data = cli.read(task)
        stage = start._read_input_stage(task)
        state = cli.read(task.with_name('autopilot.json'))
        request = (task.parent / 'issue.txt').read_bytes()
        if cli.digest(task.parent / 'issue.txt') != state['issue_digest']:
            raise RebaseError('retained request changed before restack')
    return task, data, stage, state, request


def _observe(reader, repo, landed, member):
    for parent in landed:
        pull = stack_ship._observe(reader, repo, parent)
        if not stack_ship._landed(pull):
            raise RebaseError('the previous verified PR is not confirmed landed into main')
    trunk = stack_ship._confirmed_main(reader, repo, landed[-1])
    pull = stack_ship._observe(reader, repo, member)
    if (pull['state'] != 'OPEN' or pull['baseRefName'] != member['base_branch']
            or pull['baseRefOid'] != member['base'] or trunk == member['base']):
        raise RebaseError('child PR or its parent changed since the frozen shipping run')
    return trunk


def prepare(plan_path, *, task_id, tasks_root, workspaces_root, seconds, image=None, reader=None,
            launch=start.start):
    if (not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', task_id)
            or type(seconds) is not int or not 1 <= seconds <= 86_400):
        raise RebaseError('provide a fresh task ID and a bounded restack time')
    plan_path = Path(plan_path).expanduser().resolve(strict=True)
    deadline = time.monotonic() + seconds

    def remaining():
        left = int(deadline - time.monotonic())
        if left < 1:
            raise RebaseError('restack time limit reached')
        return left

    reader = reader or stack_ship.GitHub()
    if isinstance(reader, stack_ship.GitHub):
        reader.set_deadline(deadline)
    with task_lock(plan_path, lock_name=f'.{plan_path.name}.shipping.lock'):
        repo, landed, member, plan_sha = _next(plan_path)
        new_base = _observe(reader, repo, landed, member)
        old_task, original, stage, state, request = _retained(repo, member)
    settings = stage['run_settings']
    source_mode = settings['source_mode']
    if source_mode == 'image' and not image:
        raise RebaseError('provide a local image whose source matches the new main commit')
    selected_image = image or settings['image']
    checkout = Path(original['repo']).resolve(strict=True)
    if _commit(checkout, member['head'], remaining) != member['head']:
        raise RebaseError('original child commit is unavailable')
    known_base = _git(checkout,'cat-file','-e',new_base + '^{commit}',seconds=remaining())
    if known_base.returncode:
        _output(_git(checkout,'fetch','--no-tags','--no-write-fetch-head',f'https://github.com/{repo}.git',
                     'refs/heads/main',seconds=min(60, remaining())))
    if _commit(checkout,new_base,remaining) != new_base:
        raise RebaseError('observed main commit is unavailable locally')

    records = Path(tasks_root).expanduser().resolve() / (task_id + '-restack')
    records.mkdir(mode=0o700, parents=True, exist_ok=False)
    worktree = Path(workspaces_root).expanduser().resolve() / (task_id + '-patch')
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        raise RebaseError('restack worktree already exists')
    request_file = records/'request.txt'
    request_file.write_bytes(request)
    request_file.chmod(0o600)
    _output(_git(checkout,'worktree','add','--detach',str(worktree),member['head'],seconds=remaining()))
    report = {'schema':cli.SCHEMA,'repo':repo,'pr':member['number'],'head_branch':member['head_branch'],
              'parent_branch':member['base_branch'],'old_head':member['head'],'old_base':member['base'],
              'base':new_base,'worktree':str(worktree),'shipping_plan':str(plan_path),
              'shipping_plan_sha256':plan_sha,'old_task':str(old_task),'old_review':member['proof'],
              'request_digest':state['issue_digest'],'criterion_digest':cli.criteria_digest(original)}
    receipt_path = records/'restack.json'
    replay = _git(worktree,'rebase','--no-autostash','--rebase-merges','--reapply-cherry-picks','--empty=stop',
                  '--onto',new_base,member['base'],seconds=remaining())
    (records/'rebase.log').write_bytes(replay.stdout + replay.stderr)
    if replay.returncode:
        report.update(status='rebase-blocked')
        cli.write(receipt_path,report)
        return {**report,'receipt':str(receipt_path)}
    head = _commit(worktree,'HEAD',remaining)
    after = _patch_id(worktree,new_base,head,remaining)
    if not after or _output(_git(worktree,'merge-base',new_base,head,seconds=remaining())).decode().strip() != new_base:
        report.update(status='no-verifiable-rebased-change')
        cli.write(receipt_path,report)
        return {**report,'receipt':str(receipt_path)}
    before = _patch_id(worktree,member['base'],member['head'],remaining)
    report.update(status='verifying',head=head,old_patch_id=before,new_patch_id=after,patch_id_matches=before==after,
                  task=str(Path(tasks_root).expanduser().resolve()/task_id/'task.json'))
    cli.write(receipt_path,report)
    binding = {key:report[key] for key in ('shipping_plan_sha256','old_head','old_base','old_task')}
    local_policy = original.get('local_gate')
    try:
        result = launch(task_id=task_id, repo=worktree, issue_file=request_file,
                        source=f'https://github.com/{repo}/pull/{member["number"]}',
                        kind=original['kind'], surface=original['surface'], iterations=1, seconds=remaining(),
                        image=selected_image, pr_repo=repo, tasks_root=tasks_root, workspaces_root=workspaces_root,
                        source_mode=source_mode, build_source_image=source_mode=='image', memory_mb=settings['memory_mb'],
                        required_ci_workflows=original.get('required_ci_workflows') or member.get('ci_workflows'),
                        criteria=[{key:item[key] for key in ('id','text')} for item in original['criteria']],
                        retained_contract={'criteria':original['criteria'],'changes':original['criterion_changes']},
                        local_verification=local_policy, implementer='pr-contributor',
                        verification_target={'repo':repo,'pr':member['number'],'head':head,'base':new_base,
                                             'plan':state['plan'],'restacked_from':binding})
    except (start.StartError, RebaseError) as error:
        report.update(status='verification-blocked',reason=str(error))
    else:
        verified = result.get('verification', {})
        if (result.get('phase') != 'complete' or verified.get('verdict') != 'PASS'
                or (verified.get('head'),verified.get('base'),verified.get('criterion_digest'))
                != (head,new_base,report['criterion_digest'])):
            report.update(status='verification-blocked',reason='fresh independent verification is unavailable')
        else:
            try:
                unchanged = _observe(reader,repo,landed,member) == new_base and cli.digest(plan_path) == plan_sha
            except (RebaseError, stack_ship.ShippingError, pr_watch.WatchError, delivery.DeliveryError, OSError):
                unchanged = False
            report.update(status='verified' if unchanged else 'stale',verification=verified)
    cli.write(receipt_path,report)
    return {**report,'receipt':str(receipt_path)}


def publish_rebased(receipt_path, *, seconds=180, reader=None):
    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise RebaseError('provide a bounded publication time')
    receipt_path = Path(receipt_path).expanduser().resolve(strict=True)
    reader = reader or GitHub()
    deadline = time.monotonic() + seconds
    if isinstance(reader, stack_ship.GitHub):
        reader.set_deadline(deadline)
    with task_lock(receipt_path, lock_name=f'.{receipt_path.name}.publish.lock'):
        receipt = cli.read(receipt_path)
        if receipt.get('status') != 'verified':
            raise RebaseError('a verified rebased candidate receipt is required')
        receipt_sha = cli.digest(receipt_path)
        plan_path = Path(receipt['shipping_plan']).resolve(strict=True)
        with task_lock(plan_path, lock_name=f'.{plan_path.name}.shipping.lock'):
            return _publish_locked(receipt_path, receipt, receipt_sha, plan_path, reader, deadline)


def _publish_locked(receipt_path, receipt, receipt_sha, plan_path, reader, deadline):
    repo, landed, child, plan_sha = _next(plan_path)
    bound = {'repo':repo,'pr':child['number'],'old_head':child['head'],'old_base':child['base'],
             'shipping_plan_sha256':plan_sha,'head_branch':child['head_branch'],'parent_branch':child['base_branch'],
             'old_task':str(Path(child['task']).resolve(strict=True))}
    base, head, branch, number = (receipt[k] for k in ('base','head','head_branch','pr'))
    if (any(receipt.get(key) != value for key,value in bound.items()) or branch == 'main'
            or any(not isinstance(value,str) or not pr_watch.SHA.fullmatch(value) for value in (base,head))
            or base == child['base'] or head in (child['head'],base)):
        raise RebaseError('rebased candidate no longer matches the frozen shipping plan')
    _, original, _, original_state, _ = _retained(repo, child)
    stack_ship._scope(reader, repo)
    task = Path(receipt['task']).resolve(strict=True)
    journal_path = receipt_path.with_suffix('.publish.json')
    if journal_path.is_symlink():
        raise RebaseError('publication journal must be a regular local file')
    journal = (cli.read(journal_path) if journal_path.exists()
               else {'schema':cli.SCHEMA,'receipt_sha256':receipt_sha,'events':[]})
    if journal.get('receipt_sha256') != receipt_sha:
        raise RebaseError('publication receipt changed after a write was requested')

    def observed():
        parent = landed[-1]
        if (not stack_ship._landed(stack_ship._observe(reader,repo,parent))
                or stack_ship._confirmed_main(reader,repo,parent) != base):
            raise RebaseError('main moved after fresh verification')
        remote = pr_watch._facts(reader,repo,number)
        if (remote['state'] != 'OPEN' or remote['isCrossRepository'] or remote['headRefName'] != branch):
            raise RebaseError('the selected PR or head branch is no longer available')
        return remote['headRefOid'], remote['baseRefName'], remote['baseRefOid']

    def evidence():
        data = cli.read(task)
        target = data.get('verification_target', {})
        expected = {key:receipt[key] for key in ('shipping_plan_sha256','old_head','old_base','old_task')}
        if (target.get('restacked_from') != expected
                or target.get('request_digest') != original_state.get('issue_digest')
                or receipt.get('request_digest') != original_state.get('issue_digest')
                or cli.criteria_digest(data) != cli.criteria_digest(original)
                or any(data.get(key) != original.get(key) for key in ('kind','surface','criterion_changes'))):
            raise RebaseError('fresh task does not preserve the original acceptance contract')
        member = {**child,'head':head,'base':base}
        proof = stack_verify._proof_locked(repo,member,task)
        verified = receipt['verification']
        if (proof['status'] != 'verified' or proof['review_sha256'] != verified.get('review_sha256')
                or proof['review'] != verified.get('review')):
            raise RebaseError('fresh independent verification cannot be confirmed')
        _, problems = cli.gate(data,head)
        if problems:
            raise RebaseError('task cannot authorize an update: ' + '; '.join(problems))
        workflows = original.get('required_ci_workflows') or child.get('ci_workflows')
        if data.get('required_ci_workflows') != workflows:
            raise RebaseError('rebased task lost the original CI workflow selection')
        delivery._merge_workflows(data,child.get('ci_workflows'))
        policy = original.get('local_gate')
        if bool(data.get('local_gate')) != bool(policy):
            raise RebaseError('rebased task changed the local gate policy')
        if policy:
            current = data.get('local_gate', {})
            if any(current.get(key) != policy.get(key) for key in ('mode','files','reason')):
                raise RebaseError('rebased task lost the original local gate')
        if data.get('local_gate'):
            delivery.require_local_write(task,data,head,reader,repo)
        return data

    def mark(action):
        journal['events'].append({'action':action,'at':cli.now()})
        cli.write(journal_path,journal)

    def result(status, reason=None):
        return {'status':status,'repo':repo,'pr':number,'head':head,'base':base,
                'reason':reason,'journal':str(journal_path)}

    with task_lock(task):
        prior = [item['action'] for item in journal['events']]
        current = observed()
        if current == (head,'main',base) and 'push-intent' in prior:
            if 'confirmed' not in prior:
                mark('confirmed')
            return result('updated')
        if 'push-intent' in prior:
            return result('awaiting-confirmation','rebased-push-outcome-unconfirmed')
        if current == (child['head'],child['base_branch'],child['base']):
            if 'retarget-intent' in prior:
                return result('awaiting-confirmation','retarget-outcome-unconfirmed')
            evidence()
            if (observed() != current or time.monotonic() >= deadline
                    or cli.digest(receipt_path) != receipt_sha):
                raise RebaseError('publication inputs changed before retarget')
            mark('retarget-intent')
            try:
                reader.retarget(repo,number)
            except (delivery.DeliveryError, OSError) as error:
                return result('awaiting-confirmation',str(error))
        elif current != (child['head'],'main',base) or 'retarget-intent' not in prior:
            raise RebaseError('the remote child changed outside this rebased update')
        data = evidence()
        if (observed() != (child['head'],'main',base) or time.monotonic() >= deadline
                or cli.digest(receipt_path) != receipt_sha):
            raise RebaseError('rebased upload requires the confirmed current main base and old head')
        mark('push-intent')
        try:
            reader.push_rebased(data['repo'],repo,branch,child['head'],head)
        except (RebaseError, delivery.DeliveryError, OSError) as error:
            return result('awaiting-confirmation',str(error))
        if observed() != (head,'main',base):
            return result('awaiting-confirmation','rebased-push-outcome-unconfirmed')
        mark('confirmed')
        return result('updated')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    prepare_cli = sub.add_parser('verify', help='Rebase locally and independently verify the result')
    for name in ('plan','id','tasks-root','workspaces-root'):
        prepare_cli.add_argument('--'+name,required=True)
    prepare_cli.add_argument('--seconds',type=int,required=True)
    prepare_cli.add_argument('--image')
    publish_cli = sub.add_parser('update', help='Publish a verified rebased candidate to its existing PR')
    publish_cli.add_argument('--receipt',required=True)
    publish_cli.add_argument('--seconds',type=int,default=180)
    args=parser.parse_args(argv)
    try:
        with redirect_stdout(sys.stderr):
            if args.command == 'verify':
                report = prepare(args.plan,task_id=args.id,tasks_root=args.tasks_root,workspaces_root=args.workspaces_root,
                                 seconds=args.seconds,image=args.image)
            else:
                report = publish_rebased(args.receipt,seconds=args.seconds)
    except (OSError,ValueError,RebaseError,verify_pr.VerifyError,pr_watch.WatchError,start.StartError,
            stack_ship.ShippingError,delivery.DeliveryError,cli.WorkbenchError) as error:
        parser.error(str(error))
    print(json.dumps(report,indent=2,ensure_ascii=False))
    return 0 if report['status'] in ('verified','updated') else 1


if __name__ == '__main__':
    sys.exit(main())
