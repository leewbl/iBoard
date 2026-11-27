"""Reconstruct a retained task's progress and next safe step without rerunning it."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re

from workbench import autopilot, cli, decisions, delivery, run_control, start
from workbench.task_lock import observe_task_lock


PSTACK_SOURCES = {
    'skills/poteto-mode/playbooks/session-pickup.md': 'python3 -m workbench.pickup',
    'skills/ketchup/SKILL.md': 'python3 -m workbench.pickup --check-pr',
}
INTERRUPTED = {'implementing', 'repairing', 'adopting'}
BASELINE = {'plan', 'reproduce', 'implement'}
CANDIDATE = {'verify', 'delivery', 'close-issue', 'complete'}


def _sha(value):
    return isinstance(value, str) and bool(re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', value))


def _pr_identity(record, head):
    if not isinstance(record, dict) or not _sha(head):
        return None
    repo, number = record.get('repo'), record.get('pr')
    if (not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)
            or any(part in ('.', '..') for part in repo.split('/'))
            or type(number) is not int or number < 1):
        return None
    return {'repo':repo, 'number':number, 'candidate':head, 'url':f'https://github.com/{repo}/pull/{number}'}


def _evidence(task, data, head, workspace):
    valid, invalid = [], []
    for item in data.get('evidence', []):
        if item.get('superseded') or item.get('commit') not in (head, data['base_commit']):
            continue
        summary = {key:item.get(key) for key in ('id', 'kind', 'actor', 'commit', 'path', 'sha256')}
        try:
            path = Path(item['path'])
            resolved = path.resolve(strict=True)
            if (path.is_symlink() or not resolved.is_file() or
                    not (resolved.is_relative_to(task.parent) or resolved.is_relative_to(workspace)) or
                    cli.digest(resolved) != item.get('sha256')):
                raise ValueError('evidence identity changed or is outside the task')
            if item.get('execution_report'):
                run = Path(item['execution_report'])
                if (run.is_symlink() or run.resolve(strict=True).parent != task.parent or
                        cli.digest(run) != item.get('execution_report_sha256')):
                    raise ValueError('linked execution report changed')
        except (OSError, KeyError, TypeError, ValueError, cli.WorkbenchError):
            invalid.append(summary)
        else:
            valid.append(summary)
    return {'retained':valid, 'invalid':invalid}


def _timeline(data, since):
    def parse(value):
        stamp = datetime.fromisoformat(value.removesuffix('Z') + ('+00:00' if value.endswith('Z') else ''))
        if stamp.tzinfo is None:
            raise ValueError('timestamp has no timezone')
        return stamp

    cutoff = parse(since) if since is not None else None
    events = []
    for item in (*data.get('events', []), *data.get('criterion_changes', [])):
        if not isinstance(item, dict) or not isinstance(item.get('at'), str):
            continue
        try:
            when = parse(item['at'])
        except ValueError:
            continue
        if cutoff is not None and when <= cutoff:
            continue
        events.append((when, {k:item[k] for k in ('at', 'type', 'action', 'id', 'number', 'reason', 'failure')
                              if k in item}))
    return [item for _, item in sorted(events, key=lambda entry:entry[0])[-30:]]


def _bound_review(data, candidate, retained):
    for item in retained:
        if item['kind'] != 'independent-review' or item['commit'] != candidate or item['actor'] == data['implementer']:
            continue
        try:
            review = json.loads(Path(item['path']).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if (isinstance(review, dict) and review.get('verdict') == 'PASS'
                and review.get('candidate_commit') == candidate and review.get('baseline_commit') == data['base_commit']
                and review.get('criterion_digest') == cli.criteria_digest(data)):
            return item['path']
    return None


def _checks(rollup):
    if not isinstance(rollup, list):
        return None
    counts = {}
    for check in rollup:
        if not isinstance(check, dict):
            label = 'unknown'
        elif check.get('status') in ('QUEUED', 'REQUESTED', 'IN_PROGRESS', 'PENDING', 'WAITING'):
            label = 'pending'
        else:
            value = check.get('conclusion') or check.get('state')
            label = value.lower() if value in ('SUCCESS', 'FAILURE', 'ERROR', 'CANCELLED', 'NEUTRAL', 'SKIPPED',
                                                'TIMED_OUT', 'ACTION_REQUIRED', 'PENDING') else 'unknown'
        counts[label] = counts.get(label, 0) + 1
    return counts


def _inspect(task, lock, since):
    data = cli.read(task)
    report = {'id':data.get('id'), 'base':data['base_commit'], 'recorded_status':data['status'],
              'lock':lock, 'observed_at':cli.now(), 'events':_timeline(data, since)}
    if lock == 'held':
        return {**report, 'execution':'active', 'next':'wait-for-current-operation'}

    state_file = task.parent / 'autopilot.json'
    state = cli.read(state_file) if state_file.exists() else {'phase':'plan'}
    phase, candidate = state['phase'], state.get('candidate_commit')
    workspace = Path(data['repo']).resolve()
    report.update(phase=phase, candidate=candidate, original_source=data.get('original_source', data.get('source')),
                  criteria=data.get('criteria', []), evidence=_evidence(task, data, candidate, workspace))
    try:
        rows = decisions.read(task)
        if rows:
            report['decisions'] = {'file':str(task.with_name('decisions.tsv')), 'total':len(rows), 'recent':rows[-6:]}
    except (OSError, ValueError, decisions.DecisionError) as error:
        report['decisions'] = {'error':f'could not read retained decisions: {error}'}
    report['bound_review_record'] = _bound_review(data, candidate, report['evidence']['retained'])
    try:
        head = cli.sha(workspace, 'HEAD', timeout=12)
        branch = cli.git(workspace, 'rev-parse', '--abbrev-ref', 'HEAD', timeout=12)
        dirty = bool(cli.git(workspace, '--no-optional-locks', 'status', '--porcelain',
                             '--untracked-files=all', timeout=12))
        report['worktree'] = {'path':str(workspace), 'head':head, 'branch':branch, 'dirty':dirty}
    except (OSError, cli.WorkbenchError):
        head, dirty = None, None
        report['worktree'] = {'path':str(workspace), 'error':'worktree could not be read'}

    published = state.get('published_pr')
    merged = state.get('delivery')
    if isinstance(published, dict) and published.get('head') == candidate:
        report['pr'] = _pr_identity(published, candidate)
    if (isinstance(merged, dict) and merged.get('merged') is True
            and merged.get('verified_head') == candidate):
        report['pr'] = _pr_identity(merged, candidate)
        if report['pr']:
            report['outcome'] = 'merge-recorded'
    if phase == 'complete':
        verification = state.get('verification')
        target = data.get('verification_target')
        if (isinstance(target, dict) and isinstance(verification, dict)
                and state.get('verification_target') == target and verification.get('verdict') == 'PASS'
                and verification.get('base') == data['base_commit'] == target.get('base')
                and verification.get('head') == candidate == target.get('head') == head and dirty is False
                and (verification.get('repo'), verification.get('pr')) == (target.get('repo'), target.get('pr'))
                and isinstance(verification.get('review'), str)
                and verification['review'] == report['bound_review_record']
                and any(item['path'] == verification['review'] and item['sha256'] == verification.get('review_sha256')
                        for item in report['evidence']['retained']) and not report['evidence']['invalid']):
            report['pr'] = _pr_identity(verification, candidate)
            if report['pr']:
                report['outcome'] = 'verification-recorded'
        if 'outcome' not in report and data.get('publish_only') is True and report.get('pr'):
            report['outcome'] = 'publication-recorded'
        report['execution'] = 'complete' if 'outcome' in report else 'needs-inspection'
        report['next'] = 'none' if 'outcome' in report else 'confirm-delivery-outcome'
        return report

    blockers = []
    if phase in INTERRUPTED:
        blockers.append(f'{phase} was interrupted; inspect retained changes before resuming')
    expected_head = data['base_commit'] if phase in BASELINE else candidate
    if not head or dirty or head != expected_head:
        blockers.append('worktree does not match a clean recorded resume commit')
    if phase not in BASELINE | CANDIDATE | INTERRUPTED:
        blockers.append('phase is unknown')
    if report['evidence']['invalid']:
        blockers.append('retained evidence is missing, changed, or outside the task')
    if phase in {'delivery', 'close-issue'} and not report['bound_review_record']:
        blockers.append('candidate independent review evidence is unavailable')
    try:
        input_stage = start._read_input_stage(task)
        if ((phase != 'plan' and (input_stage['phase'] != 'ready' or not state.get('issue_digest')))
                or (state.get('issue_digest') and state['issue_digest'] != input_stage.get('request_digest'))
                or ('input_digest' in state and state['input_digest'] != input_stage.get('input_digest'))
                or (phase != 'plan' and 'input_digest' not in state and input_stage.get('input_digest') is not None)):
            blockers.append('workflow state does not match the original task input')
    except (autopilot.AutopilotError, start.StartError, cli.WorkbenchError, OSError, ValueError, KeyError) as error:
        blockers.append(f'original input check failed: {error}')
    blockers.extend(cli.enforce_budget(data))
    if data['status'] != 'paused':
        blockers.append('record says running but no cooperating workbench lock was held')

    report['blockers'] = blockers
    report['execution'] = 'needs-inspection' if blockers else 'paused'
    report['next'] = 'inspect-retained-state' if blockers else 'resume-from-' + phase
    return report


def inspect(task_path, *, since=None, check_pr=False, github=None):
    task = Path(task_path).expanduser().resolve(strict=True)
    with observe_task_lock(task) as lock:
        report = _inspect(task, lock, since)
        try:
            control = run_control.read(task)
        except (OSError, run_control.ControlError) as error:
            report.update(execution='needs-inspection', next='inspect-operator-hold',
                          operator_hold_error=str(error))
        else:
            if control:
                report['operator_hold'] = control
                if control['status'] == 'held':
                    if lock == 'held':
                        report['next'] = 'wait-for-admitted-step'
                    elif report['execution'] == 'paused':
                        report.update(execution='held', next='release-operator-hold-before-resume')
    if check_pr and report.get('pr'):
        pr = report['pr']
        try:
            observed = (github or delivery.GitHub()).pull(pr['repo'], pr['number'])
            if observed.get('number') != pr['number'] or observed.get('state') not in ('OPEN', 'CLOSED', 'MERGED'):
                raise delivery.DeliveryError('GitHub did not confirm the requested PR')
            same_head = observed.get('headRefOid') == pr['candidate']
            pr.update(state=observed['state'], observed_at=cli.now(), head_matches=same_head,
                      head=observed.get('headRefOid'), checks=_checks(observed.get('statusCheckRollup')))
            if report.get('outcome') == 'verification-recorded':
                pr['base_matches'] = observed.get('baseRefOid') == report['base']
            if (not same_head or pr.get('base_matches') is False
                    or (report.get('outcome') == 'merge-recorded' and observed['state'] != 'MERGED')):
                report['execution'] = 'needs-inspection'
                report['next'] = 'reconcile-pr-with-recorded-candidate'
        except (delivery.DeliveryError, OSError, ValueError, AttributeError):
            pr['state'] = 'unknown'
            pr['error'] = 'remote PR state could not be confirmed'
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--since', help='Only show events after this ISO8601 timestamp')
    parser.add_argument('--check-pr', action='store_true', help='Read current GitHub state for the recorded PR')
    args = parser.parse_args(argv)
    try:
        report = inspect(args.task, since=args.since, check_pr=args.check_pr)
    except (OSError, ValueError, KeyError, cli.WorkbenchError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
