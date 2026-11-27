"""Verify OpenClaw user journeys using iBoard's offline Docker runner."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from runner.sandbox import SandboxError, run_sandbox
from workbench import browser_recording, cli, workflow

JOURNEYS = {
    "gateway-health": ("gateway.mjs", "VERIFY_OPENCLAW_GATEWAY_HEALTH_PASS"),
    "skills-check": ("skills-check.mjs", "VERIFY_OPENCLAW_SKILLS_CHECK_PASS"),
    "skills-inspect": ("skills-inspect.mjs", "VERIFY_OPENCLAW_SKILLS_INSPECT_PASS"),
    "config-validate": ("config-validate.mjs", "VERIFY_OPENCLAW_CONFIG_VALIDATE_PASS"),
}
SESSION_RENAME = 'control-ui-session-rename'
SESSION_DELETE = 'control-ui-session-delete'
SESSION_FILTER = 'control-ui-session-status-filter'
SESSION_WRITES = (SESSION_RENAME, SESSION_DELETE)
SESSION_JOURNEYS = (*SESSION_WRITES, SESSION_FILTER)
BROWSER_JOURNEYS = {
    'control-ui-connected': ('control-ui-connected.mjs', 'VERIFY_OPENCLAW_CONTROL_UI_CONNECTED_PASS'),
    SESSION_RENAME: ('control-ui-connected.mjs', 'VERIFY_OPENCLAW_CONTROL_UI_SESSION_RENAME_PASS'),
    SESSION_DELETE: ('control-ui-connected.mjs', 'VERIFY_OPENCLAW_CONTROL_UI_SESSION_DELETE_PASS'),
    SESSION_FILTER: ('control-ui-connected.mjs', 'VERIFY_OPENCLAW_CONTROL_UI_SESSION_STATUS_FILTER_PASS'),
}
SKILL = Path(__file__).resolve().parents[1] / "skills" / "verify-openclaw"


def _timeout(journey):
    return 480 if journey in SESSION_JOURNEYS else 320


def _journey(journey):
    helper, marker = (BROWSER_JOURNEYS if journey in BROWSER_JOURNEYS else JOURNEYS)[journey]
    command = ['node', f'/workspace/{helper}']
    if journey in SESSION_JOURNEYS:
        command.append(journey.removeprefix('control-ui-'))
    return helper, marker, command


def _filter_evidence(ui, readback):
    evidence = ui['filter']
    fixtures = evidence['fixtures']
    active, archived = fixtures['active'], fixtures['archived']
    if not (readback['count'] == 1 and 'archived' not in readback
            and type(evidence['sessionMutations']) is int and evidence['sessionMutations'] == 0
            and active['key'] == 'agent:main:iboard-browser-verification'
            and archived['key'] == 'agent:main:iboard-browser-archived'
            and all(isinstance(item['sessionId'], str) and item['sessionId'].strip() for item in (active, archived))
            and active['sessionId'] != archived['sessionId']):
        return False
    expectations = (
        ('active', 'load', [active]),
        ('archived', 'filter', [archived]),
        ('all', 'filter', [active, archived]),
        ('active', 'filter-refresh', [active]),
    )
    steps = evidence['steps']
    if not isinstance(steps, list) or len(steps) != len(expectations):
        return False
    requests = set()
    for step, (state, trigger, expected) in zip(steps, expectations):
        if state == 'active':
            scope_matches = 'archived' not in step
        elif state == 'archived':
            scope_matches = step.get('archived') is True
        else:
            scope_matches = step.get('archived') == 'all'
        request = step['requestId']
        if not (scope_matches and (step.get('state'), step.get('trigger'), step.get('source')) ==
                (state, trigger, 'sessions-page') and isinstance(request, str) and request.strip()
                and request not in requests):
            return False
        if (not isinstance(step['gateway'], list) or not isinstance(step['visibleKeys'], list)
                or sorted(step['gateway'], key=lambda item: item['key']) != sorted(expected, key=lambda item: item['key'])
                or sorted(step['visibleKeys']) != sorted(item['key'] for item in expected)):
            return False
        requests.add(request)
    return True


def _browser_evidence(report, screenshot):
    """Check the artifact attachment and the observed session behavior."""
    try:
        if (screenshot.is_symlink() or str(screenshot.resolve(strict=True)) != report.get('screenshot_path')
                or cli.digest(screenshot) != report.get('screenshot_sha256')):
            return False
        marker = _journey(report['journey'])[1]
        restart_required = report['journey'] in SESSION_WRITES
        lines = report['stdout'].splitlines()
        if report.get('stdout_truncated') or len(lines) != (5 if restart_required else 4) or lines[-1] != marker:
            return False
        doctor, *activity, ui, cleanup = map(json.loads, lines[:-1])
        initial_pid = doctor['gatewayPid']
        clean_exit = {'code': 0, 'signal': None}
        handshake, readback = ui['handshake'], ui['readback']
        if not (doctor.get('phase') == 'doctor' and doctor.get('ready') is True and doctor.get('live') is True
                and doctor.get('origin') == 'http://127.0.0.1:18789' and type(initial_pid) is int and initial_pid > 1
                and ui.get('phase') == 'ui' and ui.get('route') == '/sessions'
                and ui.get('servedCommit') == report['commit'] and ui.get('screenshot') is True
                and handshake.get('role') == 'operator' and handshake.get('method') == 'token'
                and type(handshake.get('protocol')) is int and handshake['protocol'] > 0
                and readback.get('method') == 'sessions.list' and type(readback.get('count')) is int
                and cleanup == {'phase': 'cleanup', 'exit': clean_exit, 'scratchRemoved': True}):
            return False
        if not restart_required:
            if 'mutation' in ui or ui.get('gatewayPid') != initial_pid:
                return False
            if report['journey'] == SESSION_FILTER:
                return _filter_evidence(ui, readback)
            return readback['count'] == 0
        restart = activity[0]
        restarted_pid = restart['gatewayPid']
        if not (restart.get('phase') == 'restart' and restart.get('origin') == doctor['origin']
                and restart.get('ready') is True and restart.get('live') is True and restart.get('portClosed') is True
                and restart.get('previousPid') == initial_pid and restart.get('previousExit') == clean_exit
                and type(restarted_pid) is int and restarted_pid > 1 and restarted_pid != initial_pid
                and ui.get('gatewayPid') == restarted_pid):
            return False
        mutation = ui['mutation']
        if not (mutation.get('key') == 'agent:main:iboard-browser-verification'
                and isinstance(mutation.get('sessionId'), str) and mutation['sessionId'].strip()):
            return False
        if report['journey'] == SESSION_DELETE:
            survivor = mutation['survivor']
            return (readback['count'] == 1 and readback.get('archived') == 'all'
                    and mutation.get('method') == 'sessions.delete' and mutation.get('responseKey') == mutation['key']
                    and (mutation.get('expectedSessionId'), mutation.get('requestCount'),
                         mutation.get('afterCancelCount')) == (mutation['sessionId'], 1, 2)
                    and type(mutation['requestCount']) is int and type(mutation['afterCancelCount']) is int
                    and mutation.get('cancelBrowserReplaced') is True and mutation.get('cancelGatewayPid') == initial_pid
                    and all(mutation.get(key) is True for key in (
                        'canceledWithoutWrite', 'deleted', 'freshContextReadback', 'targetAbsent'))
                    and survivor.get('key') == 'agent:main:iboard-browser-survivor'
                    and isinstance(survivor.get('sessionId'), str) and survivor['sessionId'].strip() != ''
                    and survivor['sessionId'] != mutation['sessionId'])
        return (readback['count'] > 0 and mutation.get('method') == 'sessions.patch'
                and mutation.get('originalLabel') == 'iBoard verification fixture'
                and mutation.get('finalLabel') == 'iBoard verification persisted'
                and mutation.get('menuEntries') == ['context-menu', 'row-button']
                and mutation.get('expectedSessionId') == mutation['sessionId']
                and type(mutation.get('requestCount')) is int and mutation['requestCount'] == 1
                and all(mutation.get(key) is True for key in ('canceledWithoutWrite', 'accepted', 'freshContextReadback')))
    except (OSError, KeyError, ValueError, TypeError, AttributeError, cli.WorkbenchError):
        return False


def verify(repo, image, journey, *, timeout, memory_mb, screenshot=None, recording=None):
    browser = journey in BROWSER_JOURNEYS
    if browser != (screenshot is not None):
        raise ValueError('a Control UI browser journey requires a screenshot path')
    if recording is not None and not browser:
        raise ValueError('a recording is only available for the Control UI browser journeys')
    helper, marker, command = _journey(journey)
    deadline = time.monotonic() + timeout
    report = {"journey": journey, "requested_image": image}
    try:
        repo = repo.expanduser().resolve(strict=True)
        head = cli.sha(repo, "HEAD")
        report["commit"] = head
        pinned = workflow._verified_image(repo, image, min(180, timeout), commit=head)
        report["verified_image_id"] = pinned
        # Only the selected harness is mounted. Never mount the target checkout,
        # the iBoard repository (which may contain credentials), or a home directory.
        with tempfile.TemporaryDirectory(prefix="iboard-openclaw-") as scratch:
            copy = Path(scratch) / helper
            shutil.copyfile(SKILL / helper, copy)
            if browser:
                (Path(scratch) / 'expected-commit.txt').write_text(head + '\n', encoding='utf-8')
            report["helper_sha256"] = hashlib.sha256(copy.read_bytes()).hexdigest()
            report.update(run_sandbox(
                scratch, pinned, command, deadline - time.monotonic(),
                memory_mb=memory_mb, source_root="/app", workspace_read_only=True,
                **({'screenshot_output': screenshot, 'pids_limit': 384} if browser else {}),
                **({'recording_output': recording} if recording is not None else {}),
            ))
    except (OSError, ValueError, SandboxError, workflow.WorkflowError, cli.WorkbenchError) as error:
        report["error"] = str(error)
    report["passed"] = (
        not report.get("error") and not report.get("timed_out") and report.get("exit_code") == 0
        and report.get("cleanup_confirmed") is True
        and marker in report.get("stdout", "").splitlines()
        and (not browser or _browser_evidence(report, Path(screenshot)))
        and (recording is None or browser_recording.receipt(
            report, recording, report.get('commit'), expected_journey=journey.removeprefix('control-ui-')))
    )
    return report


def audit(repo, image, *, timeout, memory_mb):
    """Check the documented feature map and exercise every registered journey."""
    report = {'journey': 'audit', 'requested_image': image, 'runs': {}, 'passed': False}
    feature_dir = SKILL / 'features'
    try:
        links = re.findall(r'^\|\s*\[[^\]\n]+\]\(([a-z0-9-]+)\.md\)\s*\|',
                           (feature_dir / 'README.md').read_text(encoding='utf-8'), re.MULTILINE)
        documented = {p.stem for p in feature_dir.glob('*.md') if p.name != 'README.md'}
        expected = set(JOURNEYS)
        if len(links) != len(set(links)) or set(links) != expected or documented != expected:
            report.update(status='map-drift', listed=links, documented=sorted(documented),
                          registered=sorted(expected))
            return report
    except (OSError, ValueError) as error:
        report.update(status='map-unavailable', error=str(error))
        return report

    deadline = time.monotonic() + timeout
    commit, pinned = None, None
    for journey in links:
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            report['status'] = 'timeout'
            return report
        run = verify(repo, pinned or image, journey, timeout=remaining, memory_mb=memory_mb)
        report['runs'][journey] = run
        if not run['passed']:
            report['status'] = 'check-failed'
            return report
        if commit is None:
            commit, pinned = run['commit'], run['verified_image_id']
        if run['commit'] != commit or run['verified_image_id'] != pinned:
            report['status'] = 'target-changed'
            return report
    report.update(status='map-verified', commit=commit, verified_image_id=pinned, passed=True)
    return report


class GatewayGateError(cli.WorkbenchError):
    pass


def applies(data, destination=None):
    if destination and destination.lower().endswith(('/openclaw', '/myopenclaw')):
        return True
    try:
        return json.loads(cli.git(data['repo'], 'show', f"{data['base_commit']}:package.json")).get('name') == 'openclaw'
    except (cli.WorkbenchError, ValueError, AttributeError):
        return False


def required_journeys(data, head):
    """Select the smoke check and mapped regressions from the committed diff."""
    try:
        paths = cli.git(data['repo'], 'diff', '--name-only', '--no-renames', '-z',
                        data['base_commit'], head, '--').split('\0')
    except (KeyError, OSError, cli.WorkbenchError) as error:
        raise GatewayGateError('Cannot determine required OpenClaw journeys') from error
    selected = ['gateway-health']
    if any(path in ('src/shared/frontmatter.ts', 'src/agents/agent-scope-config.ts')
           or path.startswith(('src/skills/', 'src/cli/skills-cli.', 'src/cli/skills-cli/'))
           for path in paths):
        selected.extend(('skills-check', 'skills-inspect'))
    if any(path.startswith(('src/config/', 'src/cli/config-cli.', 'src/cli/config-cli-')) for path in paths):
        selected.append('config-validate')
    sessions_ui = any(path == 'ui/src/styles/sessions.css'
                      or path.startswith(('ui/src/pages/sessions/', 'ui/src/lib/sessions/')) for path in paths)
    if sessions_ui or 'ui/src/lib/session-rename.ts' in paths:
        selected.append(SESSION_RENAME)
    if sessions_ui or 'src/gateway/server-methods/sessions-delete.ts' in paths:
        selected.append(SESSION_DELETE)
    if sessions_ui or any(path in ('ui/src/components/solid/settings-ui.tsx',
                                   'src/gateway/server-methods/sessions-read.ts',
                                   'src/gateway/server-methods/sessions-patch-archive.ts')
                          for path in paths):
        selected.append(SESSION_FILTER)
    return selected


def needs_browser(data, head):
    return any(journey in BROWSER_JOURNEYS for journey in required_journeys(data, head))


def build_browser_image(repo, base, image, head, timeout):
    from workbench import visual_parity
    # Reuse the existing offline OpenClaw UI build with frozen dependencies.
    plan = {'rebuild': {'profile': visual_parity.OPENCLAW_PROFILE,
                        'sha256': cli.digest(visual_parity.OPENCLAW_RECIPE)}}
    return visual_parity.rebuild_image(repo, base, image, head, plan, timeout)['image']


def collect(task, data, head, image, actors, *, remaining, memory_mb, record_browser=False):
    results = []
    for journey in required_journeys(data, head):
        for actor in actors:
            path = task.parent / f'{journey}-{uuid.uuid4().hex}.json'
            options = {'screenshot': path.with_suffix('.png')} if journey in BROWSER_JOURNEYS else {}
            if options and record_browser:
                options['recording'] = path.with_suffix('.jsonl')
            report = verify(Path(data['repo']), image, journey,
                            timeout=min(_timeout(journey), remaining()), memory_mb=memory_mb, **options)
            report.update(actor=actor, source_mode='image', source_verified_commit=report.get('commit'))
            cli.write(path, report)
            if not report['passed'] or report['commit'] != head:
                if workflow.docker_oom(report):
                    raise workflow.WorkflowError(f'OpenClaw {journey} exceeded available Docker memory; raw report saved at {path}')
                raise workflow.WorkflowError(f'OpenClaw {journey} verification failed; raw report saved at {path}')
            results.append({'report': str(path), 'actor': actor, 'journey': journey})
    return results


def require(data, head, destination, *, review_item=None):
    """Require two distinct executions per journey in one accepted review."""
    if not applies(data, destination):
        return
    required = required_journeys(data, head)
    helpers = {journey: cli.digest(SKILL / _journey(journey)[0]) for journey in required}
    for item in data['evidence'] if review_item is None else (review_item,):
        if (item.get('superseded') or item.get('kind') != 'independent-review'
                or item.get('commit') != head or item.get('actor') != 'independent-verifier'
                or item.get('actor') == data['implementer']
                or item.get('criteria_digest') != cli.criteria_digest(data)):
            continue
        try:
            if cli.digest(item['path']) != item.get('sha256'):
                continue
            review = json.loads(Path(item['path']).read_text())
            if (review.get('verdict') != 'PASS' or review.get('candidate_commit') != head
                    or review.get('baseline_commit') != data['base_commit']
                    or review.get('criterion_digest') != cli.criteria_digest(data)):
                continue
            reports = {journey: [] for journey in required}
            for entry in review['reports'].values():
                if 'path' not in entry or cli.digest(entry['path']) != entry['sha256']:
                    continue
                run = json.loads(Path(entry['path']).read_text())
                if not any(e.get('kind') == 'runtime-log' and e.get('commit') == head
                           and e.get('actor') == run.get('actor') and not e.get('superseded')
                           and e.get('path') == entry['path'] and e.get('sha256') == entry['sha256']
                           for e in data['evidence']):
                    continue
                journey = run.get('journey')
                if journey not in reports:
                    continue
                _, marker, command = _journey(journey)
                container = run.get('create_stdout')
                if (not isinstance(container, str) or run.get('create_stdout_truncated') is True
                        or not re.fullmatch(r'[0-9a-f]{64}', container.strip())):
                    continue
                if journey in BROWSER_JOURNEYS:
                    if not _browser_evidence(run, Path(entry['path']).with_suffix('.png')):
                        continue
                    if ('recording_path' in run or 'recording_sha256' in run) and not browser_recording.receipt(
                            run, Path(entry['path']).with_suffix('.jsonl'), head,
                            expected_journey=journey.removeprefix('control-ui-')):
                        continue
                if (run.get('commit') == head
                        and run.get('helper_sha256') == helpers[journey] and run.get('passed') is True
                        and run.get('exit_code') == 0 and run.get('cleanup_confirmed') is True
                        and run.get('container_created') is True and run.get('source_root') == '/app'
                        and run.get('command') == command
                        and run.get('workspace_read_only') is True and run.get('timed_out') is False
                        and not run.get('error') and marker in run.get('stdout', '').splitlines()
                        and run.get('verified_image_id') == run.get('image') and
                        re.fullmatch(r'sha256:[0-9a-f]{64}', str(run.get('image'))) and run.get('workspace')):
                    reports[journey].append({**run, 'sha256': entry['sha256']})
            # Use the same immutable image across every required journey.
            pairs_by_image = {
                journey: {a['image']: (a, b) for a in runs for b in runs
                 if a.get('actor') == data['implementer'] and b.get('actor') == item['actor']
                 and a['image'] == b['image'] and a['workspace'] != b['workspace']
                 and a['create_stdout'].strip() != b['create_stdout'].strip()}
                for journey, runs in reports.items()
            }
            common = set.intersection(*(set(pairs) for pairs in pairs_by_image.values()))
            if common:
                image = min(common)
                return {journey: pairs[image] for journey, pairs in pairs_by_image.items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, cli.WorkbenchError):
            continue
    raise GatewayGateError(
        'OpenClaw PR requires two passing, independently reviewed offline runs '
        f'on the same candidate image for each journey: {", ".join(required)}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journey", choices=(*JOURNEYS, *BROWSER_JOURNEYS, "audit"))
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument('--screenshot', type=Path, help='PNG evidence path for a Control UI browser journey')
    parser.add_argument('--recording', type=Path, help='JSONL recording path for a Control UI browser journey')
    parser.add_argument("--timeout", type=int, help='seconds (default: 480 for Sessions journeys, otherwise 320)')
    parser.add_argument("--memory-mb", type=int, default=3328)
    args = parser.parse_args(argv)
    if (args.journey in BROWSER_JOURNEYS) != (args.screenshot is not None):
        parser.error('--screenshot is required only for a Control UI browser journey')
    if args.screenshot is not None:
        args.screenshot = args.screenshot.expanduser().absolute()
        if args.screenshot.exists() or args.screenshot.is_symlink() or args.screenshot.resolve() == args.report.expanduser().resolve():
            parser.error('choose a new screenshot path separate from the JSON report')
    if args.recording is not None:
        args.recording = args.recording.expanduser().absolute()
        if (args.screenshot is None or args.recording.suffix != '.jsonl' or args.recording.exists()
                or args.recording.is_symlink()
                or args.recording.resolve() in (args.report.expanduser().resolve(), args.screenshot.resolve())):
            parser.error('choose a new JSONL recording path separate from the report and browser screenshot')
    # Reserve the evidence path before running; retries must use a new file.
    def private_create(path, flags):
        return os.open(path, flags, 0o600)

    with open(args.report.expanduser(), "x", encoding="utf-8", opener=private_create) as output:
        options = dict(timeout=_timeout(args.journey) if args.timeout is None else args.timeout, memory_mb=args.memory_mb)
        if args.journey == 'audit':
            report = audit(args.repo, args.image, **options)
        else:
            if args.screenshot is not None:
                options['screenshot'] = args.screenshot
            if args.recording is not None:
                options['recording'] = args.recording
            report = verify(args.repo, args.image, args.journey, **options)
        json.dump(report, output, indent=2)
        output.write("\n")
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {args.report}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
