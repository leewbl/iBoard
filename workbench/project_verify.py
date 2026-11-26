"""Generate, run and maintain project-local verification skills in offline Docker."""

import argparse
import difflib
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from tempfile import TemporaryDirectory
import time

from runner.sandbox import SandboxError, run_sandbox
from workbench import cli, project_surface, workflow
from workbench.verify_pr import VerifyError, _bounded_json


PSTACK_COMMANDS = {
    "skills/create-verification-skill/SKILL.md": "create",
    "skills/maintain-verification-skill/SKILL.md": "maintain",
}
ENGINES = {".mjs": "node", ".py": "python3", ".sh": "sh"}
ERRORS = (OSError, ValueError, cli.WorkbenchError, workflow.WorkflowError, SandboxError, VerifyError,
          subprocess.SubprocessError)


def _text(value, name, limit=1500):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 for char in value) or "`" in value):
        raise ValueError(f"{name} must be a nonempty line of text")
    return value


def _slug(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,55}", value):
        raise ValueError(f"invalid verification identifier: {value!r}")
    return value


def _source_path(value):
    path = PurePosixPath(_text(value, "source path", 250))
    if (path.is_absolute() or str(path) != value or not re.fullmatch(r"[a-zA-Z0-9./_+@-]+", value)
            or any(part in ("..", ".git") for part in path.parts)):
        raise ValueError(f"invalid repository-relative source path: {value!r}")
    return value


def _load_contract(path):
    raw = _bounded_json(Path(path), 90_000)
    required = {"project", "surface", "launch", "cleanup", "doctor", "features"}
    if not isinstance(raw, dict) or set(raw) - required - {"entrypoint_inventory"} or not required <= set(raw):
        raise ValueError("verification contract needs project, surface, launch, cleanup, doctor and features")
    _slug(raw["project"])
    for key in ("surface", "launch", "cleanup"):
        _text(raw[key], key)
    features = raw["features"]
    if not isinstance(features, list) or not 1 <= len(features) <= 20:
        raise ValueError("declare one to twenty mapped features")
    ids, markers = set(), set()
    for check in [raw["doctor"], *features]:
        fields = {"id", "helper", "marker", "reach", "drive", "observe", "gotchas", "source"}
        if not isinstance(check, dict) or set(check) - fields - {"screenshot", "entrypoints"} or not fields <= set(check):
            raise ValueError("each check needs id, helper, marker, reach, drive, observe, gotchas and source")
        check_id = _slug(check["id"])
        marker = _text(check["marker"], "marker", 100)
        if check_id in ids or marker in markers or not re.fullmatch(r"[A-Z][A-Z0-9_]+_PASS", marker):
            raise ValueError("checks need distinct identifiers and standalone uppercase pass markers")
        ids.add(check_id)
        markers.add(marker)
        helper = _text(check["helper"], "helper", 100)
        if (not re.fullmatch(r"[a-zA-Z0-9_-]+\.(mjs|py|sh)", helper)
                or type(check.get("screenshot", False)) is not bool):
            raise ValueError("helpers must be standalone .mjs, .py or .sh files")
        for key in ("reach", "drive", "observe", "gotchas"):
            _text(check[key], key)
        sources = check["source"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= 6:
            raise ValueError("each check needs one to six concrete source anchors")
        for source in sources:
            if not isinstance(source, dict) or set(source) != {"path", "contains"}:
                raise ValueError("a source anchor needs path and contains")
            _source_path(source["path"])
            _text(source["contains"], "source anchor", 250)
    if "entrypoints" in raw["doctor"]:
        raise ValueError("user entrypoints must be named on feature journeys")
    project_surface.validate(raw, _source_path, _text)
    return raw


def _helper(directory, name):
    path = directory / name
    if directory.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size > 128_000:
        raise ValueError(f"missing, oversized or symbolic helper: {name}")
    data = path.read_bytes()
    if not data.strip() or b"\0" in data:
        raise ValueError(f"helper must be executable text: {name}")
    data.decode("utf-8")
    return data


def _artifacts(contract, helpers):
    slug, doctor = contract["project"], contract["doctor"]
    maps = {}
    for feature in contract["features"]:
        key = feature["id"]
        commands = "\n".join(f"- `{command}`" for command in feature.get("entrypoints", []))
        entrypoints = f"\n{commands}\n" if commands else ""
        maps[f"features/{key}.md"] = (
            f"# {key}\n\n## Sub-features\n\n{feature['observe']}\n\n"
            f"## How to get to it (user POV)\n\n{feature['reach']}\n{entrypoints}\n"
            f"## Driving it with iBoard\n\n{feature['drive']}\n\n"
            f"Run the generated skill with journey `{key}`. Evidence: `{key}.json`.\n\n"
            f"## Gotchas\n\n{feature['gotchas']}\n"
        )
    links = "\n".join(f"- [{entry['id']}]({entry['id']}.md)" for entry in contract["features"])
    maps["features/README.md"] = f"# Feature map\n\n{links}\n"
    maps["SKILL.md"] = (
        f"---\nname: verify-{slug}\n"
        f'description: "{slug} verification through the project user paths in offline Docker."\n'
        f"disable-model-invocation: true\n---\n\n"
        f"# Verify {slug}\n\n## Launch\n\n{contract['surface']}\n\n{contract['launch']}\n\n"
        f"Run from iBoard with a local dependency image whose tracked /app files match the selected Git revision. "
        f"Each drive starts a fresh network-disabled container.\n\n"
        f"## Doctor\n\n{doctor['drive']} {doctor['observe']}\n\n"
        f"## Drive\n\nChoose a journey from the [feature map](features/README.md).\n\n"
        "```sh\npython3 -m workbench.project_verify drive --skill \"$SKILL_DIR\" "
        "--repo \"$TARGET_REPO\" --image \"$LOCAL_IMAGE\" --out \"$EVIDENCE_DIR\" "
        f"--journey {contract['features'][0]['id']}\n```\n\n"
        "Use the absolute skill path, target Git checkout, prepared local image, and a new evidence directory. "
        "The runner checks doctor before the journey. Use the maintain command for the entire map.\n\n"
        "## Evidence\n\nThe output contains source locations, per-run JSON, and PNG where configured. "
        "Read the actions, asserted outcomes and raw logs. Keep evidence outside the target checkout.\n\n"
        f"## Cleanup\n\n{contract['cleanup']} The runner removes the container and retains evidence.\n\n"
        "## Helpers\n\nUse the commands above to execute helpers offline.\n\n"
        + "\n".join(
            f"- `{check['id']}` runs `{ENGINES[Path(check['helper']).suffix]} /workspace/{check['helper']}`."
            for check in [doctor, *contract["features"]]
        ) + "\n"
    )
    maps["contract.json"] = json.dumps(contract, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    result = {path: data.encode("utf-8") for path, data in maps.items()}
    for check in [doctor, *contract["features"]]:
        name = check["helper"]
        result.setdefault(f"helpers/{name}", _helper(helpers, name))
    return result


def _write_bundle(path, files):
    path.mkdir()
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(content)
        if name.startswith("helpers/"):
            target.chmod(0o755)


def _inventory(path):
    files, issues = {}, []
    for child in sorted(path.rglob("*")):
        name = child.relative_to(path).as_posix()
        if child.is_symlink() or not (child.is_dir() or child.is_file()):
            issues.append(name)
        elif child.is_file():
            if child.stat().st_size > 128_000:
                issues.append(name)
            else:
                files[name] = child.read_bytes()
                if name.startswith("helpers/") and not child.stat().st_mode & 0o111:
                    issues.append(name)
    return files, issues


def _read_blob(repo, revision, path, cache):
    if path not in cache:
        try:
            oid = cli.git(repo, "rev-parse", "--verify", f"{revision}:{path}", timeout=10)
            size = int(cli.git(repo, "cat-file", "-s", oid, timeout=10))
            if size > 1_000_000:
                raise ValueError("source object exceeds 1 MB")
            # cli.git strips outer whitespace, so read the exact blob for source line numbers.
            raw = subprocess.run(['git', '-C', str(repo), 'cat-file', 'blob', oid],
                                 capture_output=True, check=True, timeout=10).stdout
            cache[path] = (oid, raw.decode('utf-8'), None)
        except (ValueError, cli.WorkbenchError, subprocess.SubprocessError) as error:
            cache[path] = (None, "", str(error))
    return cache[path]


def _source(read_blob, check):
    result = []
    for anchor in check["source"]:
        oid, text, error = read_blob(anchor["path"])
        line = next((i for i, line in enumerate(text.splitlines(), 1) if anchor["contains"] in line), None)
        result.append({**anchor, "blob": oid, "line": line, "error": error})
    return result


def _changes(previous, candidate):
    return [{"path": path, "before": hashlib.sha256(previous[path]).hexdigest() if path in previous else None,
             "after": hashlib.sha256(candidate[path]).hexdigest() if path in candidate else None}
            for path in sorted(previous.keys() | candidate.keys()) if previous.get(path) != candidate.get(path)]


class Verification:
    def __init__(self, repo, revision, image, output, timeout, memory_mb, sandbox, attester):
        self.repo, self.revision, self.image = repo, revision, image
        self.output, self.memory_mb = output, memory_mb
        self.deadline = time.monotonic() + timeout
        self.sandbox, self.attester = sandbox, attester
        self.report = {"schema": cli.SCHEMA, "commit": revision, "requested_image": image,
                       "runs": [], "source": {}, "outcome": "blocked"}

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left < 1:
            raise ValueError("project verification time limit expired")
        return left

    def drive(self, bundle, check, attempt):
        key, helper = check["id"], check["helper"]
        basename = key + attempt
        command = [ENGINES[Path(helper).suffix], f"/workspace/{helper}"]
        screenshot = self.output / f"{basename}.png" if check.get("screenshot") else None
        helper_sha = None
        # Use a private frozen copy of the explicit helper. No host project, credentials,
        # installed skill tree or evidence directory is mounted into the container.
        try:
            data = _helper(bundle / "helpers", helper)
        except (OSError, ValueError) as error:
            run = {"error": str(error), "diagnostic": "harness-input"}
        else:
            helper_sha = hashlib.sha256(data).hexdigest()
            try:
                with TemporaryDirectory(prefix="iboard-verify-skill-") as scratch:
                    (Path(scratch) / helper).write_bytes(data)
                    run = self.sandbox(scratch, self.report["verified_image_id"], command, self.remaining(),
                                       memory_mb=self.memory_mb, workspace_read_only=True,
                                       source_root="/app", screenshot_output=screenshot)
            except (ValueError, OSError, SandboxError) as error:
                run = {"error": str(error), "diagnostic": "sandbox"}
        passed = (run.get("container_created") is True and run.get("cleanup_confirmed") is True
                  and run.get("workspace_read_only") is True and run.get("image") == self.report["verified_image_id"]
                  and run.get("command") == command and run.get("exit_code") == 0 and not run.get("error")
                  and not run.get("timed_out") and check["marker"] in run.get("stdout", "").splitlines()
                  and (screenshot is None or screenshot.is_file() and cli.digest(screenshot) == run.get("screenshot_sha256")))
        path = self.output / f"{basename}.json"
        run.update(schema=cli.SCHEMA, commit=self.revision, check=key, passed=passed,
                   helper_sha256=helper_sha, expected_marker=check["marker"])
        cli.write(path, run)
        self.report["runs"].append({"check": key, "report": path.name, "sha256": cli.digest(path),
                                    "passed": passed, "cleanup_confirmed": run.get("cleanup_confirmed") is True})
        return passed

    def verify(self, bundle, contract, selected):
        doctor = contract["doctor"]
        cache = {}
        checks = [doctor, *contract["features"]]
        def read_blob(path):
            return _read_blob(self.repo, self.revision, path, cache)
        self.report["source"] = {c["id"]: _source(read_blob, c) for c in checks}
        drift = [key for key, anchors in self.report["source"].items() if any(not a["line"] for a in anchors)]
        self.report["source_drift"] = drift
        inventory = project_surface.scan(contract, read_blob)
        if inventory["scope_count"]:
            self.report["entrypoints"] = inventory
        image_id = self.attester(self.repo, self.image, min(180, self.remaining()), commit=self.revision)
        self.report["verified_image_id"] = image_id
        healthy = self.drive(bundle, doctor, "")
        self.report["features"] = {}
        for check in selected:
            key = check["id"]
            if not healthy:
                self.report["features"][key] = "doctor-blocked"
                continue
            passed = self.drive(bundle, check, "")
            self.report["features"][key] = "passed" if passed else "behavior-unverified"
            if not passed:
                healthy = self.drive(bundle, doctor, f".after-{key}")
        return (not drift and not _entrypoint_gaps(inventory) and healthy
                and all(status == "passed" for status in self.report["features"].values())
                and all(row["cleanup_confirmed"] for row in self.report["runs"]))


def _entrypoint_gaps(inventory):
    return any(inventory.get(key) for key in ("unmapped", "stale", "errors"))


def discover(*, repo, revision, out, manifest, since=None):
    repo = Path(repo).expanduser().resolve(strict=True)
    commit = cli.sha(repo, revision, timeout=10)
    contract = _load_contract(manifest)
    if not contract.get("entrypoint_inventory"):
        raise ValueError("discover requires an entrypoint_inventory scope")

    def scan(ref):
        cache = {}
        return project_surface.scan(contract, lambda path: _read_blob(repo, ref, path, cache))

    current = scan(commit)
    report = {"schema": cli.SCHEMA, "commit": commit, "entrypoints": current, "mode": "discover",
              "outcome": "review-required" if _entrypoint_gaps(current) else "mapped"}
    if since:
        previous = cli.sha(repo, since, timeout=10)
        old = scan(previous)
        before = {entry["command"] for entry in old["entries"]}
        after = {entry["command"] for entry in current["entries"]}
        report["comparison"] = {"commit": previous, "errors": old["errors"],
                                "added": sorted(after - before), "removed": sorted(before - after)}
        if old["errors"]:
            report["outcome"] = "review-required"
    output = Path(out).expanduser().resolve()
    if output.is_relative_to(repo):
        raise ValueError("keep discovery evidence outside the target checkout")
    output.mkdir(mode=0o700, parents=True)
    cli.write(output / "result.json", report)
    return report


def run(mode, *, repo, revision, image, out, timeout=600, memory_mb=3328, manifest=None, helpers=None,
        skill=None, journey=None, sandbox=run_sandbox, attester=workflow._verified_image):
    repo = Path(repo).expanduser().resolve(strict=True)
    revision = cli.sha(repo, revision, timeout=10)
    if not 1 <= timeout <= 3600 or not 128 <= memory_mb <= 8192:
        raise ValueError("time and memory bounds are 1–3600 s and 128–8192 MiB")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]*", image):
        raise ValueError("use a valid local image reference")
    output = Path(out).expanduser().resolve()
    installed = Path(skill).expanduser().resolve(strict=True) if skill else None
    if output.is_relative_to(repo) or installed and output.is_relative_to(installed):
        raise ValueError("keep verification evidence and staging outside the target and installed skill")
    output.mkdir(mode=0o700, parents=True)
    runner = Verification(repo, revision, image, output, timeout, memory_mb, sandbox, attester)
    report = runner.report
    report.update(mode=mode, started_at=cli.now())
    try:
        if mode != "create":
            if (installed / "contract.json").is_symlink() or not installed.is_dir():
                raise ValueError("the installed skill must have its own regular contract")
            original = _load_contract(installed / "contract.json")
            previous, unsafe = _inventory(installed)
            report["map_drift"] = list(unsafe)
            try:
                expected = _artifacts(original, installed / "helpers")
                report["map_drift"] += [entry["path"] for entry in _changes(previous, expected)]
            except (OSError, ValueError) as error:
                report["map_drift"].append(str(error))
        if manifest:
            contract = _load_contract(manifest)
            if installed and original["project"] != contract["project"]:
                raise ValueError("replacement contract names a different project")
            files = _artifacts(contract, Path(helpers).expanduser().resolve(strict=True))
            bundle = output / "draft"
            _write_bundle(bundle, files)
            report["bundle"] = bundle.name
            if installed:
                report["changes"] = _changes(previous, files)
                report["replaces_unsafe_entries"] = unsafe
        else:
            contract, bundle = original, installed
        features = contract["features"]
        if mode in ("create", "drive"):
            selected = [entry for entry in features if entry["id"] == journey] if journey else features[:1]
            if not selected:
                raise ValueError("journey is absent from the feature map")
        else:
            selected = features
        proved = runner.verify(bundle, contract, selected)
        if proved and (manifest or not report.get("map_drift")):
            if manifest:
                release = output / f"verify-{contract['project']}"
                bundle.rename(release)
                report["bundle"] = release.name
            if report.get("changes"):
                with (output / "changes.patch").open("w", encoding="utf-8") as patch:
                    for change in report["changes"]:
                        name = change["path"]
                        patch.writelines(difflib.unified_diff(
                            previous.get(name, b"").decode("utf-8", "replace").splitlines(True),
                            files.get(name, b"").decode("utf-8", "replace").splitlines(True),
                            fromfile="before/" + name, tofile="after/" + name))
            report["outcome"] = {"create": "verified", "drive": "verified", "maintain":
                                 "changed" if report.get("changes") or report.get("replaces_unsafe_entries")
                                 else "clean"}[mode]
        elif report.get("map_drift") and not manifest:
            report["reason"] = "skill files diverge from the contract; pass a proven replacement to correct them"
        elif report.get("source_drift"):
            report["reason"] = "source anchors moved or disappeared"
        elif _entrypoint_gaps(report.get("entrypoints", {})):
            report["reason"] = "the source entrypoint inventory needs map corrections"
        else:
            report["reason"] = "one or more journeys lack passing execution or cleanup evidence"
    except ERRORS as error:
        report["reason"] = str(error)
    report["finished_at"] = cli.now()
    cli.write(output / "result.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    discovery = sub.add_parser("discover", help="Find unregistered and stale user commands from tracked source")
    discovery.add_argument("--repo", required=True)
    discovery.add_argument("--revision", default="HEAD")
    discovery.add_argument("--since")
    discovery.add_argument("--manifest", required=True)
    discovery.add_argument("--out", required=True)
    for mode in ("create", "drive", "maintain"):
        action = sub.add_parser(mode)
        action.add_argument("--repo", required=True)
        action.add_argument("--revision", default="HEAD")
        action.add_argument("--image", required=True)
        action.add_argument("--out", required=True)
        action.add_argument("--timeout", type=int, default=600)
        action.add_argument("--memory-mb", type=int, default=3328)
        if mode != "create":
            action.add_argument("--skill", required=True)
        if mode != "drive":
            action.add_argument("--manifest", required=mode == "create")
            action.add_argument("--helpers", required=mode == "create")
        if mode != "maintain":
            action.add_argument("--journey")
    args = vars(parser.parse_args(argv))
    if args["mode"] == "maintain" and bool(args["manifest"]) != bool(args["helpers"]):
        parser.error("maintain replacements require both --manifest and --helpers")
    try:
        report = discover(**{key: value for key, value in args.items() if key != "mode"}) \
            if args["mode"] == "discover" else run(**args)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps({"outcome": report["outcome"], "report": str(Path(args["out"]).resolve() / "result.json")},
                     ensure_ascii=False))
    return int(report["outcome"] in ("blocked", "review-required"))


if __name__ == "__main__":
    raise SystemExit(main())
