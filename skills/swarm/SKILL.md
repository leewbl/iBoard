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

Report the overall verdict, lane outcomes, any race rule and selection, and links to exact evidence. This entry runs committed commands. Hosted workers and model investigations use different execution paths from this local verifier.

## Model investigations

Use `workbench.swarm_investigate` to answer read-only questions about a fixed commit. A coverage run can give each lane a different question and source range. A model race gives every arm the same ordered source ranges and question. The manifest names the exact provider and model for each lane. Each answer must quote a committed source line, distinguish observations from inferences, and keep unresolved points visible.

The model manifest uses `schema`, `revision`, `predicate`, `parallelism`, `seconds`, `shape`, `race_rule`, `lanes`, `judge`, and `rubric`. Each lane has an `id`, a `question`, a `files` array of committed paths or `path:start-end` ranges, and an `engine` with `provider` (`glm` or `openai`) and `model`. Workers receive only the approved source excerpts in their prompt. GLM runs through the restricted Claude Code adapter, and OpenAI uses the existing restricted Codex adapter.

Use `coverage` with a null rule or `race` with `first-pass`, `rank-all`, or `best-of`. For the ranked rules, name a judge model outside the race and freeze each rubric item as an `id` and `text`. The judge receives neutral labels and the original source. A tied score leaves the winner unresolved. For coverage and first-pass, use a null judge and an empty rubric. An invalid citation can be retried once. A provider failure stops new calls to that provider for the run.

Add `model_profile` with the `path` and `sha256` of a [confirmed profile](../setup-pstack/SKILL.md)
to inherit the `swarm-workers` route. Omit a lane's engine or use `"profile"` to
inherit it. Explicit race arms can name another confirmed model and effort
within the same budget. For ranked runs, use `"profile"` or `null` as the judge
to select the first eligible confirmed judge outside the parent vendor and
outside the race. An explicit judge must also be in the approved pool. The
frozen plan retains the profile and all resolved routes. Audit checks the
requested effort and model metadata along with the source quotes.

```sh
python3 -m workbench.swarm_investigate freeze --manifest "$MANIFEST" --repo "$REPO" --out "$PLAN"
python3 -m workbench.swarm_investigate run --plan "$PLAN/plan.json" --sha256 "$PLAN_DIGEST" --out "$EVIDENCE"
python3 -m workbench.swarm_investigate audit --plan "$PLAN/plan.json" --sha256 "$PLAN_DIGEST" \
  --report "$EVIDENCE/result.json" --report-sha256 "$RESULT_DIGEST"
```

The audit checks source quotes and verdicts against Git again without calling a model. A passing investigation establishes that valid source references were supplied. Runtime behavior still requires the offline execution workflow.
