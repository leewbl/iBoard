"""Bind a verified maintenance correction to the ordinary iBoard PR gate."""

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from tempfile import TemporaryDirectory

from runner.sandbox import MAX_OUTPUT, run_sandbox
from workbench import cli, project_verify, skill_sources, workflow
from workbench.automate_me import SHA, _digest, _file, _keep, _read, _require
from workbench.task_lock import locked_task


KIND = "project-verification-maintenance"
FIELD = "project_verification_maintenance"
MAINTENANCE = "maintenance/result.json"
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
ERRORS = (*project_verify.ERRORS, KeyError, TypeError, AttributeError)


def _git_bytes(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, timeout=20).stdout


def _scope(repo, base, head, root):
    project_verify._source_path(root)
    _require(re.fullmatch(r"verify-[a-z][a-z0-9-]{0,55}", PurePosixPath(root).name),
             "select one project-local verify-* skill directory")
    cli.git(repo, "merge-base", "--is-ancestor", base, head, timeout=20)
    commits = cli.git(repo, "rev-list", head, "^" + base, timeout=20).splitlines()
    _require(commits, "no committed skill correction")
    prefix = (root + "/").encode("utf-8")
    for commit in commits:
        paths = _git_bytes(repo, "diff-tree", "-r", "--no-commit-id", "--name-only",
                           "--no-renames", "--ignore-submodules=none", "-z", base, commit)
        _require(all(path.startswith(prefix) for path in paths.split(b"\0") if path),
                 "maintenance candidates may change only the selected skill in every new commit")


def _committed_bundle(repo, revision, root):
    tree = _git_bytes(repo, "ls-tree", "-rz", "--full-tree", revision, "--", root)
    files, modes = {}, {}
    with skill_sources._blobs(repo) as read:
        for row in tree.split(b"\0"):
            if not row:
                continue
            header, path = row.decode("utf-8").split("\t", 1)
            mode, kind, oid = header.split()
            _require(kind == "blob" and mode in skill_sources.REGULAR, "committed skill files must be regular files")
            name = path.removeprefix(root + "/")
            _require(path.startswith(root + "/"), "invalid committed skill path")
            project_verify._source_path(name)
            files[name] = read(oid).encode("utf-8")
            modes[name] = mode
    _require("contract.json" in files and "SKILL.md" in files, "choose an existing generated verification skill")
    return files, modes


def _snapshots(repo, base, head, root, directory):
    _scope(repo, base, head, root)
    previous, old_modes = _committed_bundle(repo, base, root)
    candidate, new_modes = _committed_bundle(repo, head, root)
    changes = project_verify._changes(previous, candidate)
    repaired_modes = sorted(name for name in previous if name.startswith("helpers/")
                            and old_modes[name] != "100755")
    _require(changes or repaired_modes, "no committed skill correction")
    old, new = directory / "previous", directory / "candidate"
    project_verify._write_bundle(old, previous)
    for name in repaired_modes:
        (old / name).chmod(0o644)
    project_verify._write_bundle(new, candidate)
    contract = project_verify._load_contract(new / "contract.json")
    _require(PurePosixPath(root).name == "verify-" + contract["project"],
             "skill directory and verification contract project differ")
    expected = project_verify._artifacts(contract, new / "helpers")
    _require(candidate == expected and all(
        mode == ("100755" if name.startswith("helpers/") else "100644") for name, mode in new_modes.items()),
        "candidate must match the generated bundle and executable helper modes")
    sources = [anchor["path"] for check in [contract["doctor"], *contract["features"]] for anchor in check["source"]]
    sources += [binding["path"] for scope in contract.get("entrypoint_inventory", []) for binding in scope["sources"]]
    _require(not any(path == root or path.startswith(root + "/") for path in sources),
             "verification source anchors and entrypoints must be outside the edited skill")
    return old, new, contract, changes, repaired_modes


def _checked_runs(directory, report, contract, revision, bundle):
    checks = [contract["doctor"], *contract["features"]]
    image = report["verified_image_id"]
    _require(IMAGE.fullmatch(image) and report["features"] == {c["id"]: "passed" for c in contract["features"]},
             "maintenance requires all declared journeys on an immutable image")
    _require(len(report["runs"]) == len(checks), "maintenance requires one successful run per declared check")
    containers = set()
    for check, entry in zip(checks, report["runs"]):
        name = check["id"]
        filename = name + ".json"
        _require(entry == {"check": name, "report": filename, "sha256": entry["sha256"],
                           "passed": True, "cleanup_confirmed": True}, "maintenance run index differs")
        run = json.loads(_read(directory / filename, 12 * MAX_OUTPUT + 512_000, entry["sha256"]))
        container = run["create_stdout"].strip()
        _require(run["schema"] == cli.SCHEMA and run["commit"] == revision and run["check"] == name
                 and run["helper_sha256"] == cli.digest(bundle / "helpers" / check["helper"])
                 and run["expected_marker"] == check["marker"] and run["source_root"] == "/app"
                 and run["passed"] is True and run["timed_out"] is False and SHA.fullmatch(container)
                 and not run.get("create_stdout_truncated") and container not in containers,
                 f"maintenance run is not a fresh execution of the committed helper: {name}")
        for stream in ("stdout", "stderr"):
            _require(not run.get(stream + "_truncated") and
                     _digest(run[stream].encode("utf-8")) == run.get(stream + "_sha256"),
                     f"incomplete maintenance output: {name}/{stream}")
        screenshot = _file(directory / (name + ".png")) if check.get("screenshot") else None
        recording = _file(directory / (name + ".jsonl")) if check.get("recording") else None
        _require(project_verify._run_passed(run, check, image, revision, screenshot, recording),
                 f"maintenance run did not prove the declared journey: {name}")
        containers.add(container)
    return len(checks)


def check(*, repo, base, candidate, skill, image, out, timeout=600, memory_mb=3328,
          sandbox=run_sandbox, attester=workflow._verified_image):
    repo = Path(repo).expanduser().resolve(strict=True)
    base, head = (cli.sha(repo, ref, timeout=20) for ref in (base, candidate))
    output = Path(out).expanduser().resolve()
    _require(not output.is_relative_to(repo), "keep maintenance evidence outside the worktree")
    with TemporaryDirectory(prefix="iboard-verify-delivery-") as scratch:
        old, new, contract, _, _ = _snapshots(repo, base, head, skill, Path(scratch))
        output.mkdir(mode=0o700, parents=True)
        directory = output / "maintenance"
        report = project_verify.run("maintain", repo=repo, revision=base, image=image, out=directory,
                                    timeout=timeout, memory_mb=memory_mb, manifest=new / "contract.json",
                                    helpers=new / "helpers", skill=old, sandbox=sandbox, attester=attester)
        reason = None
        if report["outcome"] == "changed":
            try:
                _checked_runs(directory, report, contract, base, new)
            except ERRORS as error:
                reason = str(error)
        else:
            reason = report.get("reason", "no verified correction was produced")
    status = "failed" if reason else "passed"
    proof = {"schema": cli.SCHEMA, "kind": KIND, "status": status, "base": base, "candidate": head, "skill": skill,
             "maintenance": {"path": MAINTENANCE, "sha256": cli.digest(output / MAINTENANCE)}}
    if reason:
        proof["reason"] = reason
    ref = _keep(output / "receipt.json", proof)
    return {**ref, "status": status}


def require(data, head):
    """Read the exact committed candidate, and recheck each proof before delivery."""
    try:
        ref = data[FIELD]
        path = _file(ref["path"])
        _require(set(ref) == {"path", "sha256"} and str(path) == ref["path"] and SHA.fullmatch(ref["sha256"]),
                 "maintenance proof needs a stable path and digest")
        proof = json.loads(_read(path, 24_000, ref["sha256"]))
        base, root = data["base_commit"], proof["skill"]
        _require((proof["schema"], proof["kind"], proof["status"], proof["base"], proof["candidate"])
                 == (cli.SCHEMA, KIND, "passed", base, head),
                 "maintenance proof must pass for the exact task baseline and candidate")
        nested = proof["maintenance"]
        _require(nested["path"] == MAINTENANCE, "unexpected maintenance report path")
        report_path = path.parent / MAINTENANCE
        report = json.loads(_read(report_path, 2_500_000, nested["sha256"]))
        _require((report["schema"], report["mode"], report["outcome"], report["commit"])
                 == (cli.SCHEMA, "maintain", "changed", base), "only a proven maintenance correction can ship")
        repo = Path(data["repo"]).resolve(strict=True)
        with TemporaryDirectory(prefix="iboard-verify-proof-") as scratch:
            _, candidate, contract, changes, repaired_modes = _snapshots(repo, base, head, root, Path(scratch))
            _require(report["changes"] == changes and report["replaces_unsafe_entries"] == repaired_modes,
                     "maintenance changes differ from the selected committed skill correction")
            source, drift, inventory = project_verify._source_coverage(repo, base, contract)
            _require(not drift and not project_verify._entrypoint_gaps(inventory)
                     and report["source"] == source and report["source_drift"] == drift
                     and report.get("entrypoints") == (inventory if inventory["scope_count"] else None),
                     "maintenance source or entrypoint coverage differs")
            runs = _checked_runs(report_path.parent, report, contract, base, candidate)
        return {"sha256": ref["sha256"], "skill": root, "runs": runs}
    except ERRORS as error:
        raise cli.WorkbenchError(f"verification maintenance evidence: {error}") from error


@locked_task("task_path")
def attach(task_path, receipt, sha256):
    task = Path(task_path).expanduser().resolve(strict=True)
    data = cli.read(task)
    ref = {"path": str(_file(receipt)), "sha256": sha256}
    _require(data.get(FIELD, ref) == ref, "task already binds a different maintenance correction")
    data[FIELD] = ref
    result = require(data, cli.sha(data["repo"], "HEAD", timeout=20))
    cli.write(task, data)
    return result


@locked_task("task_path")
def publish_checked(task_path, receipt, sha256, repository, github=None):
    from workbench import publish

    attach(task_path, receipt, sha256)
    return publish.publish(task_path, repository, github=github)


def main(argv=None):
    from workbench.delivery import DeliveryError
    from workbench.publish import PublishError

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    verify = commands.add_parser("check")
    for key in ("repo", "base", "candidate", "skill", "image", "out"):
        verify.add_argument("--" + key, required=True)
    verify.add_argument("--timeout", type=int, default=600)
    verify.add_argument("--memory-mb", type=int, default=3328)
    for name in ("attach", "publish"):
        action = commands.add_parser(name)
        action.add_argument("--task", required=True, dest="task_path")
        action.add_argument("--receipt", required=True)
        action.add_argument("--sha256", required=True)
        if name == "publish":
            action.add_argument("--repository", required=True)
    args = vars(parser.parse_args(argv))
    action = args.pop("action")
    try:
        result = {"check": check, "attach": attach, "publish": publish_checked}[action](**args)
    except ERRORS + (DeliveryError, PublishError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(action == "check" and result["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
