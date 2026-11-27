"""Deliver an independently verified Benny repair as one draft PR on its personal fork."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import time

from workbench import autopilot, benny, benny_handoff, benny_repair, benny_reproduce, cli, delivery
from workbench import project_verify, publish, run_control, start, verify_openclaw
from workbench.benny_slack import Slack
from workbench.task_lock import task_lock


PHASES = ('push-intent', 'pushed', 'create-intent', 'draft')
CREDENTIAL = re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:gh[pousr]_|github_pat_|xox[baprs]-|sk-(?:proj-)?)'
                        rb'[A-Za-z0-9_-]{12,}|\bAKIA[A-Z0-9]{16}\b')


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _diff(data, head):
    repo, base = data['repo'], data['base_commit']
    net = delivery.candidate_diff(repo, base, head)
    history = cli.git(repo, 'rev-list', '--reverse', '--parents', '--max-count=21', f'{base}..{head}').splitlines()
    if not net or not 1 <= len(history) <= 20:
        raise ValueError('Benny requires a bounded, nonempty outgoing change')
    expected = base
    for line in history:
        commit, *parents = line.split()
        if parents != [expected]:
            raise ValueError('Benny outgoing commits must form one ordered chain from the verified baseline')
        patch = delivery.candidate_diff(repo, expected, commit)
        if (b'\nBinary files ' in patch or b'\nGIT binary patch' in patch
                or any(CREDENTIAL.search(line[1:]) for line in patch.splitlines() if line.startswith(b'+'))):
            raise ValueError('Benny outgoing history contains probable credentials or binary changes without review')
        names = cli.git(repo, 'diff', '--no-renames', '--name-only', '--diff-filter=ACMT', '-z',
                        expected, commit, '--')
        for path in names.rstrip('\0').split('\0'):
            autopilot._reject_credential_artifact(path)
        expected = commit
    if expected != head:
        raise ValueError('Benny outgoing commit list does not end at the verified revision')
    return hashlib.sha256(net).hexdigest()


def _public_text(text):
    return benny_handoff._text(publish._public_log_excerpt(text))


def _presentation(data, accepted, route, contract):
    head, base = accepted['head'], accepted['base']
    review = json.loads(Path(accepted['independent']['path']).read_text(encoding='utf-8'))
    causal = review['causal_verification']
    title = 'Fix ' + contract['journey']['id'].replace('-', ' ')
    journey = contract['journey']
    body = [
        '## Reproduction\n',
        '    ' + ' '.join(_public_text(journey[name]) for name in ('reach', 'drive', 'observe')),
        '\n## Root cause\n',
        '    ' + _public_text(causal['explanation']),
        '\n## Verification\n',
        f'- Baseline `{base}`: primary path and independent probe each observed the reported fault twice.',
        f'- Candidate `{head}`: both probes passed twice in separate offline Docker containers.',
        f'- Frozen reproduction SHA-256 `{data["benny_reproduction"]["sha256"]}`.',
        f'- Candidate replay SHA-256 `{accepted["candidate"]["sha256"]}`.',
        f'- Independent review SHA-256 `{accepted["independent"]["sha256"]}`.',
    ]
    if 'regression_sha256' in review:
        body.append('- Focused regression failed on the baseline and passed on the candidate.')
    if verify_openclaw.applies(data, route['rules']['repository']):
        for journey in verify_openclaw.required_journeys(data, head):
            body.append(f'- OpenClaw `{journey}` passed in two independent offline runs.')
    if review.get('impact'):
        body.extend(['\n## Related behavior\n', '    ' + _public_text(review['impact']['observed'])])
    tracker = route['decision']['verdict']['tracker']
    issue = delivery.own_issue_number(tracker, route['rules']['repository'])
    if issue is not None:
        body.append(f'\nRefs #{issue}')
    return title, '\n'.join(body) + '\n'


def _journal(path, plan):
    if path.is_symlink():
        raise ValueError('Benny draft journal cannot be a symlink')
    if not path.exists():
        return None
    result = cli.read(path)
    if result.get('plan') != plan or result.get('phase') not in PHASES:
        raise ValueError('Benny draft operation is already bound to different evidence')
    return result


def _save(path, plan, phase, **fields):
    record = {'schema': cli.SCHEMA, 'plan': plan, 'phase': phase, 'at': cli.now(), **fields}
    cli.write(path, record)
    return record


def deliver(*, task, slack, fix_search=(), tracker=None, github=None, repair=benny_repair.run_repair):
    task = Path(task).expanduser().resolve(strict=True)
    with task_lock(task):
        accepted = repair(task=task, slack=slack, fix_search=fix_search, tracker=tracker)
        data = cli.read(task)
        binding = data['benny_reproduction']
        _, proof, contract, _ = benny_reproduce._approved(binding['path'], binding['sha256'])
        _, route = benny.load_route(proof['route']['path'], proof['route']['sha256'])
        source = route['snapshot']['permalink']
        repo = proof['repository']
        head, base = accepted['head'], accepted['base']
        if data.get('source') != source or data.get('original_source') != source or accepted['status'] != 'verified':
            raise ValueError('Benny draft must match the verified source task')
        branch = 'benny/report-' + _sha(source)[:32]
        publisher = github or publish.Publisher()
        remote = publisher.repository(repo)
        owner = repo.split('/')[0].casefold()
        if (publisher.login().casefold() != owner or not remote.get('fork') or remote.get('default_branch') != 'main'
                or remote.get('owner', {}).get('login', '').casefold() != owner
                or str(remote.get('full_name', '')).casefold() != repo.casefold()):
            raise ValueError('Benny draft delivery requires the signed-in user’s configured personal fork')
        title, body = _presentation(data, accepted, route, contract)
        plan = {'repository': repo, 'branch': branch, 'base': base, 'head': head,
                'route_sha256': proof['route']['sha256'], 'before_sha256': binding['sha256'],
                'after_sha256': accepted['candidate']['sha256'], 'review_sha256': accepted['independent']['sha256'],
                'criteria_sha256': cli.criteria_digest(data), 'diff_sha256': _diff(data, head),
                'title': title, 'body_sha256': _sha(body)}
        path = task.with_name('benny-draft.json')
        record = _journal(path, plan)
        digest = cli.digest(task)

        def local_gate():
            latest = cli.read(task)
            if cli.digest(task) != digest:
                raise ValueError('Benny task record changed before delivery')
            current, issues = cli.evidence_gate(latest, head)
            if current != head or issues:
                raise ValueError('Benny draft requires current acceptance evidence and a clean candidate')
            benny_repair.candidate_proof(binding, accepted['candidate'], head)
            review = delivery._independent_verification(latest, head)
            if review != accepted['independent']:
                raise ValueError('Benny independent reviewer or receipt changed')
            verify_openclaw.require(latest, head, repo, review_item=review)
            if latest.get('local_gate'):
                delivery.require_local_write(task, data, head, publisher, repo, verified_benny_pause=True)
            return latest

        def remote_state():
            if publisher.branch(repo, 'main') != base:
                raise ValueError('Benny draft base moved since offline verification')
            current = publisher.remote_head(repo, branch)
            pulls = publisher.pulls(repo, branch, base=None, state='all')
            if not isinstance(pulls, list) or len(pulls) > 1:
                raise ValueError('Benny report branch has ambiguous pull requests')
            return current, pulls

        def confirmed(pull):
            number = pull.get('number')
            if (type(number) is not int or number < 1 or pull.get('state') != 'OPEN' or pull.get('isDraft') is not True
                    or not publish._matches_pr(pull, repo, number, branch, head, base)
                    or pull.get('body') != body or pull.get('title') != title):
                raise ValueError('GitHub pull request does not match the expected Benny draft')
            return {'status': 'draft', 'repo': repo, 'pr': number, 'head': head, 'base': base, 'branch': branch,
                    'url': f'https://github.com/{repo}/pull/{number}', 'receipt': str(path)}

        local_gate()
        current, pulls = remote_state()
        if pulls:
            if not record or record['phase'] not in ('create-intent', 'draft') or current != head:
                raise ValueError('existing pull request is outside this Benny draft operation')
            result = confirmed(pulls[0])
            if record['phase'] == 'draft' and record.get('pr') != result['pr']:
                raise ValueError('retained Benny draft changed identity')
            if record['phase'] != 'draft':
                _save(path, plan, 'draft', pr=result['pr'])
            return result
        if record and record['phase'] in ('create-intent', 'draft'):
            raise ValueError('Benny draft creation has no matching remote receipt; inspect GitHub before retrying')
        if current is not None and (current != head or record is None):
            raise ValueError('Benny report branch already exists outside this verified operation')
        if current is None and record and record['phase'] == 'pushed':
            raise ValueError('Benny report branch disappeared after the verified push')

        def admit_write(expected_branch):
            run_control.admit(task)
            budget = autopilot._budget_remaining(data)
            terms = binding.get('fix_search', fix_search)
            if binding.get('fix_search') and fix_search and list(fix_search) != terms:
                raise ValueError('Benny draft cannot change the approved GitHub search')
            deadline = time.monotonic() + min(90, budget)
            benny_reproduce._check_repair_owner(slack, proof, terms, deadline, tracker)
            if isinstance(publisher, delivery.GitHub):
                publisher.set_task_deadline(data)
            actual, existing = remote_state()
            if actual != expected_branch or existing:
                raise ValueError('Benny report branch or pull request changed immediately before delivery')
            benny_reproduce._source(slack, route, deadline)
            local_gate()
            run_control.admit(task)
            autopilot._budget_remaining(data)

        if current is None:
            admit_write(None)
            _save(path, plan, 'push-intent')
            push_error = None
            try:
                publisher.push_new(data['repo'], repo, branch, head)
            except publish.PublishError as error:
                push_error = error
            current, pulls = remote_state()
            if current != head or pulls:
                raise ValueError('Benny branch push could not be confirmed') from push_error
        _save(path, plan, 'pushed')
        admit_write(head)
        _save(path, plan, 'create-intent')
        number, create_error = None, None
        try:
            number = publisher.create(repo, branch, data['id'],
                                      {'_prepared_pr_body': body, '_prepared_pr_title': title}, draft=True)
        except publish.PublishError as error:
            create_error = error
        current, pulls = remote_state()
        if current != head or len(pulls) != 1 or (number is not None and pulls[0].get('number') != number):
            raise ValueError('Benny draft creation could not be confirmed') from create_error
        result = confirmed(pulls[0])
        _save(path, plan, 'draft', pr=result['pr'])
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--fix-search', action='append', default=[], help='Original terms for older tasks.')
    args = parser.parse_args(argv)
    try:
        result = deliver(task=args.task, slack=Slack(args.token_file), fix_search=args.fix_search)
    except (*project_verify.ERRORS, start.StartError, autopilot.AutopilotError, delivery.DeliveryError,
            publish.PublishError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
