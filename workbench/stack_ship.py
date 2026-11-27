"""Land a frozen, independently verified PR stack one member at a time."""

import argparse
import json
from pathlib import Path
import sys
import time

from workbench import autopilot, cli, delivery, pr_watch, run_control, stack_verify, start, verify_pr, workflow
from workbench.task_lock import task_lock


PSTACK_SOURCE = 'skills/poteto-mode/playbooks/shipping.md'


class ShippingError(Exception):
    pass


class GitHub(pr_watch.GitHubReader):
    def retarget(self, repo, number):
        self._call('api', '--method', 'PATCH', f'repos/{repo}/pulls/{number}', '-f', 'base=main')

    def contains(self, repo, head, trunk):
        comparison = self._call('api', '--method', 'GET', f'repos/{repo}/compare/{head}...{trunk}')
        return (comparison.get('status') in ('identical', 'ahead')
                and comparison.get('merge_base_commit', {}).get('sha') == head
                and comparison.get('base_commit', {}).get('sha') == head)

    def squash_result(self, repo, number, head, base, trunk):
        pull = self._call('api', '--method', 'GET', f'repos/{repo}/pulls/{number}')
        merged = pull.get('merge_commit_sha')
        if (pull.get('merged') is not True or pull.get('head', {}).get('sha') != head
                or pull.get('base', {}).get('ref') != 'main'
                or not isinstance(merged, str) or not pr_watch.SHA.fullmatch(merged)):
            return False
        candidate = self._call('api', '--method', 'GET', f'repos/{repo}/git/commits/{head}')
        landing = self._call('api', '--method', 'GET', f'repos/{repo}/git/commits/{merged}')
        tree = candidate.get('tree', {}).get('sha')
        parents = landing.get('parents')
        return (candidate.get('sha') == head and landing.get('sha') == merged
                and isinstance(tree, str) and pr_watch.SHA.fullmatch(tree) is not None
                and landing.get('tree', {}).get('sha') == tree
                and isinstance(parents, list) and [item.get('sha') for item in parents] == [base]
                and (merged == trunk or self.contains(repo, merged, trunk)))


def plan(repo, numbers, tasks, *, ci_workflows=None, timeout=90, reader=None):
    observation = stack_verify.audit(repo, numbers, tasks, timeout=timeout, reader=reader)
    if observation.get('default_branch') != 'main':
        raise ShippingError('cannot freeze a shipping plan without the default main branch')
    selected = set(observation['verified_prefix'])
    members, stop = [], observation.get('stop')
    workflows = workflow.validate_required_ci_workflows(ci_workflows)
    for row in observation['members']:
        if row['number'] not in selected:
            continue
        if row['cross_repository'] or (not members and row['base_branch'] != 'main'):
            stop = {'pr':row['number'], 'reason':'select-one-same-repository-path-rooted-at-main'}
            break
        if members and (row['base'], row['base_branch']) != (members[-1]['head'], members[-1]['head_branch']):
            stop = {'pr':row['number'], 'reason':'stack-branches-after-the-selected-path'}
            break
        proof = row['proof']
        members.append({key:row[key] for key in ('number', 'head', 'head_branch', 'base', 'base_branch')} |
                       {'task':str(Path(row['task']).resolve(strict=True)), 'proof':proof,
                        'ci_workflows':workflows})
    return {'schema':cli.SCHEMA, 'repo':repo, 'trunk':'main', 'frozen_at':cli.now(), 'members':members,
            'ceiling':members[-1]['number'] if members else None, 'stop':stop}


def _verify_plan(frozen):
    if (not isinstance(frozen, dict) or frozen.get('schema') != cli.SCHEMA
            or not isinstance(frozen.get('repo'), str) or not pr_watch.REPO.fullmatch(frozen['repo'])
            or frozen.get('trunk') != 'main' or not isinstance(frozen.get('members'), list)
            or not 1 <= len(frozen['members']) <= 15):
        raise ShippingError('shipping requires a nonempty frozen plan')
    seen, previous = set(), None
    for member in frozen['members']:
        if (not isinstance(member, dict) or type(member.get('number')) is not int
                or member['number'] < 1 or member['number'] in seen
                or any(not isinstance(member.get(k), str) or not pr_watch.SHA.fullmatch(member[k])
                       for k in ('head', 'base'))
                or member['head'] == member['base']
                or any(not isinstance(member.get(k), str) or not member[k]
                       for k in ('head_branch', 'base_branch', 'task'))
                or not Path(member['task']).is_absolute()
                or not isinstance(member.get('proof'), dict) or member['proof'].get('status') != 'verified'):
            raise ShippingError('invalid pinned stack member')
        if previous is None:
            if member['base_branch'] != 'main':
                raise ShippingError('the shipping root must target main')
        elif (member['base'], member['base_branch']) != (previous['head'], previous['head_branch']):
            raise ShippingError('the plan must follow one ordered dependency path')
        workflow.validate_required_ci_workflows(member.get('ci_workflows'))
        seen.add(member['number'])
        previous = member
    if frozen['ceiling'] != previous['number']:
        raise ShippingError('frozen ceiling does not match the selected members')


def _journal(path, digest, *, create=False):
    journal_path = path.with_suffix(path.suffix + '.journal.json')
    if journal_path.is_symlink():
        raise ShippingError('shipping journal must be a regular local file')
    journal = (cli.read(journal_path) if journal_path.exists() or not create else
               {'schema':cli.SCHEMA, 'plan_digest':digest, 'started_at':cli.now(),
                'steps':{}, 'events':[]})
    if journal.get('plan_digest') != digest:
        raise ShippingError('cannot resume shipping with a changed plan')
    return journal_path, journal


def _member(original, journal, repo):
    update = journal.get('restacked', {}).get(str(original['number']))
    if update is None:
        return original
    path = Path(update['receipt'])
    if path.is_symlink() or not path.is_absolute() or cli.digest(path) != update['receipt_sha256']:
        raise ShippingError('the restack receipt changed after publication')
    receipt = verify_pr._bounded_json(path, 64_000)
    identity = {'repo':repo, 'pr':original['number'], 'old_base':original['base'],
                'old_head':original['head'], 'head_branch':original['head_branch'],
                'parent_branch':original['base_branch'], 'old_task':original['task'],
                'shipping_plan_sha256':journal['plan_digest'], 'status':'verified'}
    proof = update['proof']
    if (any(receipt.get(key) != value for key, value in identity.items())
            or any(not isinstance(receipt.get(key), str) or not pr_watch.SHA.fullmatch(receipt[key])
                   for key in ('head', 'base'))
            or receipt['head'] in (original['head'], receipt['base'])
            or receipt['base'] == original['base'] or not Path(receipt['task']).is_absolute()
            or proof.get('status') != 'verified'
            or any(proof.get(key) != receipt['verification'].get(key) for key in ('review', 'review_sha256'))
            or cli.digest(path) != update['receipt_sha256']):
        raise ShippingError('the published restack is not bound to the original member and fresh proof')
    return {**original, 'head':receipt['head'], 'base':receipt['base'], 'task':receipt['task'],
            'base_branch':'main', 'proof':proof}


def _scope(github, repo):
    owner = repo.split('/')[0]
    data = github.repository(repo)
    if (github.login().casefold() != owner.casefold() or not isinstance(data, dict)
            or data.get('fork') is not True or data.get('default_branch') != 'main'
            or data.get('owner', {}).get('login', '').casefold() != owner.casefold()):
        raise ShippingError('shipping requires the signed-in personal fork and its main branch')


def _observe(github, repo, member):
    result = pr_watch._facts(github, repo, member['number'])
    if (result['isCrossRepository'] or result['headRefOid'] != member['head']
            or result['headRefName'] != member['head_branch']):
        raise ShippingError('the PR head or repository changed after verification')
    return result


def _proof(repo, member, task):
    if stack_verify._proof_locked(repo, member, task) != member['proof']:
        raise ShippingError('independent task evidence changed after the shipping plan was frozen')
    data = cli.read(task)
    delivery._merge_workflows(data, member.get('ci_workflows'))
    _, issues = cli.gate(data, member['head'])
    if issues:
        raise ShippingError('task cannot authorize a write: ' + '; '.join(issues))
    return data


def _landed(pull):
    return pull['state'] == 'MERGED' and pull['baseRefName'] == 'main'


def _confirmed_main(github, repo, member):
    trunk = github.branch(repo, 'main')
    if not isinstance(trunk, str) or not pr_watch.SHA.fullmatch(trunk):
        raise ShippingError('the verified PR head is not confirmed in main')
    if trunk != member['head'] and not github.contains(repo, member['head'], trunk):
        squash = getattr(github, 'squash_result', None)
        if not squash or not squash(repo, member['number'], member['head'], member['base'], trunk):
            raise ShippingError('the verified PR head is not confirmed in main')
    return trunk


def _ready(github, repo, member):
    status = pr_watch.snapshot(github, repo, member['number'], 'check', member['head'], member['base'])
    if (status['status'] != 'github-ready' or status['base_branch'] != 'main'
            or status['head_branch'] != member['head_branch']):
        raise ShippingError('current GitHub frontier is blocked: ' + status['reason'])


def execute(path, *, timeout=600, github=None, merge=delivery._guarded_merge):
    if type(timeout) is not int or not 1 <= timeout <= 86400:
        raise ShippingError('shipping needs a positive time limit')
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ShippingError('shipping plan must be a regular local file')
    path = path.resolve(strict=True)
    github = github or GitHub()
    deadline = time.monotonic() + timeout
    with task_lock(path, lock_name=f'.{path.name}.shipping.lock'):
        frozen = verify_pr._bounded_json(path, 128_000)
        _verify_plan(frozen)
        plan_digest = cli.digest(path)
        repo = frozen['repo']
        journal_path, journal = _journal(path, plan_digest, create=True)
        if isinstance(github, GitHub):
            github.set_deadline(deadline)
        _scope(github, repo)
        confirmed = []

        def save(member, phase, **fields):
            entry = {'pr':member['number'], 'phase':phase, 'at':cli.now(), **fields}
            journal['steps'][str(member['number'])] = entry
            journal['events'].append(entry)
            cli.write(journal_path, journal)

        def result(status, *, pr=None, reason=None):
            return {'status':status, 'repo':repo, 'ceiling':frozen['ceiling'], 'landed':confirmed,
                    'pr':pr, 'reason':reason, 'journal':str(journal_path)}

        for original in frozen['members']:
            number = original['number']
            if time.monotonic() >= deadline:
                return result('blocked', pr=number, reason='shipping-time-limit')
            if isinstance(github, GitHub):
                github.set_deadline(deadline)
            step = journal['steps'].get(str(number), {}).get('phase')
            try:
                member = _member(original, journal, repo)
                task = Path(member['task']).resolve(strict=True)
                pull = _observe(github, repo, member)
                if _landed(pull):
                    if step not in ('merge-intent', 'landed'):
                        raise ShippingError('PR was merged outside this frozen shipping run')
                    trunk_at_confirmation = _confirmed_main(github, repo, member)
                    if step != 'landed':
                        save(member, 'landed', head=member['head'], main=trunk_at_confirmation)
                    confirmed.append(number)
                    continue
                if step in ('merge-intent', 'landed'):
                    return result('awaiting-confirmation', pr=number, reason='merge-outcome-unconfirmed')
                if pull['state'] != 'OPEN' or pull['isDraft']:
                    raise ShippingError('the pinned PR must be open and ready for review')
                with task_lock(task):
                    run_control.admit(task, ShippingError)
                    if member is not original:
                        run_control.admit(original['task'], ShippingError)
                    data = _proof(repo, member, task)
                    trunk = github.branch(repo, 'main')
                    if trunk != member['base']:
                        reason = ('published-restack-base-moved' if member is not original
                                  else 'fresh-base-verification-required')
                        return {**result('blocked', pr=number, reason=reason),
                                'verified_base':member['base'], 'current_main':trunk}
                    if pull['baseRefName'] != 'main':
                        if step == 'retarget-intent':
                            return result('awaiting-confirmation', pr=number, reason='retarget-outcome-unconfirmed')
                        if not confirmed or pull['baseRefName'] != member['base_branch'] or pull['baseRefOid'] != trunk:
                            raise ShippingError('retarget would change the verified base or dependency')
                        if data.get('local_gate'):
                            delivery.require_local_write(task, data, member['head'], github, repo)
                            if (github.branch_info(repo, 'main').get('protected') is not False
                                    or github.branch_rules(repo, 'main') != []):
                                raise ShippingError('local-only retarget requires unprotected main without branch rules')
                        if github.branch(repo, 'main') != trunk or _observe(github, repo, member) != pull:
                            raise ShippingError('remote state changed before retarget')
                        if time.monotonic() >= deadline or cli.digest(path) != plan_digest:
                            raise ShippingError('shipping deadline or plan changed before retarget')
                        run_control.admit(task, ShippingError)
                        save(member, 'retarget-intent', head=member['head'], base=trunk)
                        github.retarget(repo, number)
                        pull = _observe(github, repo, member)
                    if pull['baseRefName'] != 'main' or pull['baseRefOid'] != member['base']:
                        raise ShippingError('GitHub has not confirmed the verified main base')
                    if journal['steps'].get(str(number), {}).get('phase') == 'retarget-intent':
                        save(member, 'retargeted', head=member['head'], base=member['base'])
                    _ready(github, repo, member)

                    def before_write():
                        if _member(original, journal, repo) != member:
                            raise ShippingError('published restack changed before merge')
                        _proof(repo, member, task)
                        if time.monotonic() >= deadline or cli.digest(path) != plan_digest:
                            raise ShippingError('shipping deadline or plan changed before merge')
                        run_control.admit(task, ShippingError)
                        if member is not original:
                            run_control.admit(original['task'], ShippingError)
                        save(member, 'merge-intent', head=member['head'], base=member['base'])

                    receipt = merge(task, repo, number, github, stack_binding=member,
                                    revalidate=before_write, deadline=deadline)
                    if (receipt.get('merged') is not True or receipt.get('verified_head') != member['head']
                            or not _landed(_observe(github, repo, member))):
                        raise ShippingError('GitHub has not confirmed landing into main')
                    save(member, 'landed', head=member['head'], main=_confirmed_main(github, repo, member))
                    confirmed.append(number)
            except (OSError, ValueError, KeyError, RuntimeError, cli.WorkbenchError, delivery.DeliveryError,
                    pr_watch.WatchError, start.StartError, autopilot.AutopilotError,
                    ShippingError) as error:
                pending = journal['steps'].get(str(number), {}).get('phase') in ('retarget-intent', 'merge-intent')
                return result('awaiting-confirmation' if pending else 'blocked', pr=number, reason=str(error))
        return result('complete')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    freeze = sub.add_parser('plan', help='Read GitHub and freeze the current verified prefix')
    freeze.add_argument('--repo', required=True)
    freeze.add_argument('--pr', type=int, required=True, action='append')
    freeze.add_argument('--tasks', required=True, help='JSON object mapping PR numbers to retained task files')
    freeze.add_argument('--ci-workflow', action='append')
    freeze.add_argument('--out', required=True, help='Create a new local plan file')
    land = sub.add_parser('land', help='Execute the explicitly selected frozen plan')
    land.add_argument('--plan', required=True)
    land.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan':
            tasks = stack_verify.load_tasks(args.tasks)
            report = plan(args.repo, args.pr, tasks, ci_workflows=args.ci_workflow)
            with Path(args.out).expanduser().open('x', encoding='utf-8') as stream:
                json.dump(report, stream, indent=2, ensure_ascii=False)
                stream.write('\n')
            code = 0 if report['members'] else 1
        else:
            report = execute(args.plan, timeout=args.timeout)
            code = 0 if report['status'] == 'complete' else 1
    except (OSError, ValueError, ShippingError, verify_pr.VerifyError, delivery.DeliveryError,
            pr_watch.WatchError, workflow.WorkflowError, cli.WorkbenchError) as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
