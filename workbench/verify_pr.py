"""Independently verify an existing GitHub PR in a new iBoard worktree."""

import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import re
import sys
import time

from workbench import cli, delivery, pr_watch, start


class VerifyError(Exception):
    pass


def _bounded_json(path, limit):
    path = Path(path).expanduser().resolve(strict=True)
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise VerifyError('verification input exceeds its size limit')
    return json.loads(raw)


def _criteria(criteria):
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 20:
        raise VerifyError('supply between 1 and 20 original acceptance criteria')
    seen = set()
    for item in criteria:
        if not isinstance(item, dict) or set(item) != {'id', 'text'}:
            raise VerifyError('criteria require id and text')
        ident, text = item['id'], item['text']
        if (not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', ident)
                or ident in seen or not isinstance(text, str) or not text.strip()
                or len(text) > 4000 or '\0' in text):
            raise VerifyError('invalid or duplicate acceptance criterion')
        seen.add(ident)
    return criteria


def _pair(reader, repo, number):
    pull = pr_watch._facts(reader, repo, number)
    return {key:pull[source] for key, source in
            (('head', 'headRefOid'), ('base', 'baseRefOid'), ('url', 'url'), ('state', 'state'))}


def verify(*, task_id, repo, number, checkout, request, criteria, kind, surface, image, tasks_root,
           workspaces_root, seconds, memory_mb=2048, source_mode='workspace', candidate_image=None,
           build_source_image=False, expected_head=None, expected_base=None, plan=None, reader=None,
           required_ci_workflows=None, local_verification=None,
           launch=start.start):
    if (not isinstance(repo, str) or not pr_watch.REPO.fullmatch(repo)
            or type(number) is not int or not 1 <= number <= 2_147_483_647
            or type(seconds) is not int or not 1 <= seconds <= 86_400
            or any(value is not None and (not isinstance(value, str) or not pr_watch.SHA.fullmatch(value))
                   for value in (expected_head, expected_base))):
        raise VerifyError('select an explicit GitHub PR, optional commit pins and positive time limit')
    _criteria(criteria)
    if plan is not None and (not isinstance(plan, dict) or plan.get('status') != 'ready'
                             or len(json.dumps(plan).encode('utf-8')) > 64_000):
        raise VerifyError('frozen verification plan must be a bounded ready object')
    deadline = time.monotonic() + seconds
    reader = reader or pr_watch.GitHubReader()
    if isinstance(reader, pr_watch.GitHubReader):
        reader.set_deadline(deadline)
    pair = _pair(reader, repo, number)
    head, base = pair['head'], pair['base']
    if (pair['state'] != 'OPEN' or head == base or (expected_head and expected_head != head)
            or (expected_base and expected_base != base)):
        raise VerifyError('PR is not open at the requested head and base')
    checkout = Path(checkout).expanduser().resolve(strict=True)
    if (cli.sha(checkout, base, timeout=20) != base or cli.sha(checkout, head, timeout=20) != head
            or cli.git(checkout, 'merge-base', base, head, timeout=20) != base):
        raise VerifyError('local checkout must contain the exact PR and an ancestor base')
    remaining = int(deadline - time.monotonic())
    if remaining < 1:
        raise VerifyError('verification budget exhausted before task creation')
    target = {'repo':repo, 'pr':number, 'head':head, 'base':base}
    if plan is not None:
        target['plan'] = plan
    state = launch(task_id=task_id, repo=checkout, issue_file=request,
                   source=f'https://github.com/{repo}/pull/{number}', kind=kind, surface=surface,
                   iterations=1, seconds=remaining, image=image, pr_repo=repo, tasks_root=tasks_root,
                   workspaces_root=workspaces_root, memory_mb=memory_mb, source_mode=source_mode,
                   candidate_image=candidate_image, build_source_image=build_source_image,
                   required_ci_workflows=required_ci_workflows, local_verification=local_verification,
                   criteria=criteria, verification_target=target, implementer='pr-contributor')
    receipt = state.get('verification')
    if (state.get('phase') != 'complete' or not isinstance(receipt, dict) or receipt.get('verdict') != 'PASS'
            or any(receipt.get(key) != value for key, value in target.items() if key != 'plan')):
        raise VerifyError('existing PR verification did not produce a matching independent receipt')
    report = {**receipt, 'task':str(Path(tasks_root).expanduser().resolve() / task_id / 'task.json')}
    try:
        current = _pair(reader, repo, number)
    except (delivery.DeliveryError, pr_watch.WatchError, OSError):
        return {**report, 'status':'remote-unconfirmed'}
    if current != pair:
        return {**report, 'status':'stale', 'current':current}
    return {**report, 'status':'verified', 'observed_at':cli.now(), 'url':current['url']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('id', 'repo', 'checkout', 'request', 'criteria', 'image', 'tasks-root', 'workspaces-root'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--pr', type=int, required=True)
    parser.add_argument('--kind', choices=('bug', 'feature'), required=True)
    parser.add_argument('--surface', choices=('ui', 'non-ui'), required=True)
    parser.add_argument('--seconds', type=int, required=True)
    parser.add_argument('--memory-mb', type=int, default=2048)
    parser.add_argument('--source-mode', choices=('workspace', 'image'), default='workspace')
    parser.add_argument('--candidate-image')
    parser.add_argument('--build-source-image', action='store_true')
    parser.add_argument('--head')
    parser.add_argument('--base')
    parser.add_argument('--plan', help='Optional frozen verification plan in JSON')
    args = parser.parse_args(argv)
    try:
        with redirect_stdout(sys.stderr):
            result = verify(task_id=args.id, repo=args.repo, number=args.pr, checkout=args.checkout,
                            request=args.request, criteria=_bounded_json(args.criteria, 100_000), kind=args.kind,
                            surface=args.surface, image=args.image, tasks_root=args.tasks_root,
                            workspaces_root=args.workspaces_root, seconds=args.seconds, memory_mb=args.memory_mb,
                            source_mode=args.source_mode, candidate_image=args.candidate_image,
                            build_source_image=args.build_source_image, expected_head=args.head, expected_base=args.base,
                            plan=_bounded_json(args.plan, 64_000) if args.plan else None)
    except (OSError, ValueError, VerifyError, cli.WorkbenchError, start.StartError,
            delivery.DeliveryError, pr_watch.WatchError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'verified' else 1


if __name__ == '__main__':
    sys.exit(main())
