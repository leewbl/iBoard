"""Compare checked workloads on pinned source in offline Docker."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
from statistics import median
import subprocess
from tempfile import TemporaryDirectory
import time

from runner.sandbox import run_sandbox
from workbench import cli, project_triage, project_verify, workflow
from workbench.verify_pr import _bounded_json


PSTACK_SOURCE = "skills/benchmark-checklist/SKILL.md"


def _number(value, low, high, label):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{label} is outside its measurement bounds")
    return value


def _helper_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+\.(mjs|py|sh)", value):
        raise ValueError("use a standalone .mjs, .py or .sh benchmark helper")
    return value


def _load(path):
    spec = _bounded_json(path, 64_000)
    required = {"claim", "scope", "configuration", "production_tuning", "minimum_delta_pct", "rounds", "workload"}
    if not isinstance(spec, dict) or required - spec.keys() or spec.keys() - required - {"profile", "end_to_end"}:
        raise ValueError("benchmark contract is missing fields or has unknown fields")
    for key in ("claim", "configuration"):
        project_verify._text(spec[key], key)
    if spec["scope"] not in ("end-to-end", "component"):
        raise ValueError("scope must be end-to-end or component")
    if spec["production_tuning"] is not None:
        project_verify._text(spec["production_tuning"], "production tuning evidence")
    if type(spec["rounds"]) is not int or not 5 <= spec["rounds"] <= 30:
        raise ValueError("run five to thirty alternating rounds")
    _number(spec["minimum_delta_pct"], 0.01, 100, "minimum material difference")
    if "profile" in spec:
        _helper_name(spec["profile"])
    if "end_to_end" in spec and spec["scope"] != "component":
        raise ValueError("a companion end-to-end workload belongs to a component benchmark")
    for lane in ("workload", "end_to_end"):
        if lane not in spec:
            continue
        job = spec[lane]
        fields = {"helper", "input", "completed", "digest", "max_ops_per_second", "plausibility"}
        if not isinstance(job, dict) or set(job) != fields:
            raise ValueError("each workload needs a helper, input contract, counts, digest and rate ceiling")
        _helper_name(job["helper"])
        for key in ("input", "plausibility"):
            project_verify._text(job[key], key)
        if type(job["completed"]) is not int or not 1 <= job["completed"] <= 10_000_000:
            raise ValueError("declare the expected number of completed operations")
        if not isinstance(job["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", job["digest"]):
            raise ValueError("declare the expected digest of the verified workload result")
        _number(job["max_ops_per_second"], 0.001, 1e12, "workload rate ceiling")
    if spec.get("profile") in {spec[lane]["helper"] for lane in ("workload", "end_to_end") if lane in spec}:
        raise ValueError("the separately profiled run must have its own helper")
    return spec


def _payload(report, kind):
    if (report.get("exit_code") != 0 or not report.get("container_created") or not report.get("cleanup_confirmed")
            or report.get("timed_out") or report.get("error") or report.get("stdout_truncated")
            or report.get("workspace_read_only") is not True):
        raise ValueError("sandbox did not complete with intact evidence and cleanup")
    records = []
    for line in report.get("stdout", "").splitlines():
        if line.startswith("{"):
            item = json.loads(line)
            if isinstance(item, dict) and item.get("kind") == kind:
                records.append(item)
    if len(records) != 1:
        raise ValueError(f"expected one {kind} record")
    return records[0]


def _measurement(raw, job, *, kind="iboard-benchmark"):
    result = _payload(raw, kind)
    if (result.get("unit") != "ms" or type(result.get("completed")) is not int
            or result["completed"] != job["completed"] or type(result.get("errors")) is not int
            or result["errors"] != 0 or result.get("digest") != job["digest"]):
        raise ValueError("work count, errors, unit or output digest did not match the contract")
    actual = _number(result.get("elapsed_ms"), 0.00001, 3_600_000, "elapsed time")
    wall = _number(raw.get("elapsed_seconds"), 0, 3615, "sandbox wall time") * 1000
    if actual > wall + 2 or job["completed"] * 1000 / actual > job["max_ops_per_second"]:
        raise ValueError("reported measurement exceeded the wall time or agreed rate ceiling")
    return result


def _compare(timings, threshold):
    base, candidate = timings["baseline"], timings["candidate"]
    baseline, head = median(base), median(candidate)
    gap = 100 * (baseline - head) / baseline
    faster = gap >= threshold and max(candidate) < min(base)
    slower = gap <= -threshold and min(candidate) > max(base)
    return {"baseline": {"median_ms": baseline, "range_ms": [min(base), max(base)]},
            "candidate": {"median_ms": head, "range_ms": [min(candidate), max(candidate)]},
            "delta_pct": gap, "rounds_per_side": len(base),
            "observed": "faster" if faster else "slower" if slower else "no-measurable-difference"}


def _profile(raw, job, repo, commit):
    result = _measurement(raw, job, kind="iboard-benchmark-profile")
    limiter = project_verify._text(result.get("limiter"), "limiter")
    hotspot = result.get("hotspot")
    if not isinstance(hotspot, dict) or set(hotspot) != {"path", "contains"}:
        raise ValueError("profile needs a source-backed hotspot")
    project_verify._source_path(hotspot["path"])
    project_verify._text(hotspot["contains"], "profile source anchor", 250)
    atlas = {}
    location = project_verify._source(
        lambda path: project_verify._read_blob(repo, commit, path, atlas), {"source": [hotspot]})[0]
    if not location["line"]:
        raise ValueError("profile hotspot was not found in the measured commit")
    observations = result.get("observations")
    if not isinstance(observations, list) or not 1 <= len(observations) <= 12:
        raise ValueError("profile needs measured limiter observations")
    for item in observations:
        if not isinstance(item, dict) or set(item) != {"metric", "value", "unit"}:
            raise ValueError("profile observations need metric, value and unit")
        project_verify._text(item["metric"], "profile metric", 150)
        project_verify._text(item["unit"], "profile unit", 40)
        _number(item["value"], 0, 1e14, "profile observation")
    return {"limiter": limiter, "source": location, "observations": observations}


def run(*, repo, base, revision, image, manifest, helpers, out, timeout=600, memory_mb=1024,
        sandbox=run_sandbox, attester=workflow._verified_image, derive=project_triage._derive):
    repo = Path(repo).expanduser().resolve(strict=True)
    commits = {"baseline": cli.sha(repo, base, timeout=10), "candidate": cli.sha(repo, revision, timeout=10)}
    changed = commits["baseline"] != commits["candidate"]
    if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", *commits.values()],
                      capture_output=True, timeout=10).returncode:
        raise ValueError("candidate must descend from the baseline")
    _number(timeout, 1, 3600, "total time")
    if type(memory_mb) is not int or not 128 <= memory_mb <= 8192:
        raise ValueError("memory must be 128–8192 MiB")
    project_triage._image_reference(image)
    spec = _load(Path(manifest))
    output = Path(out).expanduser().resolve()
    if output.is_relative_to(repo):
        raise ValueError("keep benchmark evidence outside the target checkout")
    source = Path(helpers).expanduser().resolve(strict=True)
    scripts = {spec[key]["helper"] for key in ("workload", "end_to_end") if key in spec}
    if "profile" in spec:
        scripts.add(spec["profile"])
    frozen = {name: project_verify._helper(source, name) for name in scripts}
    content = {name: hashlib.sha256(data).hexdigest() for name, data in frozen.items()}
    output.mkdir(mode=0o700, parents=True)
    evidence = output / "frozen"
    evidence.mkdir(mode=0o700)
    for name, data in frozen.items():
        (evidence / name).write_bytes(data)
    plan = evidence / "contract.json"
    cli.write(plan, {**spec, "schema": cli.SCHEMA, "commits": commits, "helpers": content,
                     "memory_mb": memory_mb, "cpu_limit": 2})
    result = {"schema": cli.SCHEMA, "outcome": "inconclusive", "measurement_scope": spec["scope"],
              "claim": spec["claim"], "commits": commits, "images": {}, "samples": [], "profiles": {},
              "contract_sha256": cli.digest(plan), "host_logical_cpus": os.cpu_count(),
              "host_load_before": os.getloadavg(), "order": "baseline,candidate", "issues": []}
    deadline = time.monotonic() + timeout

    def remaining():
        left = deadline - time.monotonic()
        if left < 1:
            raise ValueError("benchmark time limit expired")
        return left

    def launch(label, side, name):
        pinned = result["images"][side]
        argv = [project_verify.ENGINES[Path(name).suffix], f"/workspace/{name}"]
        with TemporaryDirectory(prefix="iboard-benchmark-") as path:
            (Path(path) / name).write_bytes(frozen[name])
            raw = sandbox(path, pinned, argv, remaining(), memory_mb=memory_mb, workspace_read_only=True,
                          source_root="/app")
        raw.update(schema=cli.SCHEMA, commit=commits[side], helper_sha256=content[name])
        path = output / f"{label}-{side}.json"
        cli.write(path, raw)
        if raw.get("image") != pinned or raw.get("command") != argv:
            raise ValueError("sandbox evidence belongs to a different image or helper invocation")
        return raw, {"side": side, "report": path.name, "sha256": cli.digest(path)}

    try:
        known = attester(repo, image, min(180, remaining()), commit=commits["baseline"])
        result["images"]["baseline"] = known
        result["images"]["candidate"] = derive(repo, commits["baseline"], commits["candidate"], known, remaining()) \
            if changed else known
        sides = ("baseline", "candidate")
        result["comparisons"] = {}
        for lane in ("workload", "end_to_end"):
            if lane not in spec:
                continue
            job = spec[lane]
            timings = {side: [] for side in sides}
            for round_no in range(1, spec["rounds"] + 1):
                for side in sides:
                    label = f"{lane}-{round_no}"
                    raw, item = launch(label, side, job["helper"])
                    result["samples"].append({**item, "round": round_no, "lane": lane})
                    measured = _measurement(raw, job)
                    result["samples"][-1]["measurement"] = measured
                    timings[side].append(measured["elapsed_ms"])
            result["comparisons"][lane] = _compare(timings, spec["minimum_delta_pct"])
        if "profile" in spec:
            for side in sides:
                raw, item = launch("profile", side, spec["profile"])
                result["profiles"][side] = item
                item.update(_profile(raw, spec["workload"], repo, commits[side]))
        comparison = result["comparisons"]["workload"]
        observed = comparison["observed"]
        if observed in ("faster", "slower"):
            if not changed:
                result["issues"].append("same-commit noise control produced a directional difference")
            if spec["production_tuning"] is None:
                result["issues"].append("production configuration evidence is missing")
            if not result["profiles"]:
                result["issues"].append("separate measured limiter evidence is missing")
            if spec["scope"] == "component" and "end_to_end" not in result["comparisons"]:
                result["issues"].append("matching end-to-end measurement is missing")
        if cli.digest(plan) != result["contract_sha256"] or any(cli.digest(evidence / name) != value
                                                                 for name, value in content.items()):
            result["issues"].append("frozen workload artifacts changed")
        if not result["issues"]:
            result["outcome"] = observed
    except project_verify.ERRORS as error:
        result["issues"].append(str(error))
    result["host_load_after"] = os.getloadavg()
    cli.write(output / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "base", "revision", "image", "manifest", "helpers", "out"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--memory-mb", type=int, default=1024)
    args = vars(parser.parse_args(argv))
    try:
        report = run(**args)
    except project_verify.ERRORS as error:
        parser.error(str(error))
    print(json.dumps({"outcome": report["outcome"], "report": str(Path(args["out"]).resolve() / "result.json")},
                     ensure_ascii=False))
    return int(report["outcome"] == "inconclusive")


if __name__ == "__main__":
    raise SystemExit(main())
