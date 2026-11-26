"""Publish and land the verified phases of an approved dispatch in order."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from workbench import cli, delivery, phase_contract, phase_dispatch, pr_watch, publish, stack_rebase, stack_ship, workflow
from workbench.task_lock import task_lock


class PhaseShippingError(ValueError):
    pass


ERRORS = (*phase_dispatch.ERRORS, pr_watch.WatchError, publish.PublishError, stack_ship.ShippingError,
          stack_rebase.RebaseError)


class GitHub(publish.Publisher, stack_ship.GitHub):
    def exact_landing(self, repo, member, trunk):
        """Confirm the exact merge commit, tree and parents, including a squash."""
        raw = self._call('api', f"repos/{repo}/pulls/{member['number']}")
        if (raw.get('merged') is not True or raw.get('merge_commit_sha') != trunk
                or raw.get('head', {}).get('sha') != member['head'] or raw.get('base', {}).get('ref') != 'main'):
            return False
        candidate = self._call('api', f"repos/{repo}/git/commits/{member['head']}")
        landing = self._call('api', f'repos/{repo}/git/commits/{trunk}')
        tree = candidate.get('tree', {}).get('sha')
        return (candidate.get('sha') == member['head'] and landing.get('sha') == trunk
                and isinstance(tree, str) and pr_watch.SHA.fullmatch(tree) is not None
                and landing.get('tree', {}).get('sha') == tree
                and [entry.get('sha') for entry in landing.get('parents', [])] in
                ([member['base']], [member['base'], member['head']]))


def _require(condition, reason):
    if not condition:
        raise PhaseShippingError(reason)


def _regular(path):
    _require(path.is_file() and not path.is_symlink(), f'missing or unsafe phase record: {path.name}')
    return path


def _proof(task, contract, entry, parent, baseline_perf, *, published=False):
    data = phase_dispatch._bound_task(task, contract, entry, parent, baseline_perf)
    verified = phase_dispatch._verified(task, data, entry)
    _require(verified['status'] in ('verified', 'awaiting-operator-review'), 'local phase proof is incomplete')
    if published:
        delivery._independent_verification(data, verified['commit'], destination=contract['destination'])
    return data, verified


def _recheck_replay(task, data, candidate, original):
    if str(task) == original['task']:
        _require(candidate == original['commit'], 'phase candidate differs from the verified original dispatch')
        return
    context = phase_contract.read(data)
    replay = context.get('replay') if context else None
    expected = {'source_task': original['task'], 'source_result': original['result'],
                'source_review': original['independent_review']}
    _require(isinstance(replay, dict) and all(replay.get(key) == value for key, value in expected.items())
             and context.get('replayed_candidate') == candidate,
             'candidate does not bind to the exact replay of the approved original phase')


@contextmanager
def _active(task):
    data = cli.read(task)
    status = data['status']
    _require(status in ('paused', 'running') and not cli.enforce_budget(data),
             'phase task is inactive or outside its approved budget')
    if status == 'paused':
        data['status'] = 'running'
        cli.write(task, data)
    try:
        yield
    finally:
        if status == 'paused':
            latest = cli.read(task)
            latest['status'] = 'paused'
            cli.write(task, latest)


def _member(step):
    return {**step['member'], 'number': step['number']}


def _observe(github, repo, member, *, state):
    latest = stack_ship._observe(github, repo, member)
    _require(latest['state'] == state and latest['baseRefName'] == 'main' and not latest['isDraft'],
             'the verified PR lifecycle or target branch changed')
    if state == 'OPEN':
        _require(latest['baseRefOid'] == member['base'], 'the PR base changed after verification')
    return latest


def _landing(github, repo, member, observed):
    _observe(github, repo, member, state='MERGED')
    exact = getattr(github, 'exact_landing', None)
    _require(isinstance(observed, str) and pr_watch.SHA.fullmatch(observed) is not None
             and (observed == member['head'] or exact and exact(repo, member, observed)),
             'main is not the exact fast-forward, merge or squash of the verified PR')
    return observed


def _ready_for_delivery(github, repo, member, data):
    if not data.get('local_gate'):
        return stack_ship._ready(github, repo, member)
    delivery.require_actions_disabled(github, repo)
    report = pr_watch.snapshot(github, repo, member['number'], 'threads-only', member['head'], member['base'])
    if (report['status'] != 'threads-clear' or report['base_branch'] != 'main'
            or report['head_branch'] != member['head_branch'] or report['draft'] or report['cross_repository']
            or report['mergeability'] != 'MERGEABLE' or report['merge_state'] not in ('CLEAN', 'UNSTABLE')
            or report['review_decision'] in ('REVIEW_REQUIRED', 'CHANGES_REQUESTED')):
        raise stack_ship.ShippingError('local phase publication is not ready: ' + report['reason'])


def _default_replay(task, base, output, *, seconds):
    from workbench import phase_reverify

    return phase_reverify.run(task, base, output, seconds=seconds)


def _fetch_landing(github, checkout, repo, head, remaining):
    def has_commit():
        return not stack_rebase._git(checkout, 'cat-file', '-e', head + '^{commit}', seconds=remaining()).returncode

    if has_commit():
        return
    _require(github.branch(repo, 'main') == head, 'main changed before fetching its exact landing')
    stack_rebase._output(stack_rebase._git(checkout, 'fetch', '--no-tags', '--no-write-fetch-head',
                                          f'https://github.com/{repo}.git', 'refs/heads/main',
                                          seconds=min(60, remaining())))
    _require(has_commit() and github.branch(repo, 'main') == head,
             'the exact managed main commit is unavailable after fetch')


def _run(root, contract, workflows, github, replay, publisher, merger):
    repo = contract['destination']
    dispatch_path = _regular(root / 'run.json')
    dispatch_digest = cli.digest(dispatch_path)
    dispatch = cli.read(dispatch_path)
    _require(dispatch.get('manifest_sha256') == contract['manifest_sha256'],
             'local dispatch does not bind to the approved manifest')
    started = datetime.fromisoformat(dispatch['started_at'])
    _require(started.tzinfo is not None and started <= datetime.now(timezone.utc), 'invalid dispatch start time')
    deadline = time.monotonic() + (started.timestamp() + contract['seconds'] - time.time())
    journal_path = root / 'shipping.json'
    _require(not journal_path.is_symlink(), 'shipping journal cannot be a symbolic link')
    identity = {'manifest_sha256': contract['manifest_sha256'], 'destination': repo,
                'started_at': dispatch['started_at'], 'ci_workflows': workflows}
    journal = cli.read(journal_path) if journal_path.exists() else {
        'schema': cli.SCHEMA, **identity, 'steps': {}, 'events': []}
    _require(all(journal.get(key) == value for key, value in identity.items()),
             'shipping authorization, dispatch deadline or workflow policy changed')
    cli.write(journal_path, journal)

    def remaining():
        seconds = int(deadline - time.monotonic())
        _require(seconds >= 1, 'approved dispatch time limit reached')
        _require(cli.digest(contract['manifest_path']) == contract['manifest_sha256']
                 and cli.digest(contract['plan_path']) == contract['plan_sha256']
                 and cli.digest(dispatch_path) == dispatch_digest,
                 'the approved dispatch or plan changed before shipping')
        if isinstance(github, GitHub):
            github.set_deadline(deadline)
        return min(seconds, 86400)

    def save(ident, state, **details):
        journal['steps'][ident].update(state=state, **details)
        journal['events'].append({'phase': ident, 'state': state, 'at': cli.now(), **details})
        cli.write(journal_path, journal)

    landed, originals = {}, {}
    entries = {entry['id']: entry for entry in contract['plan']['phases']}
    main = contract['plan']['baseline']

    def finish(status, ident=None, reason=None):
        return {'status': status, 'destination': repo, 'landed': list(landed.values()), 'phase': ident,
                'reason': reason, 'journal': str(journal_path)}

    remaining()
    stack_ship._scope(github, repo)

    def recheck_landings():
        for name in landed:
            record = journal['steps'][name]
            previous_task = _regular(Path(record['task']))
            previous_member = _member(record)
            previous_perf = dispatch['phases'][name]['baseline_perf']
            with task_lock(previous_task):
                data, proof = _proof(previous_task, contract, entries[name], previous_member['base'],
                                     previous_perf, published=True)
                _recheck_replay(previous_task, data, proof['commit'], originals[name])
            _require(proof == record['proof'], f'landed phase {name} evidence changed before advancement')

    for ident, entry in entries.items():
        step = journal['steps'].setdefault(ident, {})
        try:
            remaining()
            retained = cli.read(dispatch_path)
            _require(retained.get('manifest_sha256') == identity['manifest_sha256']
                     and retained.get('started_at') == identity['started_at'], 'local dispatch identity changed')
            row = retained['phases'].get(ident, {})
            if not row.get('result', {}).get('commit'):
                return finish('waiting-for-verification', ident, 'the local phase is not independently verified')
            _require(all(name in landed for name in entry['depends_on']), 'declared phase dependencies have not landed')
            original_task = root / 'tasks' / phase_dispatch._task_id(root, ident) / 'task.json'
            original_parent = (contract['plan']['baseline'] if entry['base'] == 'trunk'
                               else originals[entry['base']]['commit'])
            perf = row['baseline_perf']

            def original_proof():
                _regular(original_task)
                with task_lock(original_task):
                    _, proof = _proof(original_task, contract, entry, original_parent, perf)
                retained_result = row['result']
                _require(all(proof.get(key) == retained_result.get(key)
                             for key in ('task', 'commit', 'result', 'independent_review')),
                         'original phase proof differs from the dispatch journal')
                return proof

            original = original_proof()
            originals[ident] = original
            if original['status'] != 'verified':
                return finish('awaiting-operator-review', ident, 'the original phase needs exact operator approval')
            replays = root / 'shipping' / phase_dispatch._task_id(root, ident) / main
            _require(replays.resolve().is_relative_to(root), 'replay evidence directory escapes the dispatch root')

            def recorded_target(member):
                saved_task = Path(step['task']).resolve(strict=True)
                _require(member['base'] == main
                         and (saved_task == original_task if original_parent == main else saved_task.is_relative_to(replays)),
                         'retained phase landing order or task changed')
                with task_lock(_regular(saved_task)):
                    data, proof = _proof(saved_task, contract, entry, main, perf, published=True)
                    _recheck_replay(saved_task, data, proof['commit'], original)
                _require(proof['commit'] == member['head'] and proof == step['proof']
                         and member['head_branch'] == f"workbench/{data['id']}" and member.get('ci_workflows') == workflows
                         and original['result'] == step['original_result']
                         and original['independent_review'] == step['original_review'],
                         'retained write no longer matches its approved phase evidence')

            if step.get('state') == 'landed':
                member = _member(step)
                recorded_target(member)
                main = _landing(github, repo, member, step['main'])
                landed[ident] = {'phase': ident, 'pr': member['number'], 'head': member['head'], 'main': main}
                continue
            observed = github.branch(repo, 'main')
            if step.get('state') == 'merge-intent':
                member = _member(step)
                recorded_target(member)
                pull = stack_ship._observe(github, repo, member)
                if pull['state'] != 'MERGED':
                    return finish('awaiting-confirmation', ident, 'merge outcome has not been confirmed')
                main = _landing(github, repo, member, observed)
                save(ident, 'landed', main=main)
                landed[ident] = {'phase': ident, 'pr': member['number'], 'head': member['head'], 'main': main}
                continue
            _require(observed == main, 'main differs from the approved trunk or last managed landing')
            task = original_task
            if original_parent != main:
                recheck_landings()
                _fetch_landing(github, contract['repo'], repo, main, remaining)
                replayed = replay(original_task, main, replays, seconds=remaining())
                if replayed.get('status') == 'awaiting-operator-review':
                    return finish('awaiting-operator-review', ident, f"replayed candidate needs approval: {replayed['task']}")
                if replayed.get('status') != 'verified':
                    return finish('blocked', ident, replayed.get('reason', 'fresh replay verification is unavailable'))
                task = Path(replayed['task']).resolve(strict=True)
                _require(task.is_relative_to(replays), 'replay returned a task outside its allocated evidence directory')

            with task_lock(_regular(task)):
                def target_proof():
                    data, proof = _proof(task, contract, entry, main, perf, published=True)
                    candidate = original['commit'] if task == original_task else replayed['candidate']
                    _require(proof['commit'] == candidate, 'phase candidate differs from verified dispatch or replay')
                    _recheck_replay(task, data, candidate, original)
                    return data, proof

                data, proof = target_proof()
                member = {'head': proof['commit'], 'base': main, 'head_branch': f"workbench/{data['id']}",
                          'base_branch': 'main', 'ci_workflows': workflows}
                snapshot = {'task': str(task), 'member': member, 'proof': proof,
                            'original_result': original['result'], 'original_review': original['independent_review']}
                if step:
                    _require(all(step.get(key) == value for key, value in snapshot.items()),
                             'shipping proof differs from the retained write intent')
                delivery._merge_workflows(data, workflows)

                def before_write():
                    remaining()
                    recheck_landings()
                    _require(original_proof() == original and target_proof()[1] == proof,
                             'phase evidence changed before GitHub write')
                    _require(github.branch(repo, 'main') == main, 'main moved before GitHub write')

                with _active(task):
                    if not step:
                        before_write()
                        # Refuse to adopt a pre-existing matching branch or PR outside this run.
                        _require(github.remote_head(repo, member['head_branch']) is None
                                 and not github.pulls(repo, member['head_branch']),
                                 'phase branch or pull request existed before shipping')
                        save(ident, 'publish-intent', **snapshot)
                        published = publisher(task, repo, github=github)
                        number = published.get('pr')
                        _require(published.get('repo') == repo and published.get('head') == member['head']
                                 and type(number) is int and number >= 1, 'publisher returned a different PR')
                    elif step['state'] == 'publish-intent':
                        choices = github.pulls(repo, member['head_branch'])
                        if not choices:
                            return finish('awaiting-confirmation', ident, 'publication outcome has not been confirmed')
                        _require(len(choices) == 1, 'publication could not be tied to one pull request')
                        number = choices[0]['number']
                    else:
                        _require(step['state'] == 'published', 'unexpected phase shipping state')
                        number = step['number']
                    before_write()
                    pinned = {**member, 'number': number}
                    _observe(github, repo, pinned, state='OPEN')
                    if step['state'] != 'published':
                        save(ident, 'published', number=number)
                    try:
                        _ready_for_delivery(github, repo, pinned, data)
                    except stack_ship.ShippingError as error:
                        return finish('awaiting-gates', ident, str(error))

                    def before_merge():
                        before_write()
                        _observe(github, repo, pinned, state='OPEN')
                        save(ident, 'merge-intent', number=number)

                    receipt = merger(task, repo, number, github, stack_binding=pinned,
                                     revalidate=before_merge, deadline=deadline)
                    _require(step['state'] == 'merge-intent'
                             and receipt.get('merged') is True and receipt.get('verified_head') == member['head'],
                             'merge outcome has not been confirmed')
                    main = _landing(github, repo, pinned, github.branch(repo, 'main'))
                    save(ident, 'landed', main=main)
                    landed[ident] = {'phase': ident, 'pr': number, 'head': member['head'], 'main': main}
        except ERRORS as error:
            pending = step.get('state') in ('publish-intent', 'merge-intent')
            return finish('awaiting-confirmation' if pending else 'blocked', ident, str(error))
    if github.branch(repo, 'main') != main:
        return finish('blocked', reason='main changed after the last managed phase')
    return finish('complete')


def run(manifest, expected_sha256, dispatch_root, *, ci_workflows=None, github=None, replay=None,
        publisher=publish.publish, merger=delivery._guarded_merge):
    """Ship only to the frozen personal fork following current independent evidence."""
    contract = phase_contract.load(manifest, expected_sha256)
    _require(contract['destination'], 'the approved dispatch has no GitHub destination')
    root = Path(dispatch_root).expanduser().resolve(strict=True)
    _require(not root.is_relative_to(Path(contract['repo'])), 'keep shipping state outside the source checkout')
    workflows = workflow.validate_required_ci_workflows(ci_workflows)
    with task_lock(_regular(root / '.dispatch'), lock_name='.phase-dispatch.lock'):
        return _run(root, contract, workflows, github or GitHub(), replay or _default_replay, publisher, merger)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'sha256', 'dispatch-root'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--ci-workflow', action='append', dest='ci_workflows')
    args = parser.parse_args(argv)
    try:
        result = run(args.manifest, args.sha256, args.dispatch_root, ci_workflows=args.ci_workflows)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result['status'] != 'complete')


if __name__ == '__main__':
    raise SystemExit(main())
