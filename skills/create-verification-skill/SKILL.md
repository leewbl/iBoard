---
name: create-verification-skill
description: Prepare a project-local verification skill from real user journeys and prove it in iBoard's offline Docker runner.
disable-model-invocation: true
---

# Create a project verification skill

Read the target repository first. Locate the user entry point, its documented launch, existing drivers, readiness signal, observable effects and how the process isolates its state. Inspect scripts before running them. Use the project's real CLI, API or interface and use fixture data and local-only dependencies. If a required dependency cannot run in this environment, record the exact missing prerequisite.

Write a verification contract following [OpenClaw's example](../verify-openclaw/project-verification.json). Include a read-only doctor and one record per journey, starting with the primary user actions. Anchor each record to relevant text in tracked source. Describe the actual drive, expected state, cleanup and any prerequisites. Supply each helper as a standalone Node, Python or shell script that exits unsuccessfully when an assertion fails. A separate line ending in `_PASS` signals that the helper finished all assertions and cleanup. If the journey requires a screenshot, set `screenshot` to `true` and have the helper write its PNG to the path in `WORKBENCH_SCREENSHOT_PATH`.

```sh
python3 -m workbench.project_verify create --repo "$TARGET_REPO" --revision "$REVISION" \
  --manifest "$CONTRACT" --helpers "$HELPER_DIRECTORY" --image "$LOCAL_IMAGE" \
  --out "$EVIDENCE_DIR" --journey "$JOURNEY"
```

Use a local image with tracked code under `/app` and its test dependencies already installed. iBoard resolves the selected Git commit, binds the image to its tracked source, checks each source anchor, runs doctor and exercises the selected real journey. Each command gets a fresh offline container and a read-only copy of the helper. The runtime only mounts that helper. The evidence includes the image, source paths and lines, logs or screenshots, exit status and container cleanup.

Use the generated `verify-<project>` directory as a candidate project-local skill after `result.json` reports `verified`. It includes the map, contract and executable helpers. A blocked run keeps the candidate in `draft` and retains its failure evidence. Repeat the run with a new evidence directory after correcting the setup or proof. Invoke [maintain-verification-skill](../maintain-verification-skill/SKILL.md) to prove the remaining mapped journeys and keep the bundle current.
