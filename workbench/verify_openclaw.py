"""Verify an OpenClaw Gateway journey using iBoard's existing Docker runner."""

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
from workbench import cli, workflow

JOURNEYS = {
    "gateway-health": ("gateway.mjs", "VERIFY_OPENCLAW_GATEWAY_HEALTH_PASS"),
}
SKILL = Path(__file__).resolve().parents[1] / "skills" / "verify-openclaw"


def verify(repo, image, journey, *, timeout, memory_mb):
    helper, marker = JOURNEYS[journey]
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
            report["helper_sha256"] = hashlib.sha256(copy.read_bytes()).hexdigest()
            report.update(run_sandbox(
                scratch, pinned, ["node", f"/workspace/{helper}"], deadline - time.monotonic(),
                memory_mb=memory_mb, source_root="/app", workspace_read_only=True,
            ))
    except (OSError, ValueError, SandboxError, workflow.WorkflowError, cli.WorkbenchError) as error:
        report["error"] = str(error)
    report["passed"] = (
        not report.get("error") and not report.get("timed_out") and report.get("exit_code") == 0
        and report.get("cleanup_confirmed") is True
        and marker in report.get("stdout", "").splitlines()
    )
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


def collect(task, data, head, image, actors, *, remaining, memory_mb):
    results = []
    for actor in actors:
        path = task.parent / f'gateway-health-{uuid.uuid4().hex}.json'
        report = verify(Path(data['repo']), image, 'gateway-health',
                        timeout=min(220, remaining()), memory_mb=memory_mb)
        report.update(actor=actor, source_mode='image', source_verified_commit=report.get('commit'))
        cli.write(path, report)
        if not report['passed'] or report['commit'] != head:
            raise GatewayGateError(f'OpenClaw Gateway verification failed: {path}')
        results.append({'report': str(path)})
    return results


def require(data, head, destination, *, review_item=None):
    """Require two distinct, reviewed Gateway executions for the target commit."""
    if not applies(data, destination):
        return
    helper, marker = JOURNEYS['gateway-health']
    expected_helper = cli.digest(SKILL / helper)
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
            reports = []
            for entry in review['reports'].values():
                if 'path' not in entry or cli.digest(entry['path']) != entry['sha256']:
                    continue
                run = json.loads(Path(entry['path']).read_text())
                if not any(e.get('kind') == 'runtime-log' and e.get('commit') == head
                           and e.get('actor') == run.get('actor') and not e.get('superseded')
                           and e.get('path') == entry['path'] and e.get('sha256') == entry['sha256']
                           for e in data['evidence']):
                    continue
                if (run.get('journey') == 'gateway-health' and run.get('commit') == head
                        and run.get('helper_sha256') == expected_helper and run.get('passed') is True
                        and run.get('exit_code') == 0 and run.get('cleanup_confirmed') is True
                        and run.get('container_created') is True and run.get('source_root') == '/app'
                        and run.get('command') == ['node', f'/workspace/{helper}']
                        and run.get('workspace_read_only') is True and run.get('timed_out') is False
                        and not run.get('error') and marker in run.get('stdout', '').splitlines()
                        and run.get('verified_image_id') == run.get('image') and
                        re.fullmatch(r'sha256:[0-9a-f]{64}', str(run.get('image'))) and run.get('workspace')):
                    reports.append({**run, 'sha256': entry['sha256']})
            for a in reports:
                for b in reports:
                    if (a.get('actor') == data['implementer'] and b.get('actor') == item['actor']
                            and a['image'] == b['image'] and a['workspace'] != b['workspace']):
                        return a, b
        except (OSError, ValueError, KeyError, TypeError, AttributeError, cli.WorkbenchError):
            continue
    raise GatewayGateError('OpenClaw PR requires two passing, independently reviewed offline Gateway runs on this commit')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journey", choices=JOURNEYS)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=190)
    parser.add_argument("--memory-mb", type=int, default=3328)
    args = parser.parse_args(argv)
    # Reserve the evidence path before running; retries must use a new file.
    def private_create(path, flags):
        return os.open(path, flags, 0o600)

    with open(args.report.expanduser(), "x", encoding="utf-8", opener=private_create) as output:
        report = verify(args.repo, args.image, args.journey, timeout=args.timeout, memory_mb=args.memory_mb)
        json.dump(report, output, indent=2)
        output.write("\n")
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {args.report}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
