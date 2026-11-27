---
name: swarm
description: Verify several declared paths or compare several commits in separate offline sandboxes, then audit the combined result.
disable-model-invocation: true
---

# Swarm

Decide which behavior must be demonstrated before dispatch. Freeze a JSON manifest with `schema` 1, a concrete `predicate`, one to four execution slots in `parallelism`, time in `seconds`, per-container `memory_mb`, and an exact local Docker `image` ID. Each lane declares a unique `id`, a `role`, an exact `revision`, and a `command` array that runs a tracked driver under `/workspace`. Record the expected `exit_code` and an exact stdout line in `marker`.

Set `shape` to `coverage`, `race`, or `mixed`. A coverage lane is required. Every such lane must pass even when a comparison arm wins. For a race, choose the rule before the run. All arms use the same driver bytes, command, marker, expected exit, and immutable image.

- `first-pass` selects the first arm with a verified pass and still drains all workers.
- `rank-all` reports all results and selects the highest passing score when every arm produces a verdict.
- `best-of` selects the highest observed passing score and reports any blocked arm. The ranked rules read a single line `IBOARD_SCORE=<integer from 0 to 100>` from the frozen driver. Tied highest scores remain unresolved. For a coverage-only manifest set `race_rule` to `null`.

```sh
python3 -m workbench.swarm freeze --manifest "$MANIFEST" --repo "$REPO" --out "$PLAN"
python3 -m workbench.swarm run --plan "$PLAN/plan.json" --sha256 "$PLAN_DIGEST" --out "$EVIDENCE"
python3 -m workbench.swarm audit --plan "$PLAN/plan.json" --sha256 "$PLAN_DIGEST" \
  --report "$EVIDENCE/result.json" --report-sha256 "$RESULT_DIGEST"
```

The coordinator gives every worker a detached worktree and a read-only workspace in an offline Docker container. The retained worker receipts show the exact commit, command, outcome and cleanup. A worker without usable evidence can be retried once if its previous attempt cleaned up. A failed expected behavior remains an issue. A missing result remains a gap.

To require coverage in a normal iBoard task, create a coverage-only manifest using `candidate` or `baseline` in each lane's `revision` and include at least one candidate lane. Pass `--swarm-manifest "$MANIFEST" --swarm-sha256 "$MANIFEST_DIGEST"` with the usual `workbench.start` arguments. The task resolves the roles to its actual revisions before the independent review. The delivery gates audit every required lane again before writing.

Report the overall verdict, lane outcomes, any race rule and selection, and links to exact evidence. This entry runs committed commands. Delegated model investigations, hosted workers and model races remain separate from this local verifier.
