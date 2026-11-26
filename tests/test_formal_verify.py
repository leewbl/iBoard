import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from workbench import cli, formal_verify


WITNESS = ["Admit one task", "Admit another while the first is still active"]


class FormalVerifyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "project"
        self.repo.mkdir()
        cli.git(self.repo, "init", "-q")
        cli.git(self.repo, "config", "user.name", "Fixture")
        cli.git(self.repo, "config", "user.email", "fixture@example.invalid")
        cli.git(self.repo, "config", "commit.gpgsign", "false")
        self.base = self.commit("app.js", "const capacity = 1;\n")
        self.image = "sha256:" + "a" * 64
        self.images = {self.image: self.base}
        self.helpers = self.root / "helpers"
        self.helpers.mkdir()
        (self.helpers / "checker.mjs").write_text("primary checker")
        (self.helpers / "replay.mjs").write_text("independent replay")
        (self.helpers / "schedule.json").write_text('["enqueue", "settle"]')
        self.spec = {
            "id": "capacity", "property": "A lane starts at most one command at a time.",
            "harm": "Commands can concurrently change session state.",
            "bounds": ["Two tasks per lane."], "assumptions": ["Same lane and priority."],
            "exclusions": ["Restarts."], "method": "bounded-real-code", "tool_version": "fixture 1",
            "checker": "checker.mjs", "replay": "replay.mjs", "support": ["schedule.json"],
            "minimum_explored": 4,
            "source": [{"path": "app.js", "contains": "const capacity", "meaning": "Availability limit."}],
            "mutation": {"path": "app.js", "before": "capacity = 1", "after": "capacity = 2",
                         "meaning": "Increase simultaneous admissions."},
        }
        self.manifest = self.root / "manifest.json"
        cli.write(self.manifest, self.spec)
        self.events = []
        self.miss_mutation = False
        self.replay_mismatch = False
        self.replacement_misses_bug = False
        self.tool_version = self.spec["tool_version"]
        self.truncated = False

    def commit(self, path=None, contents=None):
        if path is not None:
            (self.repo / path).write_text(contents)
            cli.git(self.repo, "add", "--", path)
        cli.git(self.repo, "commit", "-qm", "fixture source change")
        return cli.sha(self.repo, "HEAD")

    def attest(self, repo, image, timeout, *, commit):
        self.assertEqual(self.images[image], commit)
        return image

    def derive(self, repo, base, head, image, timeout):
        self.assertEqual(self.images[image], base)
        current = "sha256:" + hashlib.sha256(head.encode()).hexdigest()
        self.images[current] = head
        return current

    def sandbox(self, workspace, image, command, timeout, **options):
        path = Path(workspace)
        self.assertTrue(options["workspace_read_only"])
        self.assertEqual(options["source_root"], "/app")
        self.assertEqual((path / "schedule.json").read_text(), '["enqueue", "settle"]')
        commit = self.images[image]
        tree = cli.git(self.repo, "ls-tree", "--name-only", commit).splitlines()
        source = next(src for src in tree if src in ("app.js", "lane.js"))
        version = cli.git(self.repo, "show", f"{commit}:{source}")
        replay = command[-1].endswith("/counterexample.json")
        if replay:
            self.assertEqual(json.loads((path / "counterexample.json").read_bytes()), WITNESS)
            self.assertFalse((path / "checker.mjs").exists())
        self.events.append((commit, command))
        driver = (path / Path(command[1]).name).read_text()
        broken = ("capacity = 2" in version and not self.miss_mutation
                  and not (self.replacement_misses_bug and driver == "replacement checker")
                  and not (replay and self.replay_mismatch))
        result = {"status": "violated" if broken else "holds", "tool_version": self.tool_version,
                  "explored": 1 if replay else 4, "counterexample": WITNESS if broken else []}
        stdout = formal_verify.RESULT_PREFIX + json.dumps(result) + "\n"
        code = int(broken)
        return {"command": command, "image": image, "stdout": stdout, "stderr": "", **options,
                "create_stdout": f"{len(self.events):064x}", "create_exit_code": 0,
                "container_created": True, "cleanup_confirmed": True, "timed_out": False, "exit_code": code,
                "runtime_diagnostics": {"state": {"status": "exited", "exit_code": code, "oom_killed": False}},
                "stdout_truncated": self.truncated, "stderr_truncated": False,
                "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
                "stderr_sha256": hashlib.sha256(b"").hexdigest()}

    def run_mode(self, mode, label, **kwargs):
        return formal_verify.run(mode, repo=self.repo, out=self.root / label, sandbox=self.sandbox,
                                 attest=self.attest, derive=self.derive, **kwargs)

    def create(self):
        return self.run_mode("create", "created", image=self.image, manifest=self.manifest, helpers=self.helpers)

    def installed(self, report, label="created"):
        return {"skill": self.root / label / report["bundle"], "skill_sha256": report["bundle_sha256"]}

    def inspect(self, **installed):
        report = self.run_mode("maintain", "inspect", **installed)
        self.assertEqual(report["outcome"], "review-required")
        return report

    def review(self, inspected, *, effect="verification-drift", intent=None):
        history = json.loads((self.root / "inspect/history.json").read_bytes())
        review = self.root / "review.json"
        contents = {"history_sha256": inspected["history"]["sha256"], "commits": {
            row["commit"]: {"effect": effect, "reason": "Reviewed the queue source change."}
            for row in history["commits"]
        }}
        if intent:
            contents["intent"] = intent
        cli.write(review, contents)
        return review

    def test_generated_skill_replays_and_excludes_scratch_tree(self):
        result = self.create()
        self.assertEqual(result["outcome"], "verified", result)
        self.assertEqual(result["coverage"][0]["line"], 1)
        self.assertEqual(result["mutation"]["counterexample"], WITNESS)
        self.assertEqual([run["label"] for run in result["runs"]],
                         ["verification", "mutation", "mutation-replay", "unmodified-replay"])
        mutate = result["runs"][1]
        self.assertEqual(mutate["sha256"], cli.digest(self.root / "created" / mutate["report"]))
        self.assertEqual(cli.sha(self.repo, "HEAD"), self.base)
        self.assertEqual(cli.git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(cli.git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)
        installed = self.installed(result)
        self.assertIn(result["bundle_sha256"], (installed["skill"] / "SKILL.md").read_text())
        self.assertEqual(self.run_mode("check", "reopen", **installed)["outcome"], "in-sync")

    def test_modified_verifier_support_is_refused_before_any_run(self):
        result = self.create()
        installed = self.installed(result)
        (installed["skill"] / "helpers/schedule.json").write_text("{}")
        before = len(self.events)
        observed = self.run_mode("check", "tampered", **installed)
        self.assertEqual(observed["outcome"], "blocked")
        self.assertIn("bundle or drivers changed", observed["reason"])
        self.assertEqual(len(self.events), before)

    def test_failing_to_detect_mutation_cannot_generate_skill(self):
        self.miss_mutation = True
        result = self.create()
        self.assertEqual(result["outcome"], "blocked")
        self.assertIn("failed to detect", result["reason"])
        self.assertNotIn("sync", result)
        self.assertEqual(cli.sha(self.repo, "HEAD"), self.base)

    def test_wrong_tool_and_truncated_output_are_not_property_failures(self):
        for mode in ("truncated", "wrong-tool"):
            self.truncated = mode == "truncated"
            self.tool_version = "other" if mode == "wrong-tool" else self.spec["tool_version"]
            result = self.run_mode("create", mode, image=self.image, manifest=self.manifest, helpers=self.helpers)
            self.assertEqual(result["outcome"], "blocked")
            self.assertNotIn("counterexample", result)

    def test_maintenance_follows_a_rename_and_keeps_the_public_property(self):
        installed = self.installed(self.create())
        cli.git(self.repo, "mv", "app.js", "lane.js")
        new_sync = self.commit()
        first = self.inspect(**installed)
        self.assertEqual(first["runs"][0]["result"]["status"], "holds")
        history = json.loads((self.root / "inspect/history.json").read_bytes())
        self.assertEqual(history["commits"][0]["changes"], [
            {"status": "R100", "paths": ["app.js", "lane.js"], "touches_coverage": True}
        ])
        self.spec["source"][0]["path"] = self.spec["mutation"]["path"] = "lane.js"
        cli.write(self.manifest, self.spec)
        updated = self.run_mode("maintain", "updated", review=self.review(first), manifest=self.manifest,
                                helpers=self.helpers, **installed)
        self.assertEqual((updated["outcome"], updated["sync"]), ("updated", new_sync))
        self.assertEqual(updated["verification_changes"]["fields"], ["mutation", "source"])
        self.assertIn("lane.js", (self.root / "updated/verification.diff").read_text())
        self.assertEqual(json.loads((installed["skill"] / "bundle.json").read_bytes())["sync"], self.base)

    def test_maintenance_cannot_change_bounds_without_approved_intent(self):
        installed = self.installed(self.create())
        self.commit("app.js", "const capacity = 1;\n// Documented scope change\n")
        first = self.inspect(**installed)
        review = self.review(first, effect="intentional-contract-change", intent={
            "path": "app.js", "contains": "Documented scope change", "meaning": "Updated test contract.",
        })
        self.spec["bounds"] = ["Two tasks and two priorities."]
        cli.write(self.manifest, self.spec)
        common = {"review": review, "manifest": self.manifest, "helpers": self.helpers, **installed}
        blocked = self.run_mode("maintain", "without-approval", **common)
        self.assertEqual(blocked["outcome"], "blocked")
        self.assertIn("approved manifest", blocked["reason"])
        allowed = self.run_mode("maintain", "with-approval",
                                approve_contract_change=cli.digest(self.manifest), **common)
        self.assertEqual(allowed["outcome"], "updated")
        self.assertEqual(allowed["review"]["contract_changes"]["bounds"]["before"], ["Two tasks per lane."])

    def test_maintenance_distinguishes_replayed_defect_from_a_checker_discrepancy(self):
        installed = self.installed(self.create())
        self.commit("app.js", "const capacity = 2;\n")
        first = self.inspect(**installed)
        review = self.review(first, effect="possible-bug")
        bug = self.run_mode("maintain", "confirmed", review=review, **installed)
        self.assertEqual(bug["outcome"], "bug")
        self.assertEqual(bug["counterexample"], WITNESS)
        self.assertNotIn("sync", bug)
        self.replay_mismatch = True
        disputed = self.run_mode("maintain", "disputed", review=review, **installed)
        self.assertEqual(disputed["outcome"], "review-required")
        self.assertNotIn("sync", disputed)
        self.assertEqual(disputed["runs"][-1]["result"]["status"], "holds")

    def test_replacing_a_checker_cannot_dismiss_its_reproduced_counterexample(self):
        installed = self.installed(self.create())
        self.commit("app.js", "const capacity = 2;\n")
        first = self.inspect(**installed)
        self.replacement_misses_bug = True
        (self.helpers / "checker.mjs").write_text("replacement checker")
        report = self.run_mode("maintain", "unsafe-replacement", review=self.review(first),
                               manifest=self.manifest, helpers=self.helpers, **installed)
        self.assertEqual(report["outcome"], "bug")
        self.assertEqual(report["counterexample"], WITNESS)
        self.assertEqual([item["label"] for item in report["runs"]],
                         ["as-installed", "verification", "as-installed-counterexample"])
        self.assertNotIn("bundle", report)


if __name__ == "__main__":
    unittest.main()
