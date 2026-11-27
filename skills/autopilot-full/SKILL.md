---
name: autopilot-full
description: Run a selected queue of frozen iBoard tasks with separate worktrees, verification, delivery outcomes, and a shared operator hold.
disable-model-invocation: true
---

# Autopilot full

Use this workflow when the user explicitly selects several independent tasks to finish. Stage and freeze each task with the [single-task supervisor](../autonomous-run/SKILL.md). Confirm the request, acceptance conditions, time and iteration limits, and existing authorization for each destination. Finish verification before publication. Keep dependent PRs in the [stack workflow](../autopilot-stack/SKILL.md).

Create a JSON manifest with schema 1, one to 24 members, one to four concurrent slots, and a polling interval in seconds. Each member supplies the plan path and returned SHA-256. Relative plan paths are resolved from the manifest folder.

```json
{
  "schema": 1,
  "parallelism": 2,
  "interval": 30,
  "members": [
    {"plan": "one/plan.json", "sha256": "<approved SHA-256>"},
    {"plan": "two/plan.json", "sha256": "<approved SHA-256>"}
  ]
}
```

```sh
python3 -m workbench.autopilot_full freeze --manifest "$MANIFEST" --out "$QUEUE"
python3 -m workbench.autopilot_full run --plan "$QUEUE/group.json" --sha256 "$QUEUE_SHA"
python3 -m workbench.autopilot_full show --plan "$QUEUE/group.json" --sha256 "$QUEUE_SHA"
```

Keep the group directory outside member task directories and product worktrees. Run with the returned group digest. Add `--once` for a single pass over each member. Waiting for CI leaves the member available for observation without running another implementation attempt. The group journal stores observations keyed by member. Each member's journal retains its proof, original limits, and the exact PR and commit where applicable.

Use `hold --reason TEXT` with the same group plan and digest to stop admission of new steps. In-flight steps can finish and save their checkpoint. Use `show` to read the last observations, then explicitly `release` once the active scheduler and members have stopped. Re-run against the same group plan and original budgets. A separate hold on a member stays in force until explicitly released.

GitHub writes for group members sharing a repository are serialized. Each write still runs through the member's original verification and delivery gates. If a merge advances the target branch, the next candidate needs fresh verification against that new base. This queue currently stops at that blocker. Report outcomes per task using the group journal and each member's evidence. Call out which outcomes were verified locally and which were confirmed by GitHub.
