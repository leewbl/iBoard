---
name: hillclimb
description: Try one performance hypothesis at a time, retain only measured correct improvements, and export the verified commit chain.
disable-model-invocation: true
---

# Optimize one measured outcome

Start with the user-visible problem and realistic data size, history and concurrency. Trace the source that does the work. Fix one metric, a minimum number of attempts, a target reduction, and a time budget. Record each concrete mechanism you plan to try. If the issue cannot be measured, fix the reproduction before optimizing.

Use [benchmark-checklist](../benchmark-checklist/SKILL.md) to prepare the workload, output checks, valid runtime and independent profile. Provide a known control revision that makes the same workload measurably faster or slower with the same correctness. The experiment checks the predicted direction before running any proposed edits. Supply a separate offline correctness helper for behavior that timing could miss.

The manifest fields are:

| Field | Meaning |
| --- | --- |
| `repo`, `revision`, `image` | Local Git checkout, chosen baseline, and local image matching that baseline. |
| `benchmark`, `helpers` | Benchmark contract and directory for its helpers and the correctness helper. |
| `gate` | Object with `helper` and a standalone `marker` ending in `_PASS`. |
| `sensitivity` | Object with a different `revision` above the chosen baseline and predicted `direction` of `faster` or `slower`. |
| `target_reduction_pct`, `minimum_attempts`, `seconds` | Frozen improvement target, attempt floor and total time bound. |
| `hypotheses` | Ordered objects with a `hypothesis`, editable `files` and an optional unified `patch` prepared against the expected retained parent. |
| `provider`, `memory_mb` | Model for attempts without an explicit patch, defaulting to the configured Claude Code + GLM adapter, and per-container memory. |

```sh
python3 -m workbench.hillclimb --manifest "$MANIFEST" --out "$EVIDENCE_DIR"
```

Each attempt reads the prior decision log, starts in a fresh detached worktree from the latest accepted commit, and changes only its listed files. The fixed benchmark compares that candidate against its parent. A candidate enters the retained chain only with a measurable speedup and a passing correctness gate. A slower companion end-to-end workload blocks a component improvement. A reverted attempt has no effect on later parents.

The stop predicate requires both the configured floor and a new comparison to the original baseline which confirms the target. A completed experiment exports `accepted.mbox` only after this comparison confirms a speedup and the final candidate passes a fresh correctness check. Check `result.json`, `decision.tsv`, and each raw sandbox result before delivering the retained commits. A bounded run can end with `target-unmet`, with the proved improvements available for later review. The workflow exports commits for review and delivery through the project’s existing PR gates.
