"""Verify an OpenClaw Gateway journey using iBoard's existing Docker runner."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from runner.sandbox import SandboxError, run_sandbox
from workbench import cli, workflow

JOURNEYS = {
    "gateway-health": ("gateway.mjs", "VERIFY_OPENCLAW_GATEWAY_HEALTH_PASS"),
}
SKILL = Path(__file__).resolve().parents[1] / "skills" / "verify-openclaw"


def verify(repo, image, journey, *, timeout, memory_mb):
    helper, marker = JOURNEYS[journey]
    report = {"journey": journey, "requested_image": image}
    try:
        repo = repo.expanduser().resolve(strict=True)
        head = cli.sha(repo, "HEAD")
        report["commit"] = head
        pinned = workflow._verified_image(repo, image, 180, commit=head)
        report["verified_image_id"] = pinned
        # Only the selected harness is mounted. Never mount the target checkout,
        # the iBoard repository (which may contain credentials), or a home directory.
        with tempfile.TemporaryDirectory(prefix="iboard-openclaw-") as scratch:
            copy = Path(scratch) / helper
            shutil.copyfile(SKILL / helper, copy)
            report["helper_sha256"] = hashlib.sha256(copy.read_bytes()).hexdigest()
            report.update(run_sandbox(
                scratch, pinned, ["node", f"/workspace/{helper}"], timeout,
                memory_mb=memory_mb, source_root="/app", workspace_read_only=True,
            ))
    except (OSError, ValueError, SandboxError, workflow.WorkflowError, cli.WorkbenchError) as error:
        report["error"] = str(error)
    report["passed"] = (
        not report.get("error") and report.get("exit_code") == 0
        and report.get("cleanup_confirmed") is True
        and marker in report.get("stdout", "").splitlines()
    )
    return report


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
