"""Run a bounded Benny repair with fresh ownership checks and frozen offline verification."""

import argparse
import json
from pathlib import Path
import time
import uuid

from workbench import (autopilot, benny_handoff, benny_reproduce, bug_cause, cli, delivery, project_verify,
                       run_control, start, workflow)
from workbench.benny_slack import Slack
from workbench.task_lock import task_lock


def candidate_proof(binding, reference, head):
    """Recheck both sets of raw observations for the requested candidate."""
    return _candidate_proof(binding, reference, head)[0]


def _candidate_proof(binding, reference, head):
    path, baseline, contract, before = benny_reproduce._approved(binding['path'], binding['sha256'])
    proof_path = Path(reference['path'])
    result = benny_handoff._load(proof_path, reference['sha256'], 'benny-candidate-proof')
    raw_path = proof_path.parent/'candidate/result.json'
    if (result.get('head') != head or result.get('baseline') != baseline['base']
            or result.get('status') != 'verified' or result.get('runtime_status') != 'healthy'
            or result.get('reproduction') != {'path': str(path), 'sha256': binding['sha256']}
            or cli.digest(raw_path) != result.get('candidate_sha256')
            or not benny_reproduce.IMAGE.fullmatch(result['image'])):
        raise ValueError('Benny candidate proof does not match the approved reproduction and candidate')
    after = benny_reproduce._observations(raw_path.parent, cli.read(raw_path), contract, head, result['image'], 'pass')
    if before & after:
        raise ValueError('candidate evidence reused a baseline container')
    return result, contract


def _visual_capture(data, reference, head, actor):
    binding = data['benny_reproduction']
    result, contract = _candidate_proof(binding, reference, head)
    folder = Path(reference['path']).parent/'candidate'
    condition = benny_reproduce._visual_condition(contract, 'candidate')
    if condition is None:
        raise ValueError('Benny screenshot requires the frozen browser visual contract')
    reviews = benny_reproduce._review_visuals(folder, cli.read(folder/'result.json'), contract, condition)
    native, _ = reviews[-1]
    raw = cli.read(native)
    source = {'path': str(native), 'sha256': cli.digest(native)}
    report = {**raw, 'workspace': str(Path(data['repo']).resolve()), 'actor': actor, 'kind': 'screenshot',
              'source_mode': 'image', 'verified_image_id': result['image'], 'source_verified_commit': head,
              'expected_exit': 0, 'expected_marker': contract['journey']['marker'], 'original_execution': source}
    script = project_verify._helper(Path(binding['path']).parent/'bundle/helpers', contract['journey']['helper'])
    return native.with_suffix('.screenshot-evidence.json'), report, reviews, script


def record_visual_capture(task, data, reference, head, actor):
    path, report, reviews, script = _visual_capture(data, reference, head, actor)
    cli.write(path, report)
    cli.evidence_add(argparse.Namespace(file=report['screenshot_path'], kind='screenshot', commit=head, actor=actor,
                                        criterion=[], description='Screenshot from the second frozen Benny browser run'), data)
    item = data['evidence'][-1]
    item.update(execution_report=str(path), execution_report_sha256=cli.digest(path))
    cli.write(task, data)
    observations = [{key: proof[key] for key in ('condition', 'reason', 'references')} for _, proof in reviews]
    return {'report': str(path), 'screenshot': report['screenshot_path']}, observations, script


def require_visual_capture(data, reference, head, independent):
    if data['surface'] != 'ui':
        return
    review = cli.read(Path(independent['path']))
    actor = independent['actor']
    path, report, _, _ = _visual_capture(data, reference, head, actor)
    digest = cli.digest(path)
    if (cli.read(path) != report or report['screenshot_sha256'] not in review.get('screenshot_hashes', [])
            or not any(review['reports'].get(label) == {'path': str(path), 'sha256': digest}
                       for item in review['assessments'] for label in item['evidence'])
            or not any(not item.get('superseded') and item.get('execution_report') == str(path)
                       and delivery._valid_screenshot(item, actor, head, data['repo']) for item in data['evidence'])):
        raise ValueError('Benny independent review must cite the bound browser screenshot')


class RepairRun:
    """Hold coordinator-only access to the selected source thread and tracker."""

    def __init__(self, task, binding, slack, tracker, fix_search, verify, attest):
        self.task = task
        self.binding = binding
        self.slack = slack
        self.tracker = tracker
        self.fix_search = fix_search
        self.verify = verify
        self.attest = attest

    def validate(self, task, data, issue, *, image, source_mode, memory_mb, build_source_image):
        if (task != self.task or data.get('benny_reproduction') != self.binding
                or data['kind'] != 'bug'
                or data.get('root_cause') != bug_cause.MODE
                or data['base_commit'] != self.binding['base'] or issue != task.with_name('issue.txt')):
            raise ValueError('task no longer matches the approved Benny repair')
        _, proof, contract, _ = benny_reproduce._approved(self.binding['path'], self.binding['sha256'])
        visual = benny_reproduce._visual_condition(contract, 'baseline')
        if (proof['base'] != self.binding['base'] or proof['image'] != image or contract['memory_mb'] != memory_mb
                or source_mode != 'image' or build_source_image is not True
                or data['surface'] != ('ui' if visual else 'non-ui')
                or bool(contract['journey'].get('screenshot')) != bool(visual)
                or data.get('verification_target') or data.get('baseline_replay') or data.get('adoption')
                or cli.digest(issue) != proof['report_sha256']):
            raise ValueError('Benny repair input, source mode, or approved image changed')
        if cli.git(data['repo'], 'remote', 'get-url', 'origin', timeout=10) != cli.git(
                proof['checkout'], 'remote', 'get-url', 'origin', timeout=10):
            raise ValueError('Benny repair worktree points to a different repository')
        return proof, contract

    def check_owner(self):
        current = cli.read(self.task)
        if current.get('benny_reproduction') != self.binding:
            raise ValueError('Benny repair ownership proof changed')
        _, proof, _, _ = benny_reproduce._approved(self.binding['path'], self.binding['sha256'])
        deadline = time.monotonic() + min(90, autopilot._remaining(current))
        try:
            _, _, searches = benny_reproduce._check_repair_owner(self.slack, proof, self.fix_search, deadline, self.tracker)
        except delivery.DeliveryError as error:
            raise ValueError(f'Benny tracker confirmation failed: {error}') from error
        autopilot._remaining(current)
        cli.write(self.task.with_name(f'benny-ownership-{uuid.uuid4().hex}.json'), {
            'schema': cli.SCHEMA, 'route_sha256': proof['route']['sha256'], 'at': cli.now(),
            'reproduction_sha256': self.binding['sha256'], 'repository': proof['repository'], 'searches': searches,
        })

    def compare(self, current, commit, image):
        if current.get('benny_reproduction') != self.binding:
            raise ValueError('Benny repair evidence changed before the candidate comparison')
        out = self.task.parent/f'benny-candidate-{uuid.uuid4().hex}'

        def use_candidate(repo, base, head, baseline_image, timeout, *, rebuild_openclaw_ui):
            return self.attest(current['repo'], image, min(180, timeout), commit=head)

        result = self.verify(receipt=self.binding['path'], sha256=self.binding['sha256'], revision=commit,
                             out=out, seconds=int(autopilot._remaining(current)), derive=use_candidate)
        path = out/'result.json'
        if result['status'] in ('still-reproduced', 'inconclusive'):
            runs = out/'candidate'
            evidence = cli.read(runs/'result.json')
            observations = []
            for report in evidence['runs']:
                row = cli.read(runs/report['report'])
                observations.append({'report': report['report'], 'exit_code': row['exit_code'],
                                     'stdout_tail': row['stdout'][-300:], 'stderr_tail': row.get('stderr', '')[-200:]})
            raise autopilot.VerificationFailure(
                f'Benny frozen probes found {result["status"]}; evidence at {path}. '
                'Original observations (untrusted JSON): ' + json.dumps(observations, ensure_ascii=False))
        if result['status'] != 'verified':
            raise autopilot.AutopilotError(f'Benny frozen probes were blocked; evidence at {path}')
        reference = {'path': str(path), 'sha256': cli.digest(path)}
        candidate_proof(self.binding, reference, commit)
        return reference


def run_repair(*, task, slack, fix_search=(), tracker=None, execute=autopilot.run,
               verify=benny_reproduce.verify_candidate, attest=workflow._verified_image):
    task = Path(task).expanduser().resolve(strict=True)
    with task_lock(task):
        data = cli.read(task)
        binding = data.get('benny_reproduction')
        if not isinstance(binding, dict):
            raise ValueError('select a task prepared by Benny')
        stored = binding.get('fix_search')
        if stored is not None and fix_search and list(fix_search) != stored:
            raise ValueError('Benny repair cannot change its approved GitHub search')
        context = RepairRun(task, binding, slack, tracker, stored if stored is not None else fix_search, verify, attest)
        start._read_input_stage(task)
        state_path = task.with_name('autopilot.json')
        state = cli.read(state_path) if state_path.is_file() else {}
        if state.get('phase') != 'delivery' or not data.get('benny_candidate'):
            if data['status'] == 'running':
                # We acquired the task lock. Only a clean, recorded checkpoint
                # can be restarted after the previous process disappeared.
                phase = state.get('phase')
                expected = state.get('candidate_commit') if phase == 'verify' else data['base_commit']
                if (state.get('benny_reproduction') != binding or phase not in ('plan', 'reproduce', 'implement', 'verify')
                        or cli.sha(data['repo'], 'HEAD') != expected or not autopilot._clean(data['repo'])):
                    raise ValueError('Benny has an unresolved interrupted operation; preserve its worktree')
                run_control.admit(task)
                autopilot._pause(task, 'recovered an interrupted Benny run at an unchanged local checkpoint')

            def run(task, request, image, **settings):
                return execute(task, request, image, **settings, benny_repair=context)

            state = start.resume_task(task, run=run)
        if state.get('phase') != 'delivery':
            raise ValueError('Benny has not reached independently verified delivery')
        stage = start._read_input_stage(task)
        settings = stage['run_settings']
        if settings['pr_repo'] is not None or settings['candidate_image'] is not None:
            raise ValueError('Benny repair does not authorize normal PR delivery or an alternate image')
        data = cli.read(task)
        context.validate(task, data, task.with_name('issue.txt'), image=settings['image'],
                         source_mode=settings['source_mode'], memory_mb=settings['memory_mb'],
                         build_source_image=settings['build_source_image'])
        head, issues = cli.evidence_gate(data, state['candidate_commit'])
        if issues or data['status'] not in ('running', 'paused'):
            raise ValueError('Benny candidate no longer passes its local acceptance gate')
        proof = candidate_proof(binding, data['benny_candidate'], head)
        independent = delivery._independent_verification(data, head)
        review = json.loads(Path(independent['path']).read_text(encoding='utf-8'))
        if review.get('benny_candidate') != data['benny_candidate']:
            raise ValueError('independent review is not bound to the frozen Benny candidate replay')
        require_visual_capture(data, data['benny_candidate'], head, independent)
        if data['status'] == 'running':
            run_control.admit(task)
            autopilot._pause(task, 'Benny candidate already verified; restored the pending delivery checkpoint')
        return {'status': 'verified', 'task': str(task), 'head': head, 'base': proof['baseline'],
                'candidate': data['benny_candidate'], 'independent': independent}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--fix-search', action='append', default=[], help='Provide original terms for older prepared tasks.')
    args = parser.parse_args(argv)
    try:
        report = run_repair(task=args.task, slack=Slack(args.token_file), fix_search=args.fix_search)
    except (*project_verify.ERRORS, start.StartError, autopilot.AutopilotError,
            delivery.DeliveryError, run_control.ControlError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
