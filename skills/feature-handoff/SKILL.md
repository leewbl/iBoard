---
name: feature-handoff
description: Start an approved feature with frozen design, edit scope, and acceptance criteria, then require independent runtime evidence before delivery.
disable-model-invocation: true
---

# Deliver an approved feature

Trace the affected entry points, callers, and state owners. Select a design with [Architect](../architect/SKILL.md). If a user-facing decision needs an observed result, run [Prototype](../prototype/SKILL.md) on the same Git baseline and review its recommendation. Use [Arena](../arena/SKILL.md) when competing implementations need comparison.

Prepare one JSON manifest after the design and acceptance criteria have been approved. Relative artifact paths resolve next to the manifest. Use the actual SHA-256 of each referenced artifact.

```json
{
  "design": {"path": "design/result.json", "sha256": "<approved Architect result SHA-256>"},
  "review_model": "<configured OpenAI model>",
  "criteria": [{"id": "behavior", "text": "<exact runtime expectation from the Architect result>"}],
  "checkpoint": {
    "blocking": "<decision or evidence that must come first>",
    "workstreams": "<independent work and ownership>",
    "shared_state": "<shared state and its write owner>",
    "decomposition": "<smallest safe delivery scope, or n/a: reason>"
  },
  "prototypes": []
}
```

Include every runtime obligation as an exact criterion. Add any further acceptance criteria with unique IDs. Optional `prototypes` accepts up to six `{"path": "…", "sha256": "…"}` references to native prototype `result.json` files. iBoard validates the linked execution evidence and retains the recommendations for planning.

```sh
python3 -m workbench.start --id "$TASK_ID" --repo "$TARGET_REPO" \
  --kind feature --surface "$SURFACE" --issue-file "$REQUEST" --source "$REQUEST_SOURCE" \
  --feature-manifest "$HANDOFF" --feature-sha256 "$HANDOFF_SHA256" \
  --image "$LOCAL_IMAGE" --iterations "$ITERATIONS" --seconds "$SECONDS_BUDGET"
```

iBoard starts a new worktree at the approved baseline. It retains the handoff and uses the frozen criteria for planning, implementation, repair, and review. The implementation must stay within the selected design's file list. The selected model checks the exact candidate diff for static design requirements. The independent verifier must cite the candidate's offline Docker resolution run for each frozen runtime criterion. The evidence gate rechecks the attached receipts and candidate before accepting delivery. Keep the functional documentation in the approved edit scope. UI tasks also require an independent screenshot.

For authorized GitHub delivery, provide the usual `--pr-repo` and CI policy options, or `--publish-only`. Review the task record and retained architecture and runtime evidence for failures. A revised requirement or a broader edit scope needs a new approved handoff.
