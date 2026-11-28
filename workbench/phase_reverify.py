"""Replay an exact verified phase and re-run its complete acceptance on a new base."""

import argparse
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
import json
import re

from workbench import autopilot, cli, phase_contract, phase_dispatch, phase_replay, start, workflow
from workbench.task_lock import task_lock


class ReverifyError(ValueError):
    pass


def _remaining(journal):
    elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(journal['started_at'])
    seconds = int(journal['seconds'] - elapsed.total_seconds())
    if seconds < 1:
        raise ReverifyError('phase replay time limit reached')
    return seconds


def _source(task):
    with task_lock(task):
        data = cli.read(task)
        context = phase_contract.read(data)
        if not context or context.get('replay') or context['task_path'] != task:
            raise ReverifyError('phase replay requires an original task with complete phase evidence')
        verified = phase_dispatch._verified(task, data, context['entry'])
        source = {'source_task': str(task), 'source_result': verified['result'],
                  'source_review': verified['independent_review']}
        return context, verified['commit'], source, data.get('local_gate')


def _run(root, original, new_base, *, seconds, prepare_replay, launch, resume, execute):
    phase, head, source, local_policy = _source(original)
    contract, entry, step = phase['contract'], phase['entry'], phase['step']
    repo = Path(contract['repo'])
    if root.is_relative_to(repo) or root.is_relative_to(original.parent):
        raise ReverifyError('keep replay results outside the source checkout and original task')
    baseline = contract['plan']['baseline']
    patch_input = {'trunk': baseline, 'old_base': phase['parent'], 'old_head': head,
                   'new_base': new_base, 'files': entry['files']}
    journal_path = root / 'run.json'
    binding = {'origin': source, 'phase': entry['id'], **patch_input}
    if local_policy:
        binding['local_gate'] = local_policy
    journal = cli.read(journal_path) if journal_path.exists() else {
        'schema': cli.SCHEMA, 'binding': binding, 'started_at': cli.now(), 'seconds': min(seconds, contract['seconds'])}
    if journal['binding'] != binding:
        raise ReverifyError('phase replay inputs changed after execution started')
    cli.write(journal_path, journal)
    task_id = phase_dispatch._task_id(root, f"replay-{entry['id']}")
    task = root / 'tasks' / task_id / 'task.json'
    report_path = root / 'result.json'

    def result(status, **detail):
        report = {'schema': cli.SCHEMA, 'status': status, 'phase': entry['id'], 'base': new_base,
                  'original_candidate': head, 'report': str(report_path), **detail}
        cli.write(report_path, report)
        return report

    try:
        if 'proof' not in journal:
            if task.exists():
                raise ReverifyError('retained verification task has no pinned replay proof')
            patch_report = root / 'patch' / 'result.json'
            if patch_report.is_symlink():
                raise ReverifyError('phase replay proof cannot be a symbolic link')
            if patch_report.is_file():
                replay = {**cli.read(patch_report), 'report': str(patch_report), 'sha256': cli.digest(patch_report)}
            else:
                replay = prepare_replay(repo, **patch_input, out=root / 'patch', timeout=_remaining(journal))
            if replay['status'] != 'replayed':
                return result('blocked', patch_report=replay['report'], reason=replay.get('reason', 'Git replay blocked'))
            journal['proof'] = {'path': replay['report'], 'sha256': replay['sha256']}
            cli.write(journal_path, journal)
        proof = phase_replay.validate(journal['proof'], repo=repo, **patch_input)
        replay_binding = {**source, 'proof': journal['proof']}
        candidate = proof['candidate']

        def retained():
            data = cli.read(task)
            current = phase_contract.read(data)
            if (not current or current['parent'] != new_base or current.get('replay') != replay_binding
                    or current.get('replayed_candidate') != candidate
                    or current.get('visual_baseline') != phase.get('visual_baseline')):
                raise ReverifyError('retained task does not match the approved replay')
            return data

        if task.exists():
            with task_lock(task):
                retained()
                state_path = task.with_name('autopilot.json')
                state = cli.read(state_path).get('phase') if state_path.exists() else None
                if state not in ('delivery', 'complete'):
                    _remaining(journal)
                    autopilot._pause(task, 'resuming the exact retained replay and its checks')
                    resume(task, run=execute)
        else:
            request = root / 'request.txt'
            raw_request = (original.parent / 'issue.txt').read_bytes()
            if request.is_symlink() or (request.exists() and request.read_bytes() != raw_request):
                raise ReverifyError('retained replay request changed')
            if not request.exists():
                request.write_bytes(raw_request)
                request.chmod(0o600)

            def drive(task_path, request_path, image, **options):
                data = cli.read(task_path)
                if local_policy:
                    from workbench import local_gate

                    try:
                        local_gate._select(data, candidate, local_policy['files'], local_policy['reason'])
                    except local_gate.LocalGateError as error:
                        raise ReverifyError('could not carry forward the authorized local test policy') from error
                phase_contract.bind(task_path, data, contract, entry['id'], new_base, request_path,
                                    phase['baseline_perf'], replay=replay_binding, visual_baseline=phase.get('visual_baseline'))
                cli.write(task_path, data)
                return execute(task_path, request_path, image, **options)

            launch(task_id=task_id, repo=repo, issue_file=request,
                   source=f"iBoard replay of phase {entry['id']} ({head[:12]})", kind='feature', surface=step['surface'],
                   iterations=contract['iterations'], seconds=_remaining(journal), image=step['image'],
                   tasks_root=root / 'tasks', workspaces_root=root / 'workspaces', source_mode='image',
                   build_source_image=True, memory_mb=contract['memory_mb'], criteria=phase_contract.criteria(entry),
                   prepare=partial(workflow.prepare, base_commit=new_base), run=drive)
        with task_lock(task):
            verified = phase_dispatch._verified(task, retained(), entry)
        return result(verified['status'], task=str(task), candidate=candidate, evidence=verified, patch=journal['proof'])
    except phase_dispatch.ERRORS as error:
        return result('blocked', reason=str(error)[:1800], task=str(task), patch=journal.get('proof'))


def run(original_task, new_base, out, *, seconds, prepare_replay=phase_replay.prepare,
        launch=start.start, resume=start.resume_task, execute=autopilot.run):
    if (not isinstance(new_base, str) or not re.fullmatch(r'[0-9a-f]{40}', new_base)
            or type(seconds) is not int or not 1 <= seconds <= 2_147_483_647):
        raise ReverifyError('provide an exact new base commit and a bounded replay time')
    original = Path(original_task).expanduser().resolve(strict=True)
    root = Path(out).expanduser().resolve()
    phase, _, _, _ = _source(original)
    if root.is_relative_to(Path(phase['contract']['repo'])) or root.is_relative_to(original.parent):
        raise ReverifyError('keep replay results outside the source checkout and original task')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    anchor = root / '.replay'
    if anchor.is_symlink():
        raise ReverifyError('replay lock anchor cannot be a symbolic link')
    anchor.touch(mode=0o600, exist_ok=True)
    with task_lock(anchor, lock_name='.phase-reverify.lock'):
        return _run(root, original, new_base, seconds=seconds, prepare_replay=prepare_replay,
                    launch=launch, resume=resume, execute=execute)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('task', 'new-base', 'out'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--seconds', required=True, type=int)
    args = parser.parse_args(argv)
    try:
        report = run(args.task, args.new_base, args.out, seconds=args.seconds)
    except phase_dispatch.ERRORS as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report['status'] != 'verified')


if __name__ == '__main__':
    raise SystemExit(main())
