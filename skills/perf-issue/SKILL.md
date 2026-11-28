---
name: perf-issue
description: Diagnose an observed performance fault, verify the same workload after the fix, and retain the measured result for PR delivery.
---

# Fix a measured performance issue

Prepare an iBoard task with `workbench.start --prepare-only` in an isolated worktree. Use a baseline image whose source under `/app` matches the task base. Review the [benchmark contract](../benchmark-checklist/SKILL.md), including the measured user path, completed work, expected output and separate limiter probe. A component benchmark needs a companion end-to-end workload.

```sh
python3 -m workbench.perf_issue freeze --task "$TASK" --image "$BASE_IMAGE" \
  --manifest "$BENCHMARK" --helpers "$HELPERS" --out "$BEFORE" \
  --minimum-improvement-pct "$TARGET"
```

Use the frozen profile and source anchor to explain what consumes time. Try eliminating work first, then avoiding repeats, doing less, deferring work, moving it away from interaction, using safe concurrency, and reducing its cost. If the change crosses a function boundary, use [Architect](../architect/SKILL.md) to compare approaches before edits. Keep the verification workload and functional acceptance unchanged.

Resume the task through iBoard. Its planner sees the baseline and hotspot. After functional checks, iBoard remeasures the clean candidate and passes the verified observations to the independent reviewer. It replays the timing and profile calculations from stored receipts before delivery. Require the frozen reduction and a non-regressing end-to-end observation. A missing, overlapping, or failed measurement retains its raw evidence for investigation.

For a manually edited candidate, `workbench.perf_issue verify --task "$TASK" --out "$AFTER"` records the comparison. The independent review must bind its digest in `performance_result_sha256`. Use [Opening a PR](../opening-a-pr/SKILL.md) for the approved handoff. Report the baseline, candidate, percentage delta and evidence location. For continued optimization across a sequence of improvements use Hillclimb.
