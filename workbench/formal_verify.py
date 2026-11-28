"""Create and maintain source-anchored property checks in offline Docker."""

import argparse
import difflib
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

from runner.sandbox import run_sandbox
from workbench import cli, project_triage, project_verify, refactor, workflow


CREATE = "skills/create-formal-verification-skill/SKILL.md"
MAINTAIN = "skills/maintain-formal-verification-skill/SKILL.md"
RESULT_PREFIX = "IBOARD_PROPERTY "
PROMISE = ("property", "harm", "bounds", "assumptions", "exclusions")


def _json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _load_json(path):
    return json.loads(refactor._read(path, 128_000))


def _lines(value, name):
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError(f"{name} must name one to sixteen items")
    for line in value:
        project_verify._text(line, name)


def _anchor(anchor):
    if not isinstance(anchor, dict) or set(anchor) != {"path", "contains", "meaning"}:
        raise ValueError("each source anchor needs a path, exact source text and meaning")
    project_verify._source_path(anchor["path"])
    project_verify._text(anchor["contains"], "anchor", 300)
    project_verify._text(anchor["meaning"], "anchor meaning")


def _spec(value):
    fields = {"id", "method", "tool_version", "checker", "replay", "minimum_explored",
              "source", "mutation", *PROMISE}
    if not isinstance(value, dict) or not fields <= set(value) or set(value) - fields - {"support", "replay_tool_version"}:
        raise ValueError("manifest must specify one property, scope, checker, source anchors and mutation")
    project_verify._slug(value["id"])
    for key in ("property", "harm", "tool_version"):
        project_verify._text(value[key], key)
    for key in ("bounds", "assumptions", "exclusions"):
        _lines(value[key], key)
    if value["method"] not in ("bounded-real-code", "model-check", "proof"):
        raise ValueError("declare bounded-real-code, model-check or proof")
    if type(value["minimum_explored"]) is not int or not 1 <= value["minimum_explored"] <= 10**12:
        raise ValueError("declare a minimum explored count from 1 to 10^12")
    checker, replay = value["checker"], value["replay"]
    required_drivers = [checker] if replay is None else [checker, replay]
    for name in required_drivers:
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,90}\.(mjs|py|sh)", name):
            raise ValueError("checker and optional replay must be standalone .mjs, .py or .sh files")
    if replay == checker:
        raise ValueError("an independent replay requires a separate driver")
    if "replay_tool_version" in value:
        if replay is None:
            raise ValueError("an independent tool version requires a replay driver")
        project_verify._text(value["replay_tool_version"], "replay tool version")
    support = value.get("support", [])
    if (not isinstance(support, list) or len(support) > 12
            or any(not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,90}\.[a-zA-Z0-9_-]{1,10}", name)
                   or name in (checker, replay, "counterexample.json") for name in support)
            or len(set(support)) != len(support)):
        raise ValueError("declare up to twelve distinct named support files")
    if not isinstance(value["source"], list) or not 1 <= len(value["source"]) <= 24:
        raise ValueError("declare one to twenty-four concrete source anchors")
    for anchor in value["source"]:
        _anchor(anchor)
    mutation = value["mutation"]
    if not isinstance(mutation, dict) or set(mutation) != {"path", "before", "after", "meaning"}:
        raise ValueError("declare a single real-code scratch mutation and what it breaks")
    if mutation["path"] not in {source["path"] for source in value["source"]}:
        raise ValueError("mutation must modify a covered source file")
    project_verify._text(mutation["meaning"], "mutation meaning")
    before, after = mutation["before"], mutation["after"]
    if (not isinstance(before, str) or not isinstance(after, str) or not 1 <= len(before) <= 2000
            or len(after) > 2000 or before == after or "\0" in before or "\0" in after):
        raise ValueError("mutation needs distinct exact source snippets")
    return value


def _drivers(spec, directory):
    return {name: project_verify._helper(directory, name)
            for name in (spec["checker"], spec["replay"], *spec.get("support", [])) if name is not None}


def _source(repo, revision, anchors):
    cache = {}
    result = []
    for anchor in anchors:
        oid, body, error = project_verify._read_blob(repo, revision, anchor["path"], cache)
        lines = [i for i, line in enumerate(body.splitlines(), 1) if anchor["contains"] in line]
        if error or len(lines) != 1:
            raise ValueError(f"source anchor is absent or ambiguous at {revision}: {anchor['path']} "
                             f"({anchor['contains']})")
        result.append({**anchor, "line": lines[0], "blob": oid})
    return result


def _history(repo, sync, revision, coverage, remaining):
    def git(*args):
        return cli.git(repo, *args, timeout=min(15, remaining()))

    if git("merge-base", sync, revision) != sync:
        raise ValueError("maintenance requires a descendant of the synchronized revision")
    log = git("rev-list", "--topo-order", "--reverse", "--max-count=129", revision, "^" + sync).splitlines()
    if len(log) > 128:
        raise ValueError("review at most 128 commits at once; choose an intermediate revision")
    commits = []
    for commit in log:
        raw = subprocess.run(
            ["git", "-C", str(repo), "diff-tree", "-r", "-m", "-M", "--no-commit-id", "--name-status",
             "-z", commit], capture_output=True, check=True, timeout=min(15, remaining())).stdout
        tokens = iter(raw.decode("utf-8").rstrip("\0").split("\0") if raw else [])
        changes = []
        for status in tokens:
            paths = [next(tokens, None) for _ in range(2 if status.startswith(("R", "C")) else 1)]
            if not all(paths) or not status:
                raise ValueError("Git did not retain the paths for a reviewed source change")
            entry = {"status": status, "paths": paths, "touches_coverage": bool(set(paths) & coverage)}
            if entry not in changes:
                changes.append(entry)
        commits.append({"commit": commit, "subject": git("show", "-s", "--format=%s", commit),
                        "changes": changes})
    return {"sync": sync, "revision": revision, "commits": commits}


def _review(repo, revision, history, original, proposed, path, manifest_digest, approved_digest):
    report = _load_json(path)
    history_digest = refactor._digest(_json_bytes(history))
    if (not isinstance(report, dict) or set(report) - {"history_sha256", "commits", "intent"}
            or report.get("history_sha256") != history_digest or not isinstance(report.get("commits"), dict)
            or set(report["commits"]) != {row["commit"] for row in history["commits"]}):
        raise ValueError("review must reference this history digest and account for every examined commit")
    for item in report["commits"].values():
        if (not isinstance(item, dict) or set(item) != {"effect", "reason"}
                or item["effect"] not in ("no-effect", "verification-drift", "intentional-contract-change",
                                          "possible-bug")):
            raise ValueError("each reviewed commit must have an effect and reason")
        project_verify._text(item["reason"], "commit review reason")
    delta = {key: {"before": original[key], "after": proposed[key]} for key in (*PROMISE, "minimum_explored", "method")
             if original[key] != proposed[key]}
    if delta:
        if not manifest_digest or approved_digest != manifest_digest or not report.get("intent"):
            raise ValueError("a property or coverage-bound change needs an approved manifest digest and intent anchor")
        if not any(item["effect"] == "intentional-contract-change" for item in report["commits"].values()):
            raise ValueError("an intentional contract change must be identified in the commit review")
        _anchor(report["intent"])
        report["intent"] = _source(repo, revision, [report["intent"]])[0]
        report["contract_changes"] = delta
    return report


def _outcome(raw, spec, *, replay=False):
    lines = [line[len(RESULT_PREFIX):] for line in raw["stdout"].splitlines() if line.startswith(RESULT_PREFIX)]
    if len(lines) != 1:
        raise ValueError("driver must emit exactly one structured property result")
    outcome = json.loads(lines[0])
    fields = {"status", "tool_version", "explored", "counterexample"}
    tool_version = spec.get("replay_tool_version", spec["tool_version"]) if replay else spec["tool_version"]
    if (not isinstance(outcome, dict) or set(outcome) != fields or outcome["tool_version"] != tool_version
            or type(outcome["explored"]) is not int or not 1 <= outcome["explored"] <= 10**12):
        raise ValueError("invalid property result or tool version")
    witness = outcome["counterexample"]
    if outcome["status"] == "holds" and witness == [] and raw["exit_code"] == 0:
        if not replay and outcome["explored"] < spec["minimum_explored"]:
            raise ValueError("checker did not meet the declared exploration bound")
    elif outcome["status"] == "violated" and raw["exit_code"] == 1:
        _lines(witness, "counterexample steps")
    else:
        raise ValueError("property status, counterexample and confirmed process exit disagree")
    return outcome


class Verification:
    def __init__(self, output, memory, remaining, sandbox):
        self.output = output
        self.memory = memory
        self.remaining = remaining
        self.sandbox = sandbox
        self.containers = set()
        self.results = []

    def drive(self, spec, drivers, label, commit, image, witness=None):
        name = spec["replay"] if witness else spec["checker"]
        command = [project_verify.ENGINES[Path(name).suffix], f"/workspace/{name}"]
        with TemporaryDirectory(prefix="iboard-property-driver-") as directory:
            scratch = Path(directory)
            (scratch / name).write_bytes(drivers[name])
            for support in spec.get("support", []):
                (scratch / support).write_bytes(drivers[support])
            if witness:
                (scratch / "counterexample.json").write_bytes(_json_bytes(witness))
                command.append("/workspace/counterexample.json")
            raw = self.sandbox(str(scratch), image, command, self.remaining(), memory_mb=self.memory,
                               workspace_read_only=True, source_root="/app")
        path = self.output / f"{label}.json"
        cli.write(path, {"commit": commit, "driver": name, "driver_sha256": refactor._digest(drivers[name]),
                         "support_sha256": {name: refactor._digest(drivers[name]) for name in spec.get("support", [])},
                         "witness": witness, "execution": raw})
        receipt = {"label": label, "report": path.name, "sha256": cli.digest(path)}
        self.results.append(receipt)
        refactor._observe(raw, command, image, self.memory, self.containers)
        receipt["result"] = _outcome(raw, spec, replay=witness is not None)
        return receipt["result"]


def _mutant(repo, revision, spec, image, engine, drivers, derive):
    mutation = spec["mutation"]

    def git(*args):
        return cli.git(repo, "-c", "core.hooksPath=/dev/null", *args, timeout=min(60, engine.remaining()))

    with TemporaryDirectory(prefix="iboard-property-mutation-") as directory:
        scratch = Path(directory).resolve() / "source"
        git("worktree", "add", "--detach", str(scratch), revision)
        try:
            path = scratch / mutation["path"]
            if path.is_symlink() or not path.is_file() or path.resolve() != path or path.stat().st_size > 1_000_000:
                raise ValueError("mutation target is not a regular covered source file")
            body = path.read_bytes()
            before, after = (mutation[key].encode() for key in ("before", "after"))
            if body.count(before) != 1:
                raise ValueError("real-code mutation target is absent or ambiguous")
            path.write_bytes(body.replace(before, after, 1))
            cli.git(scratch, "add", "--", mutation["path"], timeout=min(15, engine.remaining()))
            cli.git(scratch, "-c", "user.name=iBoard scratch", "-c", "user.email=iboard-scratch@example.invalid",
                    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                    "commit", "-qm", "Scratch property mutation", timeout=min(15, engine.remaining()))
            head = cli.sha(scratch, "HEAD", timeout=15)
            mutated = _image(derive(repo, revision, head, image, engine.remaining()))
            result = engine.drive(spec, drivers, "mutation", head, mutated)
            if result["status"] == "violated" and spec["replay"]:
                witness = result["counterexample"]
                replay = engine.drive(spec, drivers, "mutation-replay", head, mutated, witness=witness)
                if replay["status"] != "violated" or replay["counterexample"] != witness:
                    raise ValueError("the independent replay did not confirm the scratch counterexample")
        finally:
            cli.git(repo, "worktree", "remove", "--force", str(scratch), timeout=30)
    return result


def _image(value):
    if not refactor.IMAGE.fullmatch(value):
        raise ValueError("property verification requires an immutable local image")
    return value


def _skill(record, digest):
    spec = record["spec"]
    count_label = "proof obligations" if spec["method"] == "proof" else "explored cases"
    generated = [
        "---", f'name: verify-formal-{spec["id"]}',
        f'description: Run the source-anchored property check for {spec["id"]}.',
        "disable-model-invocation: true", "---", "",
        f'# Verify {spec["id"]}', "", spec["property"], "", f'User impact. {spec["harm"]}', "",
        f'Synchronized Git commit. `{record["sync"]}`', "",
        f'Method and tool version. `{spec["method"]}`, {spec["tool_version"]}. '
        f'Passing requires at least {spec["minimum_explored"]} {count_label}.', "",
    ]
    for key in ("bounds", "assumptions", "exclusions"):
        generated.extend([f"## {key.title()}", "", *(f"- {item}" for item in spec[key]), ""])
    generated.extend(["## Covered implementation", ""])
    generated.extend(f'- `{item["path"]}:{item["line"]}` — {item["meaning"]}'
                     for item in record["coverage"])
    generated.extend([
        "", "## Run the check", "",
        "From the iBoard checkout, set REPO to the target checkout, SKILL to this skill directory, and OUT "
        "to a new evidence directory outside both. The local image must match the synchronized Git commit.", "",
        "```sh",
        'python3 -m workbench.formal_verify check --repo "$REPO" --skill "$SKILL" --out "$OUT" '
        f'--skill-sha256 {digest} --revision {record["sync"]}',
        "```", "",
        "Read result.json and the linked raw receipt. Follow the counterexample against the actual source. "
        "A matching independent real-code replay confirms a bug. Without that replay, investigate the mismatch.",
        "", "## Maintain sources", "",
        "Run /agent-workbench:maintain-formal-verification-skill for newer commits. Review the sources, moves "
        "and new writers before updating the checker. Keep the published property and bounds unless a change "
        "is intentional and approved. Preserve old evidence and keep product fixes in a separate change.", "",
    ])
    if spec["method"] == "model-check":
        generated.extend(["Use the available statechart workflow to inspect a TLA+ trace when applicable.", ""])
    return "\n".join(generated).encode()


def _artifacts(record, drivers):
    raw = _json_bytes(record)
    digest = refactor._digest(raw)
    return {"bundle.json": raw, "SKILL.md": _skill(record, digest),
            **{f"helpers/{name}": content for name, content in drivers.items()}}


def _verification_diff(output, previous, current):
    chunks = []
    for change in project_verify._changes(previous, current):
        path = change["path"]
        chunks.extend(difflib.unified_diff(previous.get(path, b"").decode().splitlines(keepends=True),
                                           current.get(path, b"").decode().splitlines(keepends=True),
                                           fromfile=f"before/{path}", tofile=f"after/{path}"))
    if chunks:
        path = output / "verification.diff"
        path.write_text("".join(chunks), encoding="utf-8")
        return {"path": path.name, "sha256": cli.digest(path)}
    return None


def _installed(path, expected_digest):
    raw = refactor._read(path / "bundle.json", 128_000)
    if not refactor.SHA.fullmatch(expected_digest or "") or refactor._digest(raw) != expected_digest:
        raise ValueError("installed verification must match its approved bundle digest")
    record = json.loads(raw)
    if (not isinstance(record, dict) or set(record) != {"schema", "sync", "image", "spec", "coverage", "driver_sha256"}
            or type(record["schema"]) is not int or record["schema"] != 1):
        raise ValueError("installed verification has an invalid bundle schema")
    spec = _spec(record["spec"])
    drivers = _drivers(spec, path / "helpers")
    expected = _artifacts(record, drivers)
    if (not refactor.COMMIT.fullmatch(record["sync"]) or raw != expected["bundle.json"]
            or record["driver_sha256"] != {name: refactor._digest(body) for name, body in drivers.items()}
            or refactor._read(path / "SKILL.md", 128_000) != expected["SKILL.md"]):
        raise ValueError("installed verification bundle or drivers changed")
    return record, drivers


def _record_failure(report, engine, spec, drivers, commit, image, result):
    report["counterexample"] = result["counterexample"]
    if spec["replay"] is None:
        report["reason"] = "a potential violation needs an independent replay against real code"
        return
    if drivers[spec["replay"]] == drivers[spec["checker"]]:
        raise ValueError("independent replay must use a distinct implementation")
    witness = result["counterexample"]
    replay = engine.drive(spec, drivers, "counterexample-replay", commit, image, witness=witness)
    if replay["status"] == "violated" and replay["counterexample"] == witness:
        report.update(outcome="bug", reason="counterexample reproduced in the real implementation")
    else:
        report.update(outcome="review-required",
                      reason="the checker counterexample did not match the real-code replay")


def _resolve_prior_failure(report, engine, spec, drivers, commit, image, previous):
    if not previous or previous["status"] != "violated" or report.get("review", {}).get("contract_changes"):
        return True
    witness = previous["counterexample"]
    report["original_counterexample"] = witness
    if spec["replay"] is None:
        raise ValueError("a new passing checker cannot dismiss an earlier counterexample without a real-code replay")
    replay = engine.drive(spec, drivers, "as-installed-counterexample", commit, image, witness=witness)
    if replay["status"] == "violated" and replay["counterexample"] == witness:
        report.update(outcome="bug", counterexample=witness,
                      reason="the earlier counterexample still reproduces despite the replacement checker passing")
        return False
    if replay["status"] != "holds":
        raise ValueError("the old and new witnesses differ; the verification still needs review")
    report["verification_drift"] = "the as-installed counterexample did not occur in the independent real-code replay"
    return True


def run(mode, *, repo, out, revision="HEAD", image=None, manifest=None, helpers=None, skill=None,
        skill_sha256=None, review=None, approve_contract_change=None, seconds=900, memory_mb=2048,
        sandbox=run_sandbox, attest=workflow._verified_image, derive=project_triage._derive):
    if mode not in ("create", "check", "maintain") or bool(manifest) != bool(helpers):
        raise ValueError("use a supported mode and supply the property manifest with its helpers")
    repo = Path(repo).expanduser().resolve(strict=True)
    output = Path(out).expanduser().resolve()
    installed = Path(skill).expanduser().resolve(strict=True) if skill else None
    if output.is_relative_to(repo) or installed and output.is_relative_to(installed):
        raise ValueError("keep property evidence outside the target checkout and installed verification skill")
    output.mkdir(mode=0o700, parents=True)
    remaining = refactor._deadline(seconds)
    engine = Verification(output, refactor._memory(memory_mb), remaining, sandbox)
    report = {"schema": cli.SCHEMA, "mode": mode, "outcome": "blocked", "runs": engine.results,
              "pstack_sources": [CREATE, MAINTAIN]}
    try:
        head = cli.sha(repo, revision, timeout=min(15, remaining()))
        report["revision"] = head
        if mode == "create":
            prior, prior_drivers = None, {}
            old = None
            baseline = _image(attest(repo, project_triage._image_reference(image), min(300, remaining()), commit=head))
            tested = baseline
        else:
            prior, prior_drivers = _installed(installed, skill_sha256)
            old = prior["spec"]
            sync = prior["sync"]
            report["previous_sync"] = sync
            report["previous_bundle_sha256"] = skill_sha256
            report["installed_property"] = {"statement": old["property"], "coverage": prior["coverage"]}
            coverage = {anchor["path"] for anchor in old["source"]}
            history = _history(repo, sync, head, coverage, remaining)
            history_path = output / "history.json"
            history_path.write_bytes(_json_bytes(history))
            report["history"] = {"path": history_path.name, "sha256": cli.digest(history_path)}
            baseline = _image(prior["image"])
            tested = (_image(derive(repo, sync, head, baseline, remaining())) if sync != head else
                      _image(attest(repo, baseline, min(300, remaining()), commit=head)))
        report["image"] = tested
        before = None
        if prior:
            try:
                before = engine.drive(old, prior_drivers, "as-installed", head, tested)
            except (ValueError, OSError) as error:
                report["installed_error"] = str(error)
                if not history["commits"] and (mode == "check" or not (review and manifest)):
                    raise
            if history["commits"] and not review:
                report.update(outcome="review-required", reason="review commit coverage before moving the sync point")
            elif mode == "check":
                report["outcome"] = "in-sync" if before["status"] == "holds" else "blocked"
                if before["status"] == "violated":
                    _record_failure(report, engine, old, prior_drivers, head, tested, before)
        if mode != "check" and report["outcome"] != "review-required":
            manifest_bytes = refactor._read(Path(manifest), 128_000) if manifest else None
            spec = _spec(json.loads(manifest_bytes)) if manifest_bytes else old
            if prior and spec["id"] != old["id"]:
                raise ValueError("maintenance must retain the original property identifier")
            drivers = _drivers(spec, Path(helpers)) if helpers else prior_drivers
            if spec["replay"] and drivers[spec["checker"]] == drivers[spec["replay"]]:
                raise ValueError("independent replay must use a distinct implementation")
            if prior and (history["commits"] or manifest):
                if not review:
                    raise ValueError("provide the exact history review before changing a bundle")
                report["review"] = _review(repo, head, history, old, spec, Path(review),
                                           refactor._digest(manifest_bytes) if manifest else None,
                                           approve_contract_change)
            if prior and manifest:
                changes = project_verify._changes(prior_drivers, drivers)
                report["verification_changes"] = {"drivers": changes,
                                                  "fields": sorted(key for key in spec.keys() | old.keys()
                                                                   if spec.get(key) != old.get(key))}
            if not manifest and before is None:
                raise ValueError("as-installed verification could not run; supply a corrected bundle")
            anchors = _source(repo, head, spec["source"])
            report.update(property=spec["property"], scope={key: spec[key] for key in ("method", *PROMISE[2:])},
                          coverage=anchors)
            result = engine.drive(spec, drivers, "verification", head, tested) if manifest else before
            if result["status"] == "violated":
                _record_failure(report, engine, spec, drivers, head, tested, result)
                if before and before["status"] == "holds":
                    report["previous_pass_on_stale_verification"] = True
                if prior and report["outcome"] == "bug":
                    report["regression_window"] = {"synchronized": prior["sync"], "observed": head}
                    if (len(history["commits"]) == 1 and spec == old and drivers == prior_drivers):
                        report["introducing_commit"] = head
            elif _resolve_prior_failure(report, engine, old, prior_drivers, head, tested, before):
                negative = _mutant(repo, head, spec, tested, engine, drivers, derive)
                if negative["status"] != "violated":
                    raise ValueError("checker failed to detect the declared real-code mutation")
                report["mutation"] = {"meaning": spec["mutation"]["meaning"],
                                      "counterexample": negative["counterexample"]}
                final = engine.drive(spec, drivers, "unmodified-replay", head, tested)
                if final != result:
                    raise ValueError("unmodified verification did not reproduce after the scratch mutation")
                record = {"schema": 1, "sync": head, "image": tested, "spec": spec, "coverage": anchors,
                          "driver_sha256": {name: refactor._digest(content) for name, content in drivers.items()}}
                bundle = output / f'verify-formal-{spec["id"]}'
                artifacts = _artifacts(record, drivers)
                project_verify._write_bundle(bundle, artifacts)
                if prior:
                    report["verification_diff"] = _verification_diff(output, _artifacts(prior, prior_drivers),
                                                                     artifacts)
                changed = prior and (prior["sync"] != head or prior["spec"] != spec
                                     or prior["driver_sha256"] != record["driver_sha256"])
                report.update(outcome=("verified" if not prior else "updated" if changed else "in-sync"),
                              bundle=bundle.name, bundle_sha256=refactor._digest(artifacts["bundle.json"]),
                              sync=head)
    except refactor.ERRORS as error:
        report["reason"] = str(error)
    cli.write(output / "result.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    for mode in ("create", "check", "maintain"):
        sub = commands.add_parser(mode)
        for option in ("repo", "out"):
            sub.add_argument(f"--{option}", required=True)
        sub.add_argument("--revision", default="HEAD")
        sub.add_argument("--seconds", type=int, default=900)
        sub.add_argument("--memory-mb", type=int, default=2048)
        if mode == "create":
            sub.add_argument("--image", required=True)
        else:
            sub.add_argument("--skill", required=True)
            sub.add_argument("--skill-sha256", required=True)
        if mode != "check":
            sub.add_argument("--manifest", required=mode == "create")
            sub.add_argument("--helpers", required=mode == "create")
        if mode == "maintain":
            sub.add_argument("--review")
            sub.add_argument("--approve-contract-change")
    args = vars(parser.parse_args(argv))
    if bool(args.get("manifest")) != bool(args.get("helpers")):
        parser.error("provide both the property manifest and helpers")
    try:
        report = run(**args)
    except refactor.ERRORS as error:
        parser.error(str(error))
    print(json.dumps({"outcome": report["outcome"], "report": str(Path(args["out"]).resolve() / "result.json"),
                      "bundle_sha256": report.get("bundle_sha256")}))
    return 0 if report["outcome"] in ("verified", "updated", "in-sync") else 1


if __name__ == "__main__":
    raise SystemExit(main())
