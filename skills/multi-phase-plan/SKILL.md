---
name: multi-phase-plan
description: Write a dependency-ordered multi-PR plan, then run and audit its explicitly approved phases with unit, live and performance proof.
disable-model-invocation: true
---

# Plan a staged change

Use a single bounded change for an obvious one- or two-file task. For staged work, resolve open questions with prototype evidence first. Record any operator choice that an experiment cannot settle. Review the existing code and the selected [design](../architect/SKILL.md) before choosing which responsibilities can land independently.

The manifest fixes `repo`, `revision`, `request`, source `files` with optional line ranges, and editable `scope`. Set `obvious` explicitly. List unresolved `open_questions` and input `prototypes`, each with `question`, `conclusion`, `evidence` and `sha256`. An optional `design` identifies a selected Architect report by `path` and `sha256` for the same Git baseline. Supply a structured `plan` or choose an OpenAI model to draft one from this evidence.

Use `kind: "native"` to import an [iBoard prototype](../prototype/SKILL.md) result. The planner verifies the result, exact conclusion, source baseline, raw observations and visual evidence when present. A matching native recommendation can resolve that exact open question. Other questions remain pending.

```sh
python3 -m workbench.multiphase --manifest "$PLAN_MANIFEST" \
  --model "$OPENAI_MODEL" --out "$PLAN_EVIDENCE"
```

The plan contains a summary and one entry per proposed PR in landing order. Each entry names its phase `id`, branch `base` (trunk or a declared predecessor), `depends_on`, allowed `files`, one `change`, expected `observed_result`, `interaction` flag, and committed-source `citations` by ID and line. Independent phases have disjoint edit scopes. Each of these verification entries is required:

| Field | Evidence to plan |
| --- | --- |
| `unit` | The exact `scenario`, `predicate` and standalone `marker`. Emit the marker only after the check passes. |
| `live` | A distinct real user path with a `scenario`, observable `predicate` and different `marker`. |
| `perf` | The benchmark `probe` claim, `metric` set to `median_ms`, and numeric `max_ms`. Measure trunk first. |

Hand over `plan.md` and `result.json` for authorization. Unresolved questions leave the request in `needs-prototype`. A planned status establishes scope and order. Acceptance stays pending until the phase has evidence.

## Dispatch an approved plan

After the operator authorizes execution, prepare a separate manifest that refers to the native `result.json` by path and SHA-256. Declare one step for every phase. The unit and live commands must start distinct tracked `/app/` scripts from the approved trunk. Keep these entrypoints outside every phase's edit scope. Each script must print the exact planned marker on its own line. For a browser journey add a `screenshot` check using a tracked `/workspace/` script and the ordinary iBoard screenshot contract.

```json
{
  "repo": "/path/to/project",
  "plan": {"path": "plan/result.json", "sha256": "<approved plan SHA-256>"},
  "seconds": 1800,
  "iterations": 2,
  "memory_mb": 1024,
  "steps": {
    "stageOne": {
      "image": "local-project-baseline:node24",
      "unit": ["node", "/app/unit-stage-one.mjs"],
      "live": ["node", "/app/live-stage-one.mjs"],
      "feature_doc": "FEATURE.md",
      "benchmark": {"path": "benchmark.json", "sha256": "<approved contract SHA-256>"},
      "helpers": {"probe.mjs": {"path": "probe.mjs", "sha256": "<approved helper SHA-256>"}}
    }
  }
}
```

```sh
python3 -m workbench.phase_dispatch run --manifest "$DISPATCH" \
  --sha256 "$APPROVED_DISPATCH_SHA256" --out "$DISPATCH_EVIDENCE"
```

Each eligible phase gets a distinct task and worktree at its declared Git parent. All listed prerequisites must have accepted local evidence. The dispatcher measures the original trunk before the model edits. After the change, it validates the candidate unit and live runs and measures performance on the same pinned candidate image. The independent review must cite both executions. The final source diff must stay inside the phase scope. Independent phases can proceed as their own prerequisites clear. Call the same command with the same evidence directory to continue within the original deadline. Completed phases are audited again before dependent work starts.

A change to an interaction needs explicit approval of its exact commit and evidence, using `python3 -m workbench.phase_dispatch approve --task "$PHASE_TASK" --candidate "$CANDIDATE" --result-sha256 "$PHASE_RESULT_SHA256" --approved-by "$OPERATOR" --reason "$REVIEW_REASON"`. Run the dispatcher again to release dependent local tasks.

## Land the approved phases

To authorize GitHub delivery, include `destination` as the personal fork's exact `owner/repo` in the original dispatch manifest. Run the separate shipping command with the required workflow names. Shipping shares the dispatch deadline and records its remote write intents.

```sh
python3 -m workbench.phase_ship --manifest "$DISPATCH" --sha256 "$APPROVED_DISPATCH_SHA256" \
  --dispatch-root "$DISPATCH_EVIDENCE" --ci-workflow "$REQUIRED_WORKFLOW"
```

Shipping checks the original proofs and each PR against current GitHub state before each change. It advances after GitHub confirms the exact fast-forward, merge, or squash it recorded. When that landing changes the next phase's base, iBoard replays its exact linear patch history in a new worktree and reruns the unit, live, performance, and independent review gates. An existing approved OpenClaw local test policy is carried forward and rerun. A new interaction candidate needs its own approval. A conflict, unexpected main change, or unconfirmed write pauses shipping and preserves its journal. Keep the original task and replay evidence available across resumes. To test a phase on a different exact local commit without publishing, use `workbench.phase_reverify`.

## Audit a planned phase

```sh
python3 -m workbench.phase_audit --repo "$TARGET_REPO" \
  --plan "$PLAN_EVIDENCE/result.json" --plan-sha256 "$APPROVED_PLAN_SHA256" \
  --receipts "$RECEIPT_INDEX" --out "$AUDIT_EVIDENCE"
```

The receipt index binds `plan_sha256` and maps phase IDs in `phases` to a full `commit` and four path/digest references. Use separate `unit` and `live` sandbox results for the same pinned candidate image. Retain `baseline_perf` from a prior trunk comparison and `perf` from the trunk-to-candidate comparison using the same frozen [benchmark](../benchmark-checklist/SKILL.md). Each reference contains `path` and `sha256`. The audit rechecks scope, raw samples and the median. Passing an interaction phase's technical gates still leaves it awaiting operator review. An unresolved dependency keeps later phases waiting. Audit results inform the handoff and the operator's decision.
