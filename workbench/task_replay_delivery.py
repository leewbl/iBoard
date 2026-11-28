"""Advance a verified queue replay through its original PR authority."""

from pathlib import Path
import re

from workbench import (autonomous_run, autopilot, autopilot_full, cli, delivery, publish,
                       run_control, stack_ship, task_replay)
from workbench.task_lock import task_lock


class ReplayDeliveryError(ValueError):
    pass


class GitHub(publish.Publisher, stack_ship.GitHub):
    """Read exact main ancestry and write only through the existing delivery guards."""


ERRORS = (*task_replay.ERRORS, publish.PublishError, stack_ship.ShippingError)


def _regular(path):
    if path.is_symlink() or not path.is_file():
        raise ReplayDeliveryError(f'missing or unsafe delivery record: {path.name}')
    return path


def _number(value):
    return type(value) is int and value > 0


def _previous(original, member, report):
    state = autonomous_run._state(original)
    prior = state.get('published_pr')
    if prior is None:
        return None
    if (not isinstance(prior, dict) or not _number(prior.get('pr')) or prior.get('repo') != member['destination']
            or prior.get('head') != report['origin']['head']):
        raise ReplayDeliveryError('recorded publication does not match the original verified PR head')
    return {key: prior[key] for key in ('repo', 'pr', 'head')}


class ReplayAuthority:
    """Recheck the original queue authorization and the fresh replay before each write."""

    def __init__(self, plan, digest, member, original, child, github, journal_path, journal):
        self.plan, self.digest = plan, digest
        self.member, self.original, self.child, self.github = member, original, child, github
        self.journal_path, self.journal = journal_path, journal
        self.repo = member['destination']
        self.branch = f"workbench/{member['id']}"

    def evidence(self):
        queue = autopilot_full._read_pinned(self.plan, self.digest)
        if self.member not in queue['members'] or run_control.group(self.original) != self.plan:
            raise ReplayDeliveryError('the origin is no longer enrolled in the approved queue')
        approved = autopilot_full._read_pinned(Path(self.member['plan']), self.member['sha256'])
        origin_data, _ = autonomous_run._assert_task(self.original, approved, {})
        if autonomous_run._supported(origin_data, self.repo) != self.member['predicate']:
            raise ReplayDeliveryError('the origin no longer authorizes this delivery outcome')
        if cli.read(_regular(self.journal_path)) != self.journal:
            raise ReplayDeliveryError('retained replay delivery journal changed during execution')
        reference = self.journal['report']
        path = _regular(Path(reference['path']))
        report = autopilot_full._read_pinned(path, reference['sha256'])
        data = cli.read(_regular(self.child))
        state = cli.read(_regular(self.child.with_name('autopilot.json')))
        binding = data['baseline_replay']
        if (report.get('status') != 'verified' or state['phase'] != 'delivery'
                or path != self.child.with_name('result.json')
                or path.parent.name != binding['base']
                or report.get('task') != str(self.child)
                or report.get('origin') != binding['origin']
                or report['origin'].get('queue_sha256') != self.digest
                or report['origin'].get('task') != str(self.original)
                or report['origin'].get('plan_sha256') != self.member['sha256']
                or report['origin'].get('destination') != self.repo
                or report['origin'].get('base') != self.member['base']
                or report.get('base') != binding['base'] or report.get('candidate') != binding['candidate']
                or report.get('patch') != binding['patch']
                or report.get('criteria_sha256') != cli.criteria_digest(data)):
            raise ReplayDeliveryError('fresh review is outside the exact queue task, destination, or candidate')
        task_replay.validate_child(self.child, data, state)
        head, issues = cli.evidence_gate(data, binding['candidate'])
        if issues or head != binding['candidate']:
            raise ReplayDeliveryError('replayed candidate no longer passes acceptance: ' + '; '.join(issues))
        review = delivery._independent_verification(data, head, destination=self.repo)
        if report.get('review') != {key: review[key] for key in ('path', 'sha256')}:
            raise ReplayDeliveryError('fresh independent review no longer matches the replay receipt')
        if _previous(self.original, self.member, report) != self.journal['previous']:
            raise ReplayDeliveryError('the original publication identity changed')
        return report, data

    def admit(self, data):
        run_control.admit(self.original, ReplayDeliveryError)
        run_control.admit(self.child, ReplayDeliveryError)
        autopilot._budget_remaining(data)

    def pull(self, number, heads, *, base=None, states=('OPEN',)):
        pull = self.github.pull(self.repo, number)
        if (pull.get('number') != number or pull.get('state') not in states
                or pull.get('headRefName') != self.branch or pull.get('headRefOid') not in heads
                or pull.get('baseRefName') != 'main' or pull.get('isDraft') is not False
                or pull.get('isCrossRepository') is not False
                or (base is not None and pull.get('baseRefOid') != base)):
            raise ReplayDeliveryError('GitHub PR no longer matches the original branch and verified revision')
        return pull

    def check(self, task, repo, action, number, previous):
        if Path(task).resolve() != self.child or repo != self.repo:
            raise ReplayDeliveryError('replay cannot authorize a write outside its original task and destination')
        report, data = self.evidence()
        phase = self.journal['phase']
        prior = self.journal['previous']
        publication = self.journal.get('publication') or {}
        if action == 'create' and phase == 'publish' and prior is None:
            pass
        elif (action == 'replace' and phase == 'publish' and prior and
              (number, previous) == (prior['pr'], prior['head'])):
            self.pull(number, (previous, report['candidate']), base=report['base'])
        elif (action in ('merge', 'close') and self.member['predicate'] == 'merged'
              and phase == {'merge': 'merge-intent', 'close': 'merged'}[action]
              and number == publication.get('pr')):
            self.pull(number, (report['candidate'],), base=report['base'] if action == 'merge' else None,
                      states=('OPEN',) if action == 'merge' else ('MERGED',))
        else:
            raise ReplayDeliveryError('replay cannot authorize this PR or operation')
        if action != 'close' and self.github.branch(self.repo, 'main') != report['base']:
            raise ReplayDeliveryError('main moved after the replay was independently verified')
        self.admit(data)


def _result(authority, status, **fields):
    return {'status': status, 'delivery_journal': str(authority.journal_path), **fields}


def _save(authority, phase, **fields):
    journal = authority.journal
    journal.update(phase=phase, **fields)
    journal['events'].append({'at': cli.now(), 'phase': phase, **fields})
    cli.write(authority.journal_path, journal)


def _deliver(authority, ci):
    report, data = authority.evidence()
    github, repo, child = authority.github, authority.repo, authority.child
    head, base = report['candidate'], report['base']
    if isinstance(github, delivery.GitHub):
        github.set_task_deadline(data)
    stack_ship._scope(github, repo)
    authority.admit(data)
    journal = authority.journal
    if data['status'] not in ('running', 'paused'):
        raise ReplayDeliveryError('invalid replay task status for guarded delivery')
    if data['status'] == 'paused':
        data['status'] = 'running'
        cli.write(child, data)
    try:
        if journal['phase'] == 'publish':
            with run_control.delivery_lane(authority.original, repo):
                previous = journal['previous']
                if previous:
                    receipt = publish.republish(child, repo, previous['pr'], previous['head'], github,
                                                replay_authority=authority)
                else:
                    receipt = publish.publish(child, repo, github, replay_authority=authority)
            if (receipt['repo'] != repo or receipt['head'] != head or not _number(receipt.get('pr'))
                    or (previous and receipt['pr'] != previous['pr'])):
                raise ReplayDeliveryError('GitHub publication did not confirm the exact replay candidate')
            _save(authority, 'published', publication=receipt)

        publication = journal['publication']
        number = publication['pr']
        if not _number(number) or publication['repo'] != repo or publication['head'] != head:
            raise ReplayDeliveryError('retained publication does not match the replay')
        remote = {'repo': repo, 'pr': number, 'head': head, 'base': base, 'url': publication['url']}
        proof = {'candidate': head, 'base': base, 'criteria_sha256': report['criteria_sha256'],
                 'review': report['review'], 'remote': remote}

        if authority.member['predicate'] == 'published':
            authority.pull(number, (head,), base=base)
            if github.branch(repo, 'main') != base:
                raise ReplayDeliveryError('main moved after replay publication')
            remote['state'] = 'OPEN'
            return _result(authority, 'complete', proof=proof)

        pull = authority.pull(number, (head,), states=('OPEN', 'MERGED'))
        if pull['state'] == 'OPEN':
            if journal['phase'] not in ('published', 'merge-intent'):
                raise ReplayDeliveryError('retained merge outcome changed after recording')
            if pull.get('baseRefOid') != base or github.branch(repo, 'main') != base:
                raise ReplayDeliveryError('main moved before the replayed PR could merge')
            authority.admit(data)
            if not data.get('local_gate') and not ci(child, repo, number, head, github=github, once=True):
                return _result(authority, 'waiting-ci', identity=remote)
            with run_control.delivery_lane(authority.original, repo):
                if journal['phase'] == 'published':
                    _save(authority, 'merge-intent')
                receipt = delivery.guarded_merge(child, repo, number, github, replay_authority=authority)
                if receipt != {'merged': True, 'repo': repo, 'pr': number, 'verified_head': head}:
                    raise ReplayDeliveryError('merge did not return the exact authorized receipt')
            authority.pull(number, (head,), states=('MERGED',))
        if journal['phase'] not in ('merge-intent', 'merged', 'complete'):
            raise ReplayDeliveryError('PR was merged without a recorded queue merge intent')
        remote['main'] = stack_ship._confirmed_main(github, repo, {**remote, 'number': number})
        remote['state'] = 'MERGED'
        if journal['phase'] == 'merge-intent':
            _save(authority, 'merged', merged_main=remote['main'])
        if journal['phase'] == 'merged':
            with run_control.delivery_lane(authority.original, repo):
                issue = delivery.close_verified_issue(child, repo, number, github, replay_authority=authority)
            _save(authority, 'complete', issue=issue)
        return _result(authority, 'complete', proof=proof, issue=journal['issue'])
    finally:
        # Safe CI checkpoints and interruptions keep the verification-only task paused.
        latest = cli.read(child)
        if latest['status'] == 'running':
            latest['status'] = 'paused'
            cli.write(child, latest)


def advance(plan, digest, member, replay=None, *, github=None, ci=autopilot._wait_for_ci):
    plan, queue = autopilot_full._load(plan, digest)
    if member not in queue['members'] or member['predicate'] not in ('published', 'merged'):
        return None
    original = Path(member['task'])
    root = plan.parent / 'replays' / member['id']
    journal_path = root / 'delivery.json'
    if not journal_path.exists() and not journal_path.is_symlink() and replay is None:
        return None
    if root.is_symlink() or journal_path.is_symlink():
        raise ReplayDeliveryError('replay delivery records must stay in the queue evidence directory')
    if journal_path.exists():
        journal = cli.read(_regular(journal_path))
    else:
        approval = {'path': replay['report'], 'sha256': replay['sha256']}
        journal = {'schema': cli.SCHEMA, 'queue_sha256': digest, 'task': str(original), 'report': approval,
                   'phase': 'publish', 'previous': _previous(original, member, replay), 'events': []}
    if journal.get('queue_sha256') != digest or journal.get('task') != str(original):
        raise ReplayDeliveryError('delivery journal belongs to another approved queue or task')
    report_path = Path(journal['report']['path'])
    child = report_path.with_name('task.json')
    marker = report_path.with_name('.replay')
    if (report_path.name != 'result.json' or report_path.parent.parent != root
            or report_path.resolve() != report_path
            or not re.fullmatch(r'[a-f0-9]{40}', report_path.parent.name)):
        raise ReplayDeliveryError('delivery requires a replay created by the approved queue')
    with task_lock(_regular(marker), lock_name='.replay.lock', reentrant=False), \
            task_lock(original, reentrant=False), task_lock(_regular(child), reentrant=False):
        if not journal_path.exists():
            cli.write(journal_path, journal)
        authority = ReplayAuthority(plan, digest, member, original, child, github or GitHub(),
                                    journal_path, journal)
        try:
            return _deliver(authority, ci)
        except ERRORS as error:
            held = next((entry for entry in (run_control.read(original), run_control.read(child))
                         if entry and entry['status'] == 'held'), None)
            if held:
                return _result(authority, 'held', request=held['request'])
            if isinstance(error, autopilot.CIFailure):
                path = root / 'ci-failure.json'
                publication = journal.get('publication') or {}
                cli.write(path, {**error.evidence, 'schema': cli.SCHEMA, 'repo': member['destination'],
                                 'pr': publication.get('pr'), 'base': report_path.parent.name})
                return _result(authority, 'blocked', reason=str(error)[:1400], failure=str(path))
            return _result(authority, 'blocked', reason=str(error)[:1400])
