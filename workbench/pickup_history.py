"""Recover a reviewable continuation point from a selected branch, PR, or recall handoff."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from workbench import cli, delivery, pickup, recall
from workbench.automate_me import ERRORS, _file, _keep, _read, _require


REF = re.compile(r'refs/(?:heads|remotes)/[A-Za-z0-9][A-Za-z0-9_./-]{0,119}')
ROUTES = {'bug': 'bug-fix', 'feature': 'feature', 'refactor': 'refactoring'}


def _git(repo, *args):
    return cli.git(repo, *args, timeout=15)


def _root(repo):
    return Path(_git(repo, 'rev-parse', '--show-toplevel')).resolve()


def _identity(repo):
    return Path(_git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()


def _name(repo, name):
    _require(isinstance(name, str) and recall.BRANCH.fullmatch(name), 'invalid branch name')
    _git(repo, 'check-ref-format', f'refs/heads/{name}')
    return name


def _ref(repo, value):
    if pickup._sha(value):
        return cli.sha(repo, value, timeout=15)
    if not REF.fullmatch(value):
        value = 'refs/heads/' + _name(repo, value)
    else:
        _git(repo, 'check-ref-format', value)
    return cli.sha(repo, value, timeout=15)


def _pr(github, forge, number):
    entry = github.pull(forge, number)
    _require(entry.get('number') == number and entry.get('state') in {'OPEN', 'MERGED', 'CLOSED'}
             and pickup._sha(entry.get('headRefOid')) and pickup._sha(entry.get('baseRefOid')),
             'GitHub did not confirm the PR and both commits')
    return {'repo': forge, 'number': number, 'state': entry['state'], 'draft': entry.get('isDraft') is True,
            'head': entry['headRefOid'], 'base': entry['baseRefOid'], 'branch': entry.get('headRefName'),
            'checks': pickup._checks(entry.get('statusCheckRollup')),
            'url': f'https://github.com/{forge}/pull/{number}'}


def _checked_out(repo, branch, head):
    if not branch:
        return None
    records = _git(repo, 'worktree', 'list', '--porcelain', '-z')
    for record in records.split('\0\0'):
        fields = dict(field.partition(' ')[::2] for field in record.split('\0') if field)
        if fields.get('branch') != f'refs/heads/{branch}':
            continue
        path = Path(fields['worktree'])
        _require(fields['HEAD'] == head and cli.sha(path, 'HEAD', timeout=15) == head,
                 'checked-out branch changed during the observation')
        status = _git(path, '--no-optional-locks', 'status', '--porcelain', '--untracked-files=normal')
        return {'path': str(path), 'head': head, 'dirty': bool(status)}
    return None


def _delta(repo, base, head):
    left, right = map(int, _git(repo, 'rev-list', '--left-right', '--count', f'{base}...{head}').split())
    logs = []
    for row in _git(repo, 'log', '-30', '--format=%H%x09%s', f'{base}..{head}', '--').splitlines():
        sha, subject = row.split('\t', 1)
        logs.append({'sha': sha, 'subject': recall._excerpt(subject)})
    try:
        start = _git(repo, 'merge-base', base, head)
    except cli.WorkbenchError:
        start = None
    paths = _git(repo, '-c', 'core.quotePath=true', 'diff', '--name-status', '--no-renames',
                 '--no-ext-diff', '--no-textconv', start or base, head, '--').splitlines()
    return {'base_only': left, 'candidate_only': right, 'commits': logs, 'commits_truncated': right > len(logs),
            'merge_base': start, 'changed_paths': paths[:80], 'changed_paths_truncated': len(paths) > 80}


def _history(path, digest, repo):
    report = json.loads(_read(path, 250_000, digest))
    _require(report['schema'] == cli.SCHEMA and report['kind'] == 'recall-report', 'choose a recall report')
    source = report['evidence']
    evidence = json.loads(_read(source['path'], 480_000, source['sha256']))
    _require(evidence['kind'] == 'recall-evidence' and Path(evidence['repo']).resolve() == repo,
             'the recall report belongs to a different workspace')
    actual = report['live']
    trail = {'path': str(_file(path)), 'sha256': digest, 'evidence': source, 'scope': report['scope'],
             'reported_at': actual['at'], 'observed_head': actual['commit'], 'prior_artifacts': actual['facts'],
             'prior_summary': report['report'], 'citations': report['citations']}
    return trail, evidence


def _recorded_target(evidence):
    prs = [int(value[3:]) for value in evidence['artifacts'] if re.fullmatch(r'pr:[1-9][0-9]*', value)]
    branches = [value[7:] for value in evidence['artifacts'] if value.startswith('branch:')]
    if len(prs) == 1 and evidence['forge']:
        return None, prs[0]
    if not prs and len(branches) == 1:
        return branches[0], None
    raise ValueError('choose one of the branches or PRs mentioned in the selected history')


def _retained(repo, path, base, head, pr, github):
    path = _file(path)
    data = cli.read(path)
    _require(_identity(Path(data['repo'])) == _identity(repo) and data['base_commit'] == base,
             'the retained task belongs to a different repository or base')
    retained = pickup.inspect(path, check_pr=False, github=github)
    if retained['lock'] == 'held':
        return retained, data
    chosen = retained.get('candidate') or retained.get('worktree', {}).get('head')
    _require(chosen == head, 'the retained task does not describe the selected head')
    if pr and retained.get('pr'):
        recorded = retained['pr']
        _require((recorded['repo'].casefold(), recorded['number']) == (pr['repo'].casefold(), pr['number']),
                 'the retained task refers to a different PR')
    retained['path'] = str(path)
    return retained, data


def _route(context, kind):
    task, pr, worktree, delta = (context.get(key) for key in ('task', 'pr', 'worktree', 'delta'))
    if task and task['lock'] == 'held':
        return 'wait', 'pickup', task['next']
    if task and task['execution'] == 'held':
        return 'wait', 'pickup', task['next']
    task_worktree = task.get('worktree', {}) if task else {}
    if (worktree and worktree['dirty']) or task_worktree.get('dirty'):
        return 'inspect', 'interrogate', 'inspect-uncommitted-work-with-the-inherited-checkpoint'
    if task and (task_worktree.get('head') != context['head'] or task['evidence']['invalid']):
        return 'inspect', 'pickup', 'reconcile-the-retained-worktree-and-evidence'
    if pr and pr['state'] == 'CLOSED':
        return 'postmortem', 'investigation', 'check-why-the-pr-closed-and-which-criteria-remain'
    if task and task['execution'] == 'needs-inspection':
        return 'inspect', 'pickup', task['next']
    if pr and pr['state'] == 'MERGED':
        return 'ratify', 'interrogate', 'compare-landed-behavior-with-the-original-goal'
    if context['pr_base_changed']:
        return 'inspect', 'shipping', 'reconcile-the-retained-base-with-the-current-pr-base'
    if not delta['merge_base']:
        return 'inspect', 'investigation', 'confirm-the-selected-base-and-candidate-history'
    if pr and delta['base_only']:
        return 'inspect', 'shipping', 'reconcile-the-candidate-with-the-current-pr-base'
    if task and task['execution'] == 'paused':
        return 'continue', ROUTES.get(kind, 'pickup'), task['next']
    if task and task.get('bound_review_record') and task['execution'] == 'complete':
        return 'handoff', 'verify-pr' if pr else 'interrogate', 'reuse-the-exact-head-review-and-reconcile-current-delivery'
    if delta['candidate_only'] == 0:
        return 'ratify', 'interrogate', 'compare-landed-behavior-with-the-original-goal'
    return 'continue', 'verify-pr' if pr else 'interrogate', 'compare-the-inherited-candidate-with-the-original-goal'


def inspect(repo, *, base=None, branch=None, forge=None, pr=None, remote_branch=None, task=None,
            recall_report=None, recall_sha256=None, goal=None, out=None, github=None):
    repo = Path(repo).expanduser().resolve(strict=True)
    _require(_root(repo) == repo, 'choose the repository worktree root')
    _require(bool(recall_report) == bool(recall_sha256), 'provide the recall report and its digest together')
    history, source = _history(recall_report, recall_sha256, repo) if recall_report else (None, {})
    inferred = False
    if source and not (branch or pr or remote_branch):
        branch, pr = _recorded_target(source)
        if pr:
            forge = forge or source['forge']
        inferred = True
    _require(bool(branch or pr or remote_branch), 'choose a local branch, a pushed branch, or a PR')
    _require(not (pr and remote_branch), 'choose either a PR or a pushed branch')
    _require((not (pr or remote_branch) and not forge) or (
        isinstance(forge, str) and re.fullmatch(recall.REPO_NAME, forge)
        and all(part not in {'.', '..'} for part in forge.split('/'))), 'expected a GitHub owner/repository')
    _require(pr is None or (type(pr) is int and pr > 0), 'choose a positive PR number')
    if forge and source.get('forge'):
        _require(forge.casefold() == source['forge'].casefold(), 'the recall report refers to another GitHub repository')
    _require(goal is None or (isinstance(goal, str) and 1 <= len(goal.strip()) <= 1200),
             'provide a concise original goal')
    if branch:
        branch = _name(repo, branch)
    if remote_branch:
        remote_branch = _name(repo, remote_branch)
    github = github or delivery.GitHub()

    def observe():
        if pr:
            row = _pr(github, forge, pr)
            return row['head'], row['base'], row
        if remote_branch:
            head = github.branch(forge, remote_branch)
            _require(pickup._sha(head), 'GitHub did not confirm the pushed head')
            return head, None, None
        return _ref(repo, branch), None, None

    checked_at = cli.now()
    head, observed_base, remote_pr = observe()
    _require(not branch or _ref(repo, branch) == head, 'local and selected remote heads differ')
    if not branch and (hint := remote_pr['branch'] if remote_pr else remote_branch):
        try:
            if _ref(repo, _name(repo, hint)) == head:
                branch = hint
        except (ValueError, cli.WorkbenchError):
            pass
    task_base = cli.read(_file(task))['base_commit'] if task and not base else None
    anchor = _ref(repo, base or task_base) if (base or task_base) else observed_base
    _require(anchor is not None, 'select the original base commit or branch')
    try:
        cli.sha(repo, head, timeout=15)
        cli.sha(repo, anchor, timeout=15)
    except cli.WorkbenchError as error:
        raise ValueError('fetch the exact observed commits into the selected repository') from error
    worktree = _checked_out(repo, branch, head)
    delta = _delta(repo, anchor, head)
    retained, task_data = _retained(repo, task, anchor, head, remote_pr, github) if task else (None, {})
    if history:
        history.update(observed_head_matches=history['observed_head'] == head, selected_artifact=inferred)
    context = {'schema': cli.SCHEMA, 'kind': 'session-pickup', 'observed_at': checked_at, 'repo': str(repo),
               'base': anchor, 'head': head, 'branch': branch, 'remote_branch': remote_branch, 'forge': forge,
               'pr': remote_pr, 'worktree': worktree, 'delta': delta, 'task': retained, 'history': history,
               'goal': goal or task_data.get('original_source', task_data.get('source')), 'checks_rerun': [],
               'pr_base_changed': observed_base is not None and anchor != observed_base}
    last_head, last_base, last_pr = observe()
    _require((head, observed_base) == (last_head, last_base)
             and (remote_pr is None or remote_pr['state'] == last_pr['state'])
             and (not branch or _ref(repo, branch) == head) and (not base or _ref(repo, base) == anchor)
             and worktree == _checked_out(repo, branch, head), 'the selected state changed during observation')
    if last_pr:
        context['pr'] = last_pr
    verdict, playbook, next_action = _route(context, task_data.get('kind'))
    context.update(verdict=verdict, playbook=playbook, next=next_action)
    if out:
        context['receipt'] = _keep(out, context)
    return context


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    for name in ('base', 'branch', 'forge', 'remote-branch', 'task', 'recall-report', 'recall-sha256', 'goal', 'out'):
        parser.add_argument('--' + name)
    parser.add_argument('--pr', type=int)
    try:
        result = inspect(**vars(parser.parse_args(argv)))
    except ERRORS + (delivery.DeliveryError,) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
