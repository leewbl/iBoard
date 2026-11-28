---
name: create-verification-skill
description: Prepare a project-local verification skill from real user journeys and prove it in iBoard's offline Docker runner.
disable-model-invocation: true
---

# Create a project verification skill

Read the target repository first. Locate the user entry point, its documented launch, existing drivers, readiness signal, observable effects and how the process isolates its state. Inspect scripts before running them. Use the project's real CLI, API or interface and use fixture data and local-only dependencies. If a required dependency cannot run in this environment, record the exact missing prerequisite.

Write a verification contract following [OpenClaw's example](../verify-openclaw/project-verification.json). Include a read-only doctor and one record per journey, starting with the primary user actions. Anchor each record to relevant text in tracked source. Describe the actual drive, expected state, cleanup and any prerequisites. Supply each helper as a standalone Node, Python or shell script that exits unsuccessfully when an assertion fails. A separate line ending in `_PASS` signals that the helper finished all assertions and cleanup. If the journey requires a screenshot, set `screenshot` to `true` and have the helper write its PNG to the path in `WORKBENCH_SCREENSHOT_PATH`.

Use an `args` array for literal positional arguments to a shared helper. The runner invokes it directly. For browser journeys, set both `browser` and `screenshot` to `true`. Prepare the image with the built UI and its browser dependencies. The driver gets the pinned revision in `/workspace/expected-commit.txt` and prints one JSON line with `"phase":"ui"`, `"servedCommit"` set to the commit read from the running page, and `"screenshot":true` after capture. It verifies the user-visible behavior and any side effects before printing the pass marker.

For an interactive terminal, use the project's existing PTY driver where available. Send input through the terminal, check completed screen rows, and read back any persisted changes through a separate interface. Keep a bounded raw transcript alongside the observed rows and exit status. The [OpenClaw TUI journey](../verify-openclaw/features/tui-chat-pty.md) shows how to retain both the first session and its reopened history in the offline evidence report.

Use `entrypoint_inventory` to bind tracked source to user entrypoints. A CLI binding has `path` and `receiver` for Commander registrations. The scanner reads quoted names, including argument signatures, and resolves direct registrations in fixed loops such as `for (const name of ["archive", "restore"] as const)`. A JSX source binding has `kind: "jsx-options"`, `path`, `component`, and `label_key`. It selects one opening tag by its component name and `ariaLabel={t("translation.key")}`, then reads the literal values from inline option objects. Associate entries with journeys using each feature's `entrypoints`. Record an excluded entry and its prerequisite in `excluded`. An unresolved registration or control in a declared scope blocks export even when the selected smoke journey passes.

```sh
python3 -m workbench.project_verify create --repo "$TARGET_REPO" --revision "$REVISION" \
  --manifest "$CONTRACT" --helpers "$HELPER_DIRECTORY" --image "$LOCAL_IMAGE" \
  --out "$EVIDENCE_DIR" --journey "$JOURNEY"
```

Use a local image with tracked code under `/app` and its test dependencies already installed. iBoard resolves the selected Git commit, binds the image to its tracked source, checks each source anchor, runs doctor and exercises the selected real journey. Each command gets a fresh offline container and a read-only copy of the helper. Browser drives also receive the revision file. The evidence includes the image, source paths and lines, logs or screenshots, exit status and container cleanup.

Use the generated `verify-<project>` directory as a candidate project-local skill after `result.json` reports `verified`. It includes the map, contract and executable helpers. A blocked run keeps the candidate in `draft` and retains its failure evidence. Repeat the run with a new evidence directory after correcting the setup or proof. Invoke [maintain-verification-skill](../maintain-verification-skill/SKILL.md) to prove the remaining mapped journeys and keep the bundle current.
