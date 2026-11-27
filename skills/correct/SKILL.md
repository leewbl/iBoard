---
name: correct
description: Turn a repeated repository mistake into a guard and prove it against actual historical commits.
disable-model-invocation: true
---

# Correct a recurring mistake

Follow the [pinned pstack correction skill](https://github.com/cursor/plugins/blob/41e400932eca14f289e7db717a70455256b6d388/pstack/skills/correct/SKILL.md). Start with recent commits, reverts, relevant PR discussions, project instructions, and comments that explain workarounds. Cite two distinct occurrences before treating a mistake as recurring. For each class, fix one owner or supported route through the architecture when possible. Consider types next, then a lint with an actionable message, then a behavior test. Use instructions for decisions the code cannot enforce.

Keep one change per class. Use the same command locally and in CI where configured. An exception to a lint belongs at the affected line with an explanation, expiry, and human approval. Keep the repository's rule table up to date. Drop a rule when the architecture makes its mistake impossible.

## Replay the guard

Provide a schema-1 JSON manifest with `rule`, `scope`, `cases`, `driver`, `image`, and `seconds`.

- `rule` holds `id`, `description`, `level`, `mechanism`, and `higher_levels`. For level `type`, `lint`, or `behavior-test`, explain why each earlier level cannot enforce the rule.
- `scope` lists explicit repository paths needed for the probe. `cases` lists at least two occurrences with distinct `id` values, exact `bad` and `fix` commits, changed `paths` inside the scope, and an `observation`. Each bad commit is the direct parent of its repair.
- `driver` holds a committed `path` plus `engine` set to `python3` or `node`. It receives the case ID and runs against the frozen source mounted at `/workspace`. Have it accept a healthy control before exercising the mistake.
- Pin an immutable locally available Docker image and a replay time limit. A confirmed bad observation prints `GUARD_MISSED ID` and exits 7. A checked rejection prints `GUARD_HOLDS ID` and exits 0. A crash, missing dependency, or generic failing assertion does not prove a historical miss.

```sh
python3 -m workbench.correct freeze --manifest "$MANIFEST" --repo "$REPO" \
  --current "$CURRENT_COMMIT" --driver-revision "$DRIVER_COMMIT" --out "$PLAN"
python3 -m workbench.correct run --plan "$PLAN" --sha256 "$PLAN_SHA" --out "$OUT"
python3 -m workbench.correct audit --plan "$PLAN" --sha256 "$PLAN_SHA" \
  --report "$OUT/result.json" --report-sha256 "$REPORT_SHA"
```

Run the same probe on each historical parent, its repair, and the selected current commit. The runner uses fresh Git archives and read-only offline Docker. Audit checks the selected source, probe bytes, runtime receipts and container identities. Present the evidence, the chosen enforcement level, and why a stronger level was inapplicable.

## iBoard rule table

| Rule | Enforcement | Historical evidence |
| --- | --- | --- |
| OpenClaw delivery requires current, registered runtime proof of every applicable behavior, with an independent container for each role. | `workbench.verify_openclaw.require` owns the invariant. All journeys check container identities and the exact review. The browser session journey checks process replacement and authenticated readback. | `2743df6` fixes unregistered receipts. `1567cd1` adds actual Gateway replacement. [The committed probe](openclaw-proof-probe.py) checks those cases and the reuse of one container under different workspace names. Run `python3 -m unittest discover -s tests -p test_verify_openclaw_gate.py -v` to exercise the current gate. |

If a retained run predates container identity metadata, execute the required journey again before delivery. An inferred identity or a workspace name cannot establish a second execution.
