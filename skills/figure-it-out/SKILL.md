---
name: figure-it-out
description: Define and run a scoped custom playbook when a narrower iBoard workflow does not fit.
disable-model-invocation: true
---

# Figure it out

Use the [pinned pstack workflow](https://github.com/cursor/plugins/blob/41e400932eca14f289e7db717a70455256b6d388/pstack/skills/figure-it-out/SKILL.md). Read the relevant poteto principles. State the observable result, what the work affects, the remaining unknowns, and the level of rigor. Put the riskiest unknown early. Keep each unit small enough to verify and retain independently. Set a checkpoint for work expected to take several hours.

Write the playbook before editing. Put the observation methods in the baseline commit. If the change commits the repository to a hard-to-reverse design, compare structural options with [Architect](../architect/SKILL.md), and use [Arena](../arena/SKILL.md) when a competing implementation would resolve the uncertainty. Pin the selected design. If the work has separable parts that can be dispatched independently, use the existing [multi-phase dispatcher](../multi-phase-plan/SKILL.md) for those parts.

## Approved manifest

| Field | Contents |
| --- | --- |
| `schema`, `intent`, `done` | Schema 1, the requested change, and a falsifiable description of the whole product result. |
| `frame` | `rigor` as `reversible` or `high-impact`, plus `reason`, `estimated_minutes`, and `blockers`. |
| `image`, `seconds`, `memory_mb`, `model`, `review_model` | Immutable local Docker image, run time and memory, explicit GLM model, and independent OpenAI review model. |
| `design` | `null` or the Architect result's `path` and `sha256`. High-impact work requires a selection from the same Git baseline and an approved scope covering all units. |
| `checks` | Named checks with committed workspace `command`, plain-language `expectation`, and distinct `pass` and `fail` output markers. A reproducible pass exits 0. An observed failure exits 7. |
| `guards`, `final` | Names of the checks preserving existing behavior and the check for the complete requested outcome. |
| `units` | Ordered objects with `id`, `unknown`, editable file `scope`, a named `check`, and bounded `hypotheses`. Each hypothesis has `description` and `commit`. Use `null` to have Claude Code edit the scoped files, or supply an exact single commit on the previously accepted parent. |

```sh
python3 -m workbench.figure_it_out freeze --manifest "$MANIFEST" --repo "$REPO" \
  --baseline "$BASELINE" --out "$PLAN"
python3 -m workbench.figure_it_out run --plan "$PLAN" --sha256 "$PLAN_SHA" --out "$OUT"
python3 -m workbench.figure_it_out audit --plan "$PLAN" --sha256 "$PLAN_SHA" \
  --report "$OUT/trail.json" --report-sha256 "$REPORT_SHA"
```

The runner captures the final check and guards before editing. For each unit, it captures the before-state on the retained parent. It then tries the approved hypotheses in disposable worktrees. Two distinct offline runs must observe the changed behavior, shared guards and all earlier verified unit checks before independent review. Failed alternatives remain in the trail with their patches and receipts. Later units start from the retained commit only. If the observation method needs a correction, approve a new plan and retain the old run and its evidence.

The final worktree reruns every check twice, including the whole product predicate. The runner exports the verified commit chain as `verified.mbox` only after every unit and final check passes. Use the offline audit to verify the exact patch series, observations, review, and original plan. The native path sequences dependent units, evaluates workspace-mode checks, and hands back evidence and commits for the normal iBoard adoption and PR gates.

Summarize the significant decisions, the rigor and why, the trail path, what passed, and what remains open. Use `VERIFIED`, `NOT VERIFIED`, or `INCONCLUSIVE`. Turn recurring mistakes into executable rules where possible.
