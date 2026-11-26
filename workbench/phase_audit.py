"""Audit an authorized multi-PR plan and its retained stage evidence without dispatching work."""

import argparse
from datetime import datetime
from pathlib import Path
from statistics import median
import json
import re

from workbench import architect, benchmark, cli, investigate, multiphase
from workbench.prompt_profiles import PSTACK_COMMIT
from workbench.verify_pr import _bounded_json


def _receipt(reference, directory):
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ValueError("each receipt needs a path and expected SHA-256")
    path = (directory / reference["path"]).resolve(strict=True)
    if cli.digest(path) != reference["sha256"]:
        raise ValueError("receipt does not match the authorized digest")
    raw = _bounded_json(path, 2_600_000)
    if not isinstance(raw, dict) or raw.get("schema") != cli.SCHEMA:
        raise ValueError("unsupported evidence report")
    return path, raw


def _execution(raw, commit, image, marker):
    if (raw.get("commit") != commit or raw.get("image") != image or raw.get("exit_code") != 0
            or raw.get("container_created") is not True
            or raw.get("cleanup_confirmed") is not True or raw.get("workspace_read_only") is not True
            or raw.get("timed_out") or raw.get("error") or raw.get("stdout_truncated")
            or marker not in raw.get("stdout", "").splitlines()):
        raise ValueError("test result lacks a passing offline run on the exact candidate")


def _samples(path, score, base, head, probe):
    if (score.get("commits") != {"baseline": base, "candidate": head}
            or score.get("issues") or score.get("outcome") not in ("faster", "slower", "no-measurable-difference")
            or score.get("order") != "baseline,candidate"):
        raise ValueError("performance comparison is inconclusive or uses a different trunk/candidate")
    frozen = path.parent / "frozen"
    contract_path = frozen / "contract.json"
    if cli.digest(contract_path) != score.get("contract_sha256"):
        raise ValueError("frozen benchmark contract changed")
    contract = cli.read(contract_path)
    if (contract.get("claim") != probe or contract.get("commits") != score["commits"]
            or sorted(score.get("images", {})) != ["baseline", "candidate"]
            or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in score["images"].values())):
        raise ValueError("benchmark report did not execute the planned performance probe")
    if not 5 <= contract["rounds"] <= 30:
        raise ValueError("benchmark has too few or excessive paired rounds")
    normalized = {key: value for key, value in contract.items() if key != "commits"}
    job = contract["workload"]
    measurements = [entry for entry in score["samples"] if entry["lane"] == "workload"]
    expected = [(index, name) for index in range(1, contract["rounds"] + 1)
                for name in ("baseline", "candidate")]
    if [(entry["round"], entry["side"]) for entry in measurements] != expected:
        raise ValueError("performance samples are incomplete or out of order")
    digest = cli.digest(frozen / benchmark._helper_name(job["helper"]))
    if digest != contract["helpers"][job["helper"]]:
        raise ValueError("performance probe helper changed")
    values, times = {"baseline": [], "candidate": []}, []
    names = set()
    for entry in measurements:
        name = entry["report"]
        if Path(name).name != name or name in names:
            raise ValueError("performance sample path is not unique within its evidence directory")
        names.add(name)
        sample, raw = _receipt({"path": name, "sha256": entry["sha256"]}, path.parent)
        side = entry["side"]
        if (sample.parent != path.parent or raw.get("commit") != score["commits"][side]
                or raw.get("image") != score["images"][side] or raw.get("helper_sha256") != digest):
            raise ValueError("performance sample is for a different invocation or source")
        measured = benchmark._measurement(raw, job)
        if measured != entry.get("measurement"):
            raise ValueError("performance summary differs from raw execution")
        times.append(datetime.fromisoformat(raw["started_at"]))
        values[side].append(measured["elapsed_ms"])
    if times != sorted(times):
        raise ValueError("performance run times do not match the declared order")
    return normalized, median(values["candidate"]), times


def _performance(before_ref, after_ref, origin, phase, baseline, commit):
    before_path, before = _receipt(before_ref, origin)
    after_path, after = _receipt(after_ref, origin)
    check = phase["verify"]["perf"]
    initial, _, original_times = _samples(before_path, before, baseline, baseline, check["probe"])
    later, measured, later_times = _samples(after_path, after, baseline, commit, check["probe"])
    if initial != later or max(original_times) >= min(later_times):
        raise ValueError("retain an earlier trunk measurement made with the same frozen probe")
    if measured > check["max_ms"]:
        raise ValueError(f"candidate median {measured:.3f} ms exceeds {check['max_ms']} ms")
    if before["images"]["baseline"] != after["images"]["baseline"]:
        raise ValueError("trunk image changed between baseline and candidate measurements")
    return measured, after["images"]["candidate"]


def _phase(repo, entry, record, baseline, parent, origin):
    fields = {"commit", "unit", "live", "baseline_perf", "perf"}
    if not isinstance(record, dict) or set(record) != fields:
        raise ValueError("phase needs a commit, unit, live, baseline_perf and perf evidence")
    head = investigate._git(repo, "rev-parse", "--verify", "--end-of-options", record["commit"] + "^{commit}").decode().strip()
    if head != record["commit"] or head == parent:
        raise ValueError("phase must name its full distinct candidate commit")
    investigate._git(repo, "merge-base", "--is-ancestor", parent, head)
    changed = {path for path in investigate._git(repo, "diff", "--name-only", "-z", "--no-renames",
                                                   parent, head, "--").decode().split("\0") if path}
    if not changed or not changed <= set(entry["files"]):
        raise ValueError("candidate diff exceeds the phase scope or has no changes")
    unit_path, unit = _receipt(record["unit"], origin)
    live_path, live = _receipt(record["live"], origin)
    if unit_path == live_path or record["unit"]["sha256"] == record["live"]["sha256"]:
        raise ValueError("unit and live checks require distinct receipts")
    perf, image = _performance(record["baseline_perf"], record["perf"], origin, entry, baseline, head)
    _execution(unit, head, image, entry["verify"]["unit"]["marker"])
    _execution(live, head, image, entry["verify"]["live"]["marker"])
    return {"commit": head, "changed": sorted(changed), "median_ms": perf,
            "status": "awaiting-operator-review" if entry["interaction"] else "evidence-verified"}


def audit(*, repo, plan, plan_sha256, receipts, out):
    repo = Path(repo).expanduser().resolve(strict=True)
    plan = Path(plan).expanduser().resolve(strict=True)
    if cli.digest(plan) != plan_sha256:
        raise ValueError("approved plan digest did not match")
    contract = _bounded_json(plan, 180_000)
    if (not isinstance(contract, dict) or contract.get("schema") != cli.SCHEMA
            or contract.get("pstack_commit") != PSTACK_COMMIT or contract.get("status") != "planned"):
        raise ValueError("readiness audit requires a completed planning artifact")
    multiphase._validate({"summary": contract["summary"], "phases": contract["phases"]},
                         set(contract["scope"]), contract["sources"])
    baseline = contract["baseline"]
    investigate._git(repo, "cat-file", "-e", baseline + "^{commit}")
    receipts = Path(receipts).expanduser().resolve(strict=True)
    receipt_digest = cli.digest(receipts)
    records = _bounded_json(receipts, 100_000)
    if (not isinstance(records, dict) or set(records) != {"plan_sha256", "phases"}
            or records.get("plan_sha256") != plan_sha256 or not isinstance(records["phases"], dict)
            or set(records["phases"]) - {entry["id"] for entry in contract["phases"]}):
        raise ValueError("receipt index must bind to the approved plan and declared phases")
    destination = architect._output(repo, out)
    rows = {}
    for entry in contract["phases"]:
        key = entry["id"]
        waits = [dependency for dependency in entry["depends_on"]
                 if rows[dependency]["status"] != "evidence-verified"]
        if waits:
            rows[key] = {"status": "waiting-dependencies", "waits": waits}
        elif key not in records["phases"]:
            rows[key] = {"status": "evidence-missing"}
        else:
            parent = baseline if entry["base"] == "trunk" else rows[entry["base"]]["commit"]
            try:
                rows[key] = _phase(repo, entry, records["phases"][key], baseline, parent, receipts.parent)
            except multiphase.ERRORS as error:
                rows[key] = {"status": "blocked", "reason": str(error)[:750]}
    if cli.digest(plan) != plan_sha256 or cli.digest(receipts) != receipt_digest:
        raise ValueError("plan or receipt index changed during audit")
    report = {"schema": cli.SCHEMA, "plan_sha256": plan_sha256, "receipt_index_sha256": receipt_digest,
              "baseline": baseline, "phases": rows, "authorization": "audit-only"}
    cli.write(destination / "result.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "plan", "plan-sha256", "receipts", "out"):
        parser.add_argument("--" + name, required=True)
    args = vars(parser.parse_args(argv))
    try:
        report = audit(**args)
    except multiphase.ERRORS as error:
        parser.error(str(error))
    print(json.dumps({"report": str(Path(args["out"]).resolve() / "result.json"),
                      "phases": {key: value["status"] for key, value in report["phases"].items()}}))
    return int(any(row["status"] != "evidence-verified" for row in report["phases"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
