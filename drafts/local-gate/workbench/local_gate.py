"""Explicit OpenClaw Vitest local gate; narrower than remote CI."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from workbench import cli, local_vitest
from workbench.task_lock import task_lock


class LocalGateError(Exception):
    pass


def configure(task_path, files, reason):
    task = Path(task_path).expanduser().resolve(strict=True)
    with task_lock(task):
        data = cli.read(task)
        if data.get('status') != 'paused' or data.get('local_gate'):
            raise LocalGateError('an unconfigured paused task is required')
        if not isinstance(reason, str) or not reason.strip() or not data.get('criteria'):
            raise LocalGateError('criteria and reason are required')
        head = data.get('adoption', {}).get('candidate_commit') or cli.sha(data['repo'], 'HEAD')
        try:
            command = local_vitest.command(data['repo'], data['base_commit'], head, files)
        except local_vitest.GuardError as error:
            raise LocalGateError(str(error)) from error
        record = {'schema': 1, 'mode': 'openclaw-vitest-json-v1', 'command': command,
                  'files': list(files),
                  'base': data['base_commit'], 'criteria_digest': cli.criteria_digest(data),
                  'reason': reason.strip()}
        data['local_gate'] = record
        data['events'].append({'at': cli.now(), 'type': 'local-gate-selected', **record})
        cli.write(task, data)
        return record


def validate_configuration(data, head):
    policy = data.get('local_gate')
    records = [e for e in data.get('events', []) if e.get('type') == 'local-gate-selected']
    if (not isinstance(policy, dict) or policy.get('schema') != 1 or
            policy.get('mode') != 'openclaw-vitest-json-v1' or len(records) != 1 or
            any(records[0].get(k) != v for k, v in policy.items()) or
            policy.get('base') != data.get('base_commit') or
            policy.get('criteria_digest') != cli.criteria_digest(data)):
        raise LocalGateError('local policy, baseline or acceptance criteria changed')
    try:
        expected = local_vitest.command(data['repo'], policy['base'], head, policy.get('files'))
    except local_vitest.GuardError as error:
        raise LocalGateError(str(error)) from error
    if expected != policy.get('command'):
        raise LocalGateError('unit test invocation or file list changed')
    return policy


def _digest_policy(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def guard_assessments(review, changes, reports, independent_path):
    assessments = review.get('guard_assessments', [])
    if not isinstance(assessments, list) or len(assessments) != len(changes):
        raise LocalGateError('independent review does not cover every changed test source')
    execution_labels = {key for key, item in reports.items()
                        if isinstance(item, dict) and item.get('path') == independent_path}
    by_path = {item['path']: item for item in changes}
    seen = set()
    for item in assessments:
        if not isinstance(item, dict) or item.get('path') not in by_path or item['path'] in seen:
            raise LocalGateError('invalid guard source assessment')
        source = by_path[item['path']]
        refs = item.get('evidence')
        if (item.get('base_sha256') != source['base_sha256'] or item.get('head_sha256') != source['head_sha256'] or
                item.get('verdict') != 'PASS' or item.get('preserves_coverage') is not True or
                not isinstance(item.get('reason'), str) or not item['reason'].strip() or
                not isinstance(refs, list) or 'DIFF' not in refs or not execution_labels.intersection(refs)):
            raise LocalGateError('guard source change lacks independent proof of preserved coverage')
        seen.add(item['path'])
    return assessments


def record_run(task_path, head, image_id, results):
    from workbench import delivery
    task = Path(task_path)
    data = cli.read(task)
    policy = validate_configuration(data, head)
    if not isinstance(image_id, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
        raise LocalGateError('candidate image ID must be immutable')
    if not isinstance(results, list) or len(results) != 2:
        raise LocalGateError('exactly two independent unit reports are required')
    recorded = []
    for actor, result in zip((data['implementer'], 'independent-verifier'), results):
        path = result.get('report') if isinstance(result, dict) else None
        matches = [e for e in data.get('evidence', []) if e.get('path') == path and e.get('actor') == actor
                   and e.get('kind') == 'test-result' and not e.get('superseded')]
        if len(matches) != 1:
            raise LocalGateError('sandbox report is not uniquely bound to its actor')
        evidence = matches[0]
        try:
            report = json.loads(Path(path).read_text())
            if (cli.digest(path) != evidence['sha256'] or evidence.get('criteria_digest') != policy['criteria_digest'] or
                    report.get('command') != policy['command'] or report.get('expected_exit') != 0 or
                    report.get('source_mode') != 'image' or report.get('verified_image_id') != image_id or
                    not delivery._valid_run(evidence, actor, 'test-result', head, data['repo'])):
                raise LocalGateError('sandbox report identity or result mismatch')
            local_vitest.validate_json(report.get('stdout'), policy['files'])
        except (OSError, ValueError, KeyError, cli.WorkbenchError, local_vitest.GuardError) as error:
            raise LocalGateError('sandbox unit report invalid') from error
        recorded.append({'id': evidence['id'], 'path': path, 'sha256': evidence['sha256'], 'actor': actor})
    data['events'].append({'at': cli.now(), 'type': 'local-gate-run', 'head': head, 'image_id': image_id,
                           'policy_digest': _digest_policy(policy), 'reports': recorded})
    cli.write(task, data)


def validate(data, head):
    from workbench import delivery
    policy = validate_configuration(data, head)
    runs = [e for e in data.get('events', []) if e.get('type') == 'local-gate-run' and e.get('head') == head]
    if not runs:
        raise LocalGateError('candidate lacks a completed local gate run')
    run = runs[-1]
    if (run.get('policy_digest') != _digest_policy(policy) or not isinstance(run.get('image_id'), str) or
            not re.fullmatch(r'sha256:[0-9a-f]{64}', run['image_id']) or
            not isinstance(run.get('reports'), list) or len(run['reports']) != 2):
        raise LocalGateError('candidate local gate identity is invalid')
    for actor, recorded in zip((data['implementer'], 'independent-verifier'), run['reports']):
        matched = False
        for evidence in data.get('evidence', []):
            if (evidence.get('superseded') or evidence.get('kind') != 'test-result' or
                    evidence.get('actor') != actor or evidence.get('commit') != head or
                    evidence.get('criteria_digest') != policy['criteria_digest'] or
                    not isinstance(recorded, dict) or recorded.get('id') != evidence.get('id') or
                    recorded.get('sha256') != evidence.get('sha256') or
                    recorded.get('path') != evidence.get('path') or recorded.get('actor') != actor):
                continue
            try:
                if cli.digest(evidence['path']) != evidence['sha256']:
                    continue
                report = json.loads(Path(evidence['path']).read_text())
                if (report.get('command') == policy['command'] and report.get('expected_exit') == 0 and
                        report.get('source_mode') == 'image' and
                        report.get('verified_image_id') == run['image_id'] and
                        delivery._valid_run(evidence, actor, 'test-result', head, data['repo'])):
                    local_vitest.validate_json(report.get('stdout'), policy['files'])
                    matched = True
                    break
            except (OSError, ValueError, KeyError, cli.WorkbenchError, local_vitest.GuardError):
                continue
        if not matched:
            raise LocalGateError(f'missing successful bound Vitest report for {actor}')
    try:
        changes = local_vitest.changed_test_sources(data['repo'], policy['base'], head)
        reviews = [e for e in data['evidence'] if e.get('kind') == 'independent-review' and not e.get('superseded')
                   and e.get('commit') == head and e.get('actor') != data['implementer']
                   and e.get('criteria_digest') == policy['criteria_digest']]
        if not reviews:
            raise LocalGateError('guard changes require an independent review')
        item = reviews[-1]
        if cli.digest(item['path']) != item['sha256']:
            raise LocalGateError('independent review changed')
        review = json.loads(Path(item['path']).read_text())
        report_map = review.get('reports')
        diff = report_map.get('DIFF') if isinstance(report_map, dict) else None
        independent = run['reports'][1]
        linked = [r for r in report_map.values() if isinstance(r, dict) and
                  r.get('path') == independent['path'] and r.get('sha256') == independent['sha256']] if isinstance(report_map, dict) else []
        if (review.get('verdict') != 'PASS' or review.get('baseline_commit') != policy['base'] or
                review.get('candidate_commit') != head or review.get('criterion_digest') != policy['criteria_digest'] or
                not isinstance(diff, dict) or diff.get('kind') != 'git-diff' or diff.get('base') != policy['base'] or
                diff.get('head') != head or diff.get('sha256') != hashlib.sha256(delivery.candidate_diff(data['repo'], policy['base'], head)).hexdigest() or
                not linked or cli.digest(independent['path']) != independent['sha256']):
            raise LocalGateError('independent guard review is not bound to the candidate diff and sandbox report')
        guard_assessments(review, changes, report_map, independent['path'])
    except (OSError, ValueError, KeyError, local_vitest.GuardError, cli.WorkbenchError) as error:
        raise LocalGateError('cannot validate independent guard source review') from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--file', action='append', required=True)
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    print(json.dumps(configure(args.task, args.file, args.reason)))


if __name__ == '__main__':
    main()
