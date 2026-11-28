"""Find the verified prefix of a GitHub PR stack using retained task evidence."""

import argparse
import json
from pathlib import Path
import sys

from workbench import autopilot, cli, delivery, pr_watch, start, verify_pr
from workbench.task_lock import observe_task_lock


PSTACK_SOURCES = ('skills/poteto-mode/playbooks/shipping.md', 'skills/poteto-mode/playbooks/autopilot-stack.md')


def _proof(repo, member, task_path):
    task = Path(task_path).expanduser().resolve(strict=True)
    with observe_task_lock(task) as lock:
        if lock == 'held':
            return {'status':'unproven', 'reason':'task-operation-in-progress'}
        return _proof_locked(repo, member, task)


def _proof_locked(repo, member, task):
    data = cli.read(task)
    state = cli.read(task.with_name('autopilot.json'))
    head, base, number = member['head'], member['base'], member['number']
    if data['base_commit'] != base or state.get('candidate_commit') != head:
        return {'status':'stale', 'reason':'local-evidence-does-not-match-the-current-pair'}
    stage = start._read_input_stage(task)
    if (stage['phase'] != 'ready' or state.get('issue_digest') != stage.get('request_digest')
            or state.get('input_digest') != stage.get('input_digest')):
        return {'status':'unproven', 'reason':'original-input-binding-unavailable'}
    verification = state.get('verification')
    target = data.get('verification_target')
    if target is not None:
        autopilot._validate_verification_target(data, target, stage['request_digest'], stage['run_settings'], number)
        if (target['repo'] != repo or state.get('verification_target') != target or state.get('phase') != 'complete'
                or not isinstance(verification, dict) or verification.get('verdict') != 'PASS'
                or (verification.get('repo'), verification.get('pr'), verification.get('head'),
                    verification.get('base'), verification.get('criterion_digest'))
                != (repo, number, head, base, cli.criteria_digest(data))):
            return {'status':'unproven', 'reason':'existing-pr-verification-receipt-unavailable'}
    else:
        published = state.get('published_pr') or {}
        if ((published.get('repo'), published.get('pr'), published.get('head')) != (repo, number, head)
                or member['head_branch'] != f"workbench/{data['id']}"
                or stage['run_settings'].get('pr_repo') != repo):
            return {'status':'unproven', 'reason':'retained-task-does-not-identify-the-selected-pr'}
    _, issues = cli.evidence_gate(data, head)
    if issues:
        return {'status':'unproven', 'reason':'local-evidence-gate-failed', 'details':issues}
    verdict = delivery._independent_verification(data, head, destination=repo)
    if target is not None and (verification.get('review'), verification.get('review_sha256'),
                               verification.get('verifier')) != (verdict['path'], verdict['sha256'], verdict['actor']):
        return {'status':'unproven', 'reason':'independent-review-receipt-does-not-match'}
    return {'status':'verified', 'review':verdict['path'], 'review_sha256':verdict['sha256'],
            'verifier':verdict['actor'], 'criterion_digest':cli.criteria_digest(data)}


def _prefix(members, trunk):
    verified, stop = [], None
    accepted_parents = {}
    for index, member in enumerate(members):
        number = member['number']
        if not member['cross_repository'] and member['head_branch'] in accepted_parents:
            stop = {'pr':number, 'reason':'ambiguous-stack-branch'}
            break
        if index == 0:
            if member['base_branch'] != trunk:
                stop = {'pr':number, 'reason':'root-does-not-target-the-default-branch'}
        else:
            parent = accepted_parents.get(member['base_branch'])
            if parent != member['base']:
                stop = {'pr':number, 'reason':'parent-revision-unavailable-or-changed'}
        if stop is not None:
            break
        if member['state'] == 'MERGED':
            if member['base_branch'] != trunk:
                stop = {'pr':number, 'reason':'merged-into-parent-rather-than-default-branch'}
                break
        elif member['state'] != 'OPEN' or member['proof']['status'] != 'verified':
            stop = {'pr':number, 'reason':member['proof'].get('reason', 'pr-is-not-open')}
            break
        else:
            verified.append(number)
        if not member['cross_repository']:
            accepted_parents[member['head_branch']] = member['head']
    return verified, stop


def audit(repo, numbers, tasks, *, discover=False, timeout=90, reader=None):
    if not isinstance(tasks, dict) or len(tasks) > 15:
        raise verify_pr.VerifyError('expected a bounded mapping from PR numbers to retained task files')
    reader = reader or pr_watch.GitHubReader()
    observed = pr_watch.watch(repo, numbers, mode='check', discover=discover, timeout=timeout, reader=reader)
    entries = observed.get('members', [observed] if 'head' in observed else [])
    report = {'schema':1, 'repo':repo, 'observed_at':cli.now(), 'watch':observed,
              'members':[], 'verified_prefix':[], 'ceiling':None}
    if not entries:
        return {**report, 'status':'remote-unconfirmed'}
    repository = reader.repository(repo)
    trunk = repository.get('default_branch') if isinstance(repository, dict) else None
    if not isinstance(trunk, str) or not trunk:
        raise verify_pr.VerifyError('could not identify the repository default branch')
    members = []
    for entry in entries:
        number = entry['number']
        path = tasks.get(number)
        if entry['state'] == 'MERGED' and entry['base_branch'] == trunk:
            proof = {'status':'already-merged'}
        elif path is None:
            proof = {'status':'unproven', 'reason':'no-retained-task-selected'}
        else:
            try:
                proof = _proof(repo, entry, path)
            except (OSError, ValueError, KeyError, TypeError, autopilot.AutopilotError, cli.WorkbenchError,
                    delivery.DeliveryError, start.StartError) as error:
                proof = {'status':'unproven', 'reason':'retained-proof-unavailable', 'detail':str(error)[:1000]}
        members.append({**entry, 'proof':proof, 'task':str(path) if path is not None else None})
    for member in members:
        latest = pr_watch._facts(reader, repo, member['number'])
        if (latest['headRefOid'], latest['baseRefOid'], latest['state'], latest['isDraft']) != (
                member['head'], member['base'], member['state'], member['draft']):
            return {**report, 'members':members, 'status':'stale',
                    'stop':{'pr':member['number'], 'reason':'remote-changed-during-evidence-audit'}}
    prefix, stop = _prefix(members, trunk)
    return {**report, 'default_branch':trunk, 'members':members, 'verified_prefix':prefix,
            'ceiling':prefix[-1] if prefix else None, 'stop':stop,
            'status':'verified' if stop is None else 'verification-gap'}


def load_tasks(path):
    manifest = Path(path).expanduser().resolve(strict=True)
    raw = verify_pr._bounded_json(manifest, 64_000)
    if (not isinstance(raw, dict) or len(raw) > 15
            or any(not key.isascii() or not key.isdecimal() or len(key) > 10
                   or key != str(int(key)) or not 1 <= int(key) <= 2_147_483_647
                   or not isinstance(value, str) or not value.strip() for key, value in raw.items())):
        raise verify_pr.VerifyError('task manifest requires at most 15 canonical PR numbers and file paths')
    return {int(key):manifest.parent / Path(value).expanduser() for key, value in raw.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--pr', type=int, action='append', required=True)
    parser.add_argument('--discover-stack', action='store_true')
    parser.add_argument('--tasks', help='JSON object mapping PR numbers to task.json files')
    parser.add_argument('--timeout', type=int, default=90)
    args = parser.parse_args(argv)
    try:
        tasks = load_tasks(args.tasks) if args.tasks else {}
        report = audit(args.repo, args.pr, tasks, discover=args.discover_stack, timeout=args.timeout)
    except (OSError, ValueError, verify_pr.VerifyError, pr_watch.WatchError, delivery.DeliveryError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'verified' else 1


if __name__ == '__main__':
    sys.exit(main())
