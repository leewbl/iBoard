---
name: architect
description: Compare two code-grounded designs before editing and check later commits against the selected boundaries and obligations.
disable-model-invocation: true
---

# Compare designs before editing

Trace the existing user path and every system it touches. Select committed source ranges for entry points, callers, and state writers. Give the design question, known constraints and the paths approved for the eventual change. Narrow the source ranges if they exceed the inspection bound.

```sh
python3 -m workbench.architect plan --repo "$TARGET_REPO" --revision "$BASELINE" \
  --question "$DESIGN_QUESTION" --file "src/module.ts:10-80" --edit-path "src/module.ts" \
  --model "$OPENAI_MODEL" --out "$DESIGN_EVIDENCE"
```

GLM and the configured OpenAI model propose separate structures. Each proposal names its modules, one owner for each state item, proposed signatures, callers, tradeoffs, and static and runtime obligations. Current-code findings cite the frozen Git commit and actual source lines. A separate OpenAI session compares the two proposals without receiving the provider labels. Review `outline.md` and the full `result.json` before authorizing an edit. A contested recommendation preserves both alternatives. For a complex or contested design, use [Arena](../arena/SKILL.md) to validate representative implementations against an agreed acceptance check.

```sh
python3 -m workbench.architect review --repo "$TARGET_REPO" --revision "$CANDIDATE" \
  --design "$DESIGN_EVIDENCE/result.json" --design-sha256 "$APPROVED_DESIGN_SHA256" \
  --model "$OPENAI_MODEL" --out "$REVIEW_EVIDENCE"
```

The review binds the approved report to the exact candidate commit. It checks scope before sending any diff to a model and requires changed content lines for supported static obligations. `static-conforms` means the static obligations were substantiated in the diff. Runtime checks stay pending and require separate execution. `pending-verification` indicates static evidence is also incomplete. Revise the implementation or explicitly approve a new design when the review detects drift. Retain both reports with the implementation evidence.
