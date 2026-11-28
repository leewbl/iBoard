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

from workbench import (cli, delivery, model_profiles, pr_watch, run_control, stack_ship, stack_verify,
                       start, verify_openclaw, verify_pr, workflow)
from workbench.task_lock import observe_task_lock, task_lock


DEPENDENCY_INPUTS = {'package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'package-lock.json',
                     'yarn.lock', 'bun.lock', 'bun.lockb', '.npmrc', '.pnpmfile.cjs', 'pnpmfile.cjs',
                     'requirements.txt', 'pyproject.toml', 'uv.lock', 'poetry.lock',
                     'Cargo.toml', 'Cargo.lock', 'go.mod', 'go.sum', 'Dockerfile'}


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


def _require_same_dependencies(checkout, base, head, remaining):
    paths = _output(_git(checkout, 'diff', '--name-only', '--no-renames', '-z', base, head, '--',
                         seconds=min(15, remaining()))).split(b'\0')
    if any(Path(os.fsdecode(path)).name in DEPENDENCY_INPUTS or path.startswith(b'patches/') for path in paths):
        raise RebaseError('restack image overlay cannot reuse preinstalled dependencies across this change')


def _next(frozen, journal):
    landed = []
    for member in frozen['members']:
        effective = stack_ship._member(member, journal, frozen['repo'])
        step = journal['steps'].get(str(member['number']), {})
        if step.get('phase') == 'landed':
            if step.get('head') != effective['head']:
                raise RebaseError('shipping journal identifies a different landed head')
            landed.append(effective)
            continue
        known_steps = {('retargeted', member['head'], member['base']),
                       ('write-not-started', effective['head'], effective['base'])}
        if not landed or (step and (step.get('phase'), step.get('head'), step.get('base')) not in known_steps):
            raise RebaseError('restacking requires a confirmed parent and a child with no pending write')
        return landed, member, effective
    raise RebaseError('the frozen shipping plan has already landed')


def _load_plan(path):
    frozen = verify_pr._bounded_json(path, 128_000)
    stack_ship._verify_plan(frozen)
    digest = cli.digest(path)
    journal_path, journal = stack_ship._journal(path, digest)
    return frozen, journal_path, journal


def _retained(repo, member):
    task = Path(member['task']).resolve(strict=True)
    with observe_task_lock(task) as lock:
        if lock == 'held' or stack_verify._proof_locked(repo, member, task) != member['proof']:
            raise RebaseError('the child needs its original, unchanged independent proof')
        data = cli.read(task)
        if 'phase_binding' in data or any(event.get('type') == 'phase-bound' for event in data.get('events', [])):
            raise RebaseError('planned phases require phase_reverify to preserve performance and interaction review')
        unsupported = sorted(set(data) & {'root_cause', 'coverage_swarm', 'visual_baseline', 'visual_parity_result',
            'project_verification_maintenance', 'performance_contract', 'blast_radius', 'skill_authoring',
            'refactor_contract', 'refactor_result', 'feature_context', 'regression_test', 'ci_skip_policy'})
        if unsupported:
            raise RebaseError('restack cannot preserve required evidence or policy: ' + ', '.join(unsupported))
        stage = start._read_input_stage(task)
        state = cli.read(task.with_name('autopilot.json'))
        request = (task.parent / 'issue.txt').read_bytes()
        if cli.digest(task.parent / 'issue.txt') != state['issue_digest']:
            raise RebaseError('retained request changed before restack')
    return task, data, stage, state, request


def _old_tips(member, effective, parent, trunk):
    if effective is not member:
        return {(effective['head'], 'main', trunk)}
    return {(member['head'], 'main', trunk),
            (member['head'], member['base_branch'], member['base']),
            (member['head'], member['base_branch'], parent['head'])}


def _main(reader, repo, landed):
    for parent in landed:
        pull = stack_ship._observe(reader, repo, parent)
        if not stack_ship._landed(pull):
            raise RebaseError('the previous verified PR is not confirmed landed into main')
    return stack_ship._confirmed_main(reader, repo, landed[-1])


def _require_forward_main(reader, repo, old, current):
    if old != current and not reader.contains(repo, old, current):
        raise RebaseError('main no longer descends from the last verified base')


def _observe(reader, repo, landed, member, effective):
    trunk = _main(reader, repo, landed)
    pull = stack_ship._observe(reader, repo, effective)
    tips = (pull['headRefOid'], pull['baseRefName'], pull['baseRefOid'])
    if (pull['state'] != 'OPEN' or pull['isDraft'] or trunk == effective['base']
            or tips not in _old_tips(member, effective, landed[-1], trunk)):
        raise RebaseError('child PR or its parent changed since the frozen shipping run')
    if effective is not member:
        _require_forward_main(reader, repo, effective['base'], trunk)
    return trunk


def _binding(receipt):
    keys = ['shipping_plan_sha256', 'old_head', 'old_base', 'old_task']
    if 'previous_receipt_sha256' in receipt:
        keys.extend(('previous_receipt_sha256', 'lease_head'))
    return {key:receipt[key] for key in keys}


def prepare(plan_path, *, task_id, tasks_root, workspaces_root, seconds, image=None, reader=None,
            seed_image=None, expected_base=None, launch=start.start):
    if (not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', task_id)
            or type(seconds) is not int or not 1 <= seconds <= 86_400):
        raise RebaseError('provide a fresh task ID and a bounded restack time')
    if seed_image is not None and (image or not re.fullmatch(r'sha256:[a-f0-9]{64}', seed_image)):
        raise RebaseError('provide a pinned local seed image or an exact new-main image')
    plan_path = Path(plan_path).expanduser().resolve(strict=True)
    deadline = time.monotonic() + seconds

    def remaining():
        left = int(deadline - time.monotonic())
        if left < 1:
            raise RebaseError('restack time limit reached')
        return left

    reader = reader or GitHub()
    if isinstance(reader, stack_ship.GitHub):
        reader.set_deadline(deadline)
    with task_lock(plan_path, lock_name=f'.{plan_path.name}.shipping.lock'):
        frozen, _, journal = _load_plan(plan_path)
        repo, plan_sha = frozen['repo'], journal['plan_digest']
        landed, member, effective = _next(frozen, journal)
        new_base = _observe(reader, repo, landed, member, effective)
        if expected_base is not None and new_base != expected_base:
            raise RebaseError('main moved before restack preparation')
        old_task, original, stage, state, request = _retained(repo, member)
        run_control.admit(old_task, RebaseError)
        previous = journal.get('restacked', {}).get(str(member['number']))
        if previous:
            run_control.admit(effective['task'], RebaseError)
    settings = stage['run_settings']
    source_mode = settings['source_mode']
    if source_mode == 'image' and not (image or seed_image):
        raise RebaseError('provide a local image for new main or a pinned seed for the frozen root base')
    selected_image = image or settings['image']
    checkout = Path(original['repo']).resolve(strict=True)
    if _commit(checkout, member['head'], remaining) != member['head']:
        raise RebaseError('original child commit is unavailable')
    if _output(_git(checkout,'merge-base',member['base'],member['head'],
                    seconds=remaining())).decode().strip() != member['base']:
        raise RebaseError('the original child does not descend from its frozen parent')
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
    if previous:
        report.update(previous_receipt_sha256=previous['receipt_sha256'], lease_head=effective['head'])
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
    binding = _binding(report)
    local_policy = original.get('local_gate')
    profile = original.get('model_profile')
    try:
        if source_mode == 'image':
            _require_same_dependencies(worktree, new_base, head, remaining)
        if source_mode == 'image' and seed_image:
            seed_base = frozen['members'][0]['base']
            _require_same_dependencies(worktree, seed_base, new_base, remaining)
            context = {**original, 'repo':str(worktree), 'base_commit':new_base}
            if verify_openclaw.applies(context, repo) and (
                    original['surface'] == 'ui' or verify_openclaw.needs_browser(context, head)):
                selected_image = verify_openclaw.build_browser_image(
                    worktree, seed_base, seed_image, new_base, remaining())
            else:
                selected_image = workflow.build_source_image(
                    worktree, seed_base, seed_image, remaining(), revision=new_base)
            report['baseline_image'] = {'seed':seed_image, 'seed_base':seed_base, 'image':selected_image}
            cli.write(receipt_path, report)
        options = {}
        if profile is not None:
            model_profiles.intake(old_task, original, stage)
            options = {'model_profile':old_task.with_name(model_profiles.FILE),
                       'model_profile_sha256':profile['sha256']}
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
                                             'plan':state['plan'],'restacked_from':binding}, **options)
    except (start.StartError, RebaseError, model_profiles.ProfileError, workflow.WorkflowError,
            cli.WorkbenchError) as error:
        report.update(status='verification-blocked',reason=str(error))
    else:
        verified = result.get('verification', {})
        if (result.get('phase') != 'complete' or verified.get('verdict') != 'PASS'
                or (verified.get('head'),verified.get('base'),verified.get('criterion_digest'))
                != (head,new_base,report['criterion_digest'])):
            report.update(status='verification-blocked',reason='fresh independent verification is unavailable')
        else:
            try:
                unchanged = (_observe(reader, repo, landed, member, effective) == new_base
                             and cli.digest(plan_path) == plan_sha)
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
    frozen, shipping_path, shipping = _load_plan(plan_path)
    repo, plan_sha = frozen['repo'], shipping['plan_digest']
    base, head, branch, number = (receipt[k] for k in ('base','head','head_branch','pr'))
    updates = shipping.get('restacked', {})
    previous = updates.get(str(number))
    if previous and (previous['receipt'], previous['receipt_sha256']) == (str(receipt_path), receipt_sha):
        original = next(member for member in frozen['members'] if member['number'] == number)
        stack_ship._member(original, shipping, repo)
        return {'status':'updated','repo':repo,'pr':number,'head':head,'base':base,
                'reason':None,'journal':str(receipt_path.with_suffix('.publish.json'))}
    landed, child, effective = _next(frozen, shipping)
    bound = {'repo':repo,'pr':child['number'],'old_head':child['head'],'old_base':child['base'],
             'shipping_plan_sha256':plan_sha,'head_branch':child['head_branch'],'parent_branch':child['base_branch'],
             'old_task':str(Path(child['task']).resolve(strict=True))}
    lease_head = effective['head']
    if (any(receipt.get(key) != value for key,value in bound.items()) or branch == 'main'
            or receipt.get('previous_receipt_sha256') != (previous['receipt_sha256'] if previous else None)
            or receipt.get('lease_head', child['head']) != lease_head
            or any(not isinstance(value,str) or not pr_watch.SHA.fullmatch(value) for value in (base,head))
            or base in (child['base'], effective['base']) or head in (child['head'],lease_head,base)):
        raise RebaseError('rebased candidate no longer matches the frozen shipping plan')
    original_task, original, original_stage, original_state, _ = _retained(repo, child)
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
        trunk = _main(reader, repo, landed)
        if effective is not child:
            _require_forward_main(reader, repo, effective['base'], trunk)
        remote = pr_watch._facts(reader,repo,number)
        if (remote['state'] != 'OPEN' or remote['isDraft'] or remote['isCrossRepository']
                or remote['headRefName'] != branch):
            raise RebaseError('the selected PR or head branch is no longer available')
        return trunk, (remote['headRefOid'], remote['baseRefName'], remote['baseRefOid'])

    def evidence():
        data = cli.read(task)
        target = data.get('verification_target', {})
        expected = _binding(receipt)
        settings = target.get('settings', {})
        retained_settings = original_stage['run_settings']
        if (target.get('restacked_from') != expected
                or target.get('request_digest') != original_state.get('issue_digest')
                or target.get('plan') != original_state.get('plan')
                or any(settings.get(key) != retained_settings[key] for key in ('source_mode','memory_mb'))
                or ('baseline_image' in receipt and settings.get('image') != receipt['baseline_image']['image'])
                or receipt.get('request_digest') != original_state.get('issue_digest')
                or cli.criteria_digest(data) != cli.criteria_digest(original)
                or any(data.get(key) != original.get(key) for key in
                       ('kind','surface','criterion_changes','model_profile'))):
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
            if (reader.branch_info(repo, 'main').get('protected') is not False
                    or reader.branch_rules(repo, 'main') != []):
                raise RebaseError('local-only update requires unprotected main without branch rules')
        return data, proof

    def mark(action):
        journal['events'].append({'action':action,'at':cli.now()})
        cli.write(journal_path,journal)

    def result(status, reason=None):
        return {'status':status,'repo':repo,'pr':number,'head':head,'base':base,
                'reason':reason,'journal':str(journal_path)}

    def confirm(proof):
        if cli.digest(plan_path) != plan_sha or cli.digest(receipt_path) != receipt_sha:
            raise RebaseError('shipping plan or receipt changed before recording the confirmed update')
        if previous:
            shipping.setdefault('restack_history', {}).setdefault(str(number), []).append(previous)
        shipping.setdefault('restacked', {})[str(number)] = {
            'receipt':str(receipt_path), 'receipt_sha256':receipt_sha, 'proof':proof,
        }
        stack_ship._member(child, shipping, repo)
        shipping['steps'].pop(str(number), None)
        cli.write(shipping_path, shipping)
        if 'confirmed' not in [item['action'] for item in journal['events']]:
            mark('confirmed')
        return result('updated')

    def admitted():
        run_control.admit(original_task, RebaseError)
        if effective is not child:
            run_control.admit(effective['task'], RebaseError)
        run_control.admit(task, RebaseError)

    def uploaded(trunk, tips):
        if tips != (head, 'main', trunk):
            return False
        _require_forward_main(reader, repo, base, trunk)
        return True

    with task_lock(task):
        prior = [item['action'] for item in journal['events']]
        trunk, current = observed()
        if 'push-intent' in prior:
            if uploaded(trunk, current):
                _, proof = evidence()
                return confirm(proof)
            if current[0] not in (head, lease_head):
                raise RebaseError('the remote child head changed outside this rebased update')
            return result('awaiting-confirmation','rebased-push-outcome-unconfirmed')
        if current not in _old_tips(child, effective, landed[-1], trunk):
            raise RebaseError('the remote child changed outside this rebased update')
        if trunk != base:
            _require_forward_main(reader, repo, base, trunk)
            if 'retarget-intent' in prior and current[1] != 'main':
                return result('awaiting-confirmation','retarget-outcome-unconfirmed')
            return result('stale-base','main moved after fresh verification')
        if current[1] != 'main':
            if 'retarget-intent' in prior:
                return result('awaiting-confirmation','retarget-outcome-unconfirmed')
            evidence()
            if (observed() != (base, current) or time.monotonic() >= deadline
                    or cli.digest(plan_path) != plan_sha or cli.digest(receipt_path) != receipt_sha):
                raise RebaseError('publication inputs changed before retarget')
            admitted()
            mark('retarget-intent')
            try:
                reader.retarget(repo,number)
            except (delivery.DeliveryError, OSError) as error:
                return result('awaiting-confirmation',str(error))
        data, proof = evidence()
        if (observed() != (base,(lease_head,'main',base)) or time.monotonic() >= deadline
                or cli.digest(plan_path) != plan_sha or cli.digest(receipt_path) != receipt_sha):
            raise RebaseError('rebased upload requires the confirmed current main base and old head')
        admitted()
        mark('push-intent')
        try:
            reader.push_rebased(data['repo'],repo,branch,lease_head,head)
        except (RebaseError, delivery.DeliveryError, OSError) as error:
            return result('awaiting-confirmation',str(error))
        if not uploaded(*observed()):
            return result('awaiting-confirmation','rebased-push-outcome-unconfirmed')
        return confirm(proof)


def continue_shipping(plan_path, *, tasks_root, workspaces_root, seconds, seed_image=None, reader=None,
                      watch=False, poll_seconds=30, clock=time.time, sleep=time.sleep,
                      ship=stack_ship.execute, prepare_rebase=prepare, publish=publish_rebased):
    """Advance a frozen stack through fresh base verification and exact-head publication."""
    if type(seconds) is not int or not 1 <= seconds <= 86_400:
        raise RebaseError('provide a bounded time for continuous shipping')
    if type(watch) is not bool or type(poll_seconds) is not int or not 1 <= poll_seconds <= 1800:
        raise RebaseError('provide a watch interval from 1 to 1800 seconds')
    if seed_image is not None and not re.fullmatch(r'sha256:[a-f0-9]{64}', seed_image):
        raise RebaseError('the local seed image must be pinned by image ID')
    path = Path(plan_path).expanduser()
    if path.is_symlink():
        raise RebaseError('shipping plan must be a regular local file')
    path = path.resolve(strict=True)
    tasks_root = Path(tasks_root).expanduser().resolve()
    workspaces_root = Path(workspaces_root).expanduser().resolve()
    run_path = path.with_suffix(path.suffix + '.continue.json')
    reader = reader or GitHub()
    with task_lock(path, lock_name=f'.{path.name}.continue.lock', reentrant=False):
        if run_path.is_symlink():
            raise RebaseError('continuous shipping record must be a regular local file')
        plan_sha = cli.digest(path)
        config = {'plan_sha256':plan_sha, 'tasks_root':str(tasks_root),
                  'workspaces_root':str(workspaces_root), 'seconds':seconds, 'seed_image':seed_image}
        run = (cli.read(run_path) if run_path.exists() else
               {'schema':cli.SCHEMA, 'config':config, 'deadline':clock() + seconds})
        if run.get('config') != config:
            raise RebaseError('continue with the original plan, roots, image and time limit')
        if not run_path.exists():
            cli.write(run_path, run)

        def remaining():
            return max(0, int(run['deadline'] - clock()))

        def finish(result):
            run['result'] = result
            cli.write(run_path, run)
            return {**result, 'continuation':str(run_path)}

        refresh_errors = 0

        def observe(result):
            nonlocal refresh_errors
            condition = result.get('wait_for')
            if not watch or not condition:
                return finish(result)
            refresh_errors = refresh_errors + 1 if condition == 'github-status-refresh' else 0
            if refresh_errors >= 3:
                stopped = {key:value for key,value in result.items() if key != 'wait_for'}
                return finish({**stopped, 'status':'blocked', 'reason':'status-query-retries-exhausted',
                               'last_reason':result['reason']})
            finish(result)
            delay = min(poll_seconds, run['deadline'] - clock())
            if delay > 0:
                sleep(delay)
            return None

        def transient_snapshot(error):
            if not watch or not pr_watch.TRANSIENT.search(str(error)):
                raise error
            return observe({**run.get('result', {}), 'status':'blocked', 'reason':'github-temporarily-unavailable',
                            'wait_for':'github-status-refresh'})

        while (left := remaining()) > 0:
            if cli.digest(path) != plan_sha:
                raise RebaseError('shipping plan changed during continuation')
            pending = run.get('pending_update')
            if pending is not None:
                receipt = Path(pending['receipt'])
                if receipt.is_symlink() or cli.digest(receipt) != pending['sha256']:
                    raise RebaseError('pending restack receipt changed before reconciliation')
                evidence = verify_pr._bounded_json(receipt, 64_000)
                frontier = run['result']
                binding = {'shipping_plan':str(path), 'shipping_plan_sha256':plan_sha,
                           'pr':frontier['pr'], 'base':frontier['current_main']}
                if any(evidence.get(key) != value for key, value in binding.items()):
                    raise RebaseError('pending restack checkpoint belongs to a different plan or frontier')
                try:
                    updated = publish(receipt, seconds=left, reader=reader)
                except delivery.DeliveryError as error:
                    if (stopped := transient_snapshot(error)) is not None:
                        return stopped
                    continue
                if updated.get('status') not in ('updated', 'stale-base'):
                    status = {**run['result'], 'status':updated['status'], 'reason':updated['reason'],
                              'restack':str(receipt)}
                    if updated['status'] == 'awaiting-confirmation':
                        status['wait_for'] = 'remote-confirmation'
                    if (stopped := observe(status)) is not None:
                        return stopped
                    continue
                refresh_errors = 0
                del run['pending_update']
                cli.write(run_path, run)
                continue
            try:
                result = ship(path, timeout=left, github=reader)
            except delivery.DeliveryError as error:
                if (stopped := transient_snapshot(error)) is not None:
                    return stopped
                continue
            if (result.get('reason') not in ('fresh-base-verification-required', 'published-restack-base-moved')
                    or not result.get('landed')):
                if (stopped := observe(result)) is not None:
                    return stopped
                continue
            refresh_errors = 0
            if result != run.get('result'):
                finish(result)
            base, pr = result['current_main'], result['pr']
            ident = f'stack-{plan_sha[:12]}-{pr}-{base[:16]}'
            records = tasks_root / (ident + '-restack')
            receipt = records / 'restack.json'
            if (left := remaining()) == 0:
                break
            if not records.exists():
                prepare_rebase(path, task_id=ident, tasks_root=tasks_root, workspaces_root=workspaces_root,
                               seconds=left, seed_image=seed_image, expected_base=base, reader=reader)
            if not receipt.is_file() or receipt.is_symlink():
                return finish({**result, 'reason':'restack-checkpoint-incomplete', 'records':str(records)})
            evidence = verify_pr._bounded_json(receipt, 64_000)
            if ((evidence.get('shipping_plan_sha256'), evidence.get('pr'), evidence.get('base'))
                    != (plan_sha, pr, base)):
                raise RebaseError('retained restack checkpoint belongs to a different candidate or base')
            if evidence.get('status') != 'verified':
                return finish({**result, 'reason':evidence.get('reason', evidence.get('status')),
                               'restack':str(receipt)})
            run['pending_update'] = {'receipt':str(receipt), 'sha256':cli.digest(receipt)}
            cli.write(run_path, run)
        last = run.get('result', {})
        if last.get('status') == 'complete':
            return {**last, 'continuation':str(run_path)}
        stopped = {key:value for key,value in last.items() if key != 'wait_for'}
        if 'wait_for' in last:
            stopped['last_reason'] = last['reason']
        return finish({**stopped, 'status':'blocked', 'reason':'shipping-time-limit'})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    prepare_cli = sub.add_parser('verify', help='Rebase locally and independently verify the result')
    for name in ('plan','id','tasks-root','workspaces-root'):
        prepare_cli.add_argument('--'+name,required=True)
    prepare_cli.add_argument('--seconds',type=int,required=True)
    prepare_cli.add_argument('--image')
    prepare_cli.add_argument('--seed-image')
    publish_cli = sub.add_parser('update', help='Publish a verified rebased candidate to its existing PR')
    publish_cli.add_argument('--receipt',required=True)
    publish_cli.add_argument('--seconds',type=int,default=180)
    advance = sub.add_parser('continue', help='Advance the same frozen plan through each required replay')
    for name in ('plan','tasks-root','workspaces-root'):
        advance.add_argument('--'+name,required=True)
    advance.add_argument('--seconds',type=int,required=True)
    advance.add_argument('--seed-image')
    advance.add_argument('--watch', action='store_true', help='Poll known pending states under the original deadline')
    advance.add_argument('--poll-seconds', type=int, default=30)
    args=parser.parse_args(argv)
    try:
        with redirect_stdout(sys.stderr):
            if args.command == 'verify':
                report = prepare(args.plan,task_id=args.id,tasks_root=args.tasks_root,workspaces_root=args.workspaces_root,
                                 seconds=args.seconds,image=args.image,seed_image=args.seed_image)
            elif args.command == 'update':
                report = publish_rebased(args.receipt,seconds=args.seconds)
            else:
                report = continue_shipping(args.plan,tasks_root=args.tasks_root,workspaces_root=args.workspaces_root,
                                           seconds=args.seconds,seed_image=args.seed_image,
                                           watch=args.watch,poll_seconds=args.poll_seconds)
    except (OSError,ValueError,RebaseError,verify_pr.VerifyError,pr_watch.WatchError,start.StartError,
            stack_ship.ShippingError,delivery.DeliveryError,cli.WorkbenchError) as error:
        parser.error(str(error))
    print(json.dumps(report,indent=2,ensure_ascii=False))
    return 0 if report['status'] in ('verified','updated','complete') else 1


if __name__ == '__main__':
    sys.exit(main())
