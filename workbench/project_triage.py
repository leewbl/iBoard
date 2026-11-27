"""Compare one failing user journey with an independent probe in offline Docker."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import time

from runner.sandbox import run_sandbox
from workbench import cli, project_verify, workflow


def _image_reference(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]*", value):
        raise ValueError("use a valid local baseline image reference")
    return value


def _derive(repo, base, head, image_id, timeout):
    deadline = time.monotonic() + timeout
    with TemporaryDirectory(prefix="iboard-triage-worktree-") as scratch:
        workspace = Path(scratch) / "source"
        cli.git(repo, "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach",
                str(workspace), head, timeout=min(timeout, 60))
        try:
            return workflow.build_source_image(workspace, base, image_id, deadline - time.monotonic())
        finally:
            cli.git(repo, "worktree", "remove", "--force", str(workspace), timeout=30)


def _observation(raw, check, image):
    lines = raw.get("stdout", "").splitlines()
    success = check["marker"] in lines
    failure = check["failure_marker"] in lines
    intact = (raw.get("container_created") is True and raw.get("cleanup_confirmed") is True
              and raw.get("workspace_read_only") is True and raw.get("image") == image
              and not raw.get("stdout_truncated") and not raw.get("error") and not raw.get("timed_out"))
    if not intact or (success and failure):
        return "unclassified"
    if raw.get("passed") is True and success and not failure:
        return "pass"
    if raw.get("exit_code") == 1 and failure and not success:
        return "observed-failure"
    return "unclassified"


def _classify(base, candidate, changed):
    if not (base["doctor_healthy"] and candidate["doctor_healthy"]):
        return "environment-blocked"
    pass_twice, fail_twice = ["pass"] * 2, ["observed-failure"] * 2
    if base["oracle"] != pass_twice:
        return "inconclusive"
    if candidate["oracle"] == pass_twice and candidate["primary"] == fail_twice:
        return "harness-gap"
    if (changed and base["primary"] == pass_twice and candidate["oracle"] == fail_twice
            and candidate["primary"] == fail_twice):
        return "product-regression"
    if base["primary"] == pass_twice and candidate["primary"] == candidate["oracle"] == pass_twice:
        return "healthy"
    return "inconclusive"


def _probe(engine, bundle, doctor, primary, oracle, digests):
    def checked_drive(check, suffix):
        passed = engine.drive(bundle, check, suffix)
        item = engine.report["runs"][-1]
        report = engine.output / item["report"]
        raw = cli.read(report)
        if (raw.get("helper_sha256") != digests[check["helper"]]
                or raw.get("commit") != engine.revision or cli.digest(report) != item["sha256"]):
            raise ValueError("triage sandbox proof does not match the frozen inputs")
        return passed, raw

    result = {"doctor_healthy": checked_drive(doctor, "")[0], "primary": [], "oracle": []}
    for label, check in (("primary", primary), ("oracle", oracle)):
        for attempt in range(1, 3):
            if not result["doctor_healthy"]:
                return result
            _, raw = checked_drive(check, f".{label}-{attempt}")
            observed = _observation(raw, check, engine.report["verified_image_id"])
            result[label].append(observed)
            if observed != "pass":
                result["doctor_healthy"] = checked_drive(doctor, f".after-{label}-{attempt}")[0]
    return result


def run(*, repo, base, revision, image, manifest, helpers, journey, oracle, oracle_marker, oracle_failure_marker,
        out, timeout=600, memory_mb=3328, sandbox=run_sandbox, attester=workflow._verified_image, derive=_derive):
    repo = Path(repo).expanduser().resolve(strict=True)
    base = cli.sha(repo, base, timeout=10)
    revision = cli.sha(repo, revision, timeout=10)
    output = Path(out).expanduser().resolve()
    if output.is_relative_to(repo):
        raise ValueError("keep triage evidence outside the product checkout")
    if not 1 <= timeout <= 3600 or not 128 <= memory_mb <= 8192:
        raise ValueError("time and memory bounds are 1–3600 s and 128–8192 MiB")
    _image_reference(image)
    if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", base, revision],
                      capture_output=True, timeout=10).returncode:
        raise ValueError("triage requires a candidate descended from the known-good commit")
    contract = project_verify._load_contract(manifest)
    doctor = contract["doctor"]
    primary = next((item for item in contract["features"] if item["id"] == journey), None)
    if not primary or not primary.get("failure_marker"):
        raise ValueError("select a journey with a declared observed failure marker")
    known_markers = {item[key] for item in (primary, doctor) for key in ("marker", "failure_marker") if key in item}
    if (not re.fullmatch(r"[A-Z][A-Z0-9_]{0,92}_PASS", oracle_marker)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,92}_FAIL", oracle_failure_marker)
            or {oracle_marker, oracle_failure_marker} & known_markers):
        raise ValueError("provide distinct exact PASS and FAIL marker lines for the independent probe")
    source = Path(helpers).expanduser().resolve(strict=True)
    oracle = Path(oracle).expanduser()
    if oracle.suffix not in project_verify.ENGINES:
        raise ValueError("independent probe must be a standalone Node, Python or shell script")
    oracle_name = "independent-probe" + oracle.suffix
    if oracle_name in (primary["helper"], doctor["helper"]):
        raise ValueError("helper name is reserved for the independent probe")
    oracle_input = project_verify._helper(oracle.parent, oracle.name)
    primary_input = project_verify._helper(source, primary["helper"])
    if oracle_input == primary_input:
        raise ValueError("an independent probe must have its own implementation")
    inputs = {doctor["helper"]: project_verify._helper(source, doctor["helper"]),
              primary["helper"]: primary_input, oracle_name: oracle_input}
    digests = {name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()}
    alternate = {key: value for key, value in primary.items() if key not in ("args", "browser", "screenshot")}
    alternate.update(helper=oracle_name, marker=oracle_marker, failure_marker=oracle_failure_marker, screenshot=False)
    output.mkdir(mode=0o700, parents=True)
    bundle = output / "frozen"
    bundle.mkdir(mode=0o700)
    project_verify._write_bundle(bundle / "bundle", {f"helpers/{name}": data for name, data in inputs.items()})
    snapshot = {"schema": cli.SCHEMA, "base_commit": base, "candidate_commit": revision, "journey": primary,
                "doctor": doctor, "independent": alternate, "helper_digests": digests}
    cli.write(bundle / "contract.json", snapshot)
    output_report = {"schema": cli.SCHEMA, "base_commit": base, "candidate_commit": revision,
                     "journey": journey, "inputs_sha256": cli.digest(bundle / "contract.json"),
                     "outcome": "environment-blocked", "rounds": {}}
    total_deadline = time.monotonic() + timeout
    active = None

    def remaining():
        seconds = total_deadline - time.monotonic()
        if seconds < 1:
            raise ValueError("triage time limit expired")
        return seconds

    try:
        baseline_image = attester(repo, image, min(180, remaining()), commit=base)
        for side, commit in (("baseline", base), ("candidate", revision)):
            pinned = baseline_image if commit == base else derive(repo, base, commit, baseline_image, remaining())
            destination = output / side
            destination.mkdir(mode=0o700)
            active = project_verify.Verification(repo, commit, pinned, destination, remaining(), memory_mb, sandbox, attester)
            active.report["verified_image_id"] = pinned
            output_report["rounds"][side] = active.report
            active.report["observations"] = _probe(active, bundle / "bundle", doctor, primary, alternate, digests)
            cli.write(destination / "result.json", active.report)
        rounds = output_report["rounds"]
        if cli.digest(bundle / "contract.json") != output_report["inputs_sha256"]:
            raise ValueError("triage contract changed during verification")
        if any(cli.digest(bundle / "bundle" / "helpers" / name) != value for name, value in digests.items()):
            raise ValueError("triage helpers changed during verification")
        output_report["outcome"] = _classify(rounds["baseline"]["observations"], rounds["candidate"]["observations"],
                                               base != revision)
        output_report["comparison"] = "source-overlay" if base != revision else "same-image-replay"
    except project_verify.ERRORS as error:
        if active is not None:
            cli.write(active.output / "result.json", active.report)
        output_report["reason"] = str(error)
    cli.write(output / "result.json", output_report)
    return output_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "base", "revision", "image", "manifest", "helpers", "journey", "oracle",
                 "oracle-marker", "oracle-failure-marker", "out"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--memory-mb", type=int, default=3328)
    args = vars(parser.parse_args(argv))
    try:
        report = run(**args)
    except project_verify.ERRORS as error:
        parser.error(str(error))
    print(json.dumps({"outcome": report["outcome"], "report": str(Path(args["out"]).resolve() / "result.json")},
                     ensure_ascii=False))
    return int(report["outcome"] in ("environment-blocked", "inconclusive"))


if __name__ == "__main__":
    raise SystemExit(main())
