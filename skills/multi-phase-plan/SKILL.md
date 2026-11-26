---
name: multi-phase-plan
description: Write and audit a dependency-ordered multi-PR plan with separate unit, live and performance proof per stage.
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

## Audit a planned phase

```sh
python3 -m workbench.phase_audit --repo "$TARGET_REPO" \
  --plan "$PLAN_EVIDENCE/result.json" --plan-sha256 "$APPROVED_PLAN_SHA256" \
  --receipts "$RECEIPT_INDEX" --out "$AUDIT_EVIDENCE"
```

The receipt index binds `plan_sha256` and maps phase IDs in `phases` to a full `commit` and four path/digest references. Use separate `unit` and `live` sandbox results for the same pinned candidate image. Retain `baseline_perf` from a prior trunk comparison and `perf` from the trunk-to-candidate comparison using the same frozen [benchmark](../benchmark-checklist/SKILL.md). Each reference contains `path` and `sha256`. The audit rechecks scope, raw samples and the median. Passing an interaction phase's technical gates still leaves it awaiting operator review. An unresolved dependency keeps later phases waiting. Audit results inform the handoff and the operator's decision.
