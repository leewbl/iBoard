---
name: maintain-formal-verification-skill
description: Review changes to verified code, replay the old and current properties, and keep verification in sync without modifying the product.
disable-model-invocation: true
---

# Maintain a formal verification skill

Identify the installed project skill by its property and implementation coverage. Read its sync commit, assumptions, bounds, drivers and fixtures. Use the approved bundle digest from its creation or last maintenance report. If a project lacks a bundle, use [creation](../create-formal-verification-skill/SKILL.md) first.

```sh
python3 -m workbench.formal_verify maintain --repo "$TARGET_REPO" --revision "$REVISION" \
  --skill "$SKILL_DIR" --skill-sha256 "$BUNDLE_DIGEST" --out "$FIRST_PASS"
```

Read the as-installed receipt even if it passed. Read all commits in `history.json`, their covered changes, moved or renamed implementation, and any new writers of guarded state, including writers outside earlier coverage. Match the model or driver against the real code, including whether transitions are atomic. If verification no longer describes the code, update the verification files. Follow plausible counterexamples through the implementation before declaring a defect. Keep any proposed product fix in a separate task or commit.

Use the SHA-256 of `history.json` in a review record. The `commits` object maps every listed commit SHA to an `effect` and a source-based `reason`. Use `no-effect`, `verification-drift`, `intentional-contract-change` or `possible-bug`. Preserve the original property and bounds while bringing the verifier back in sync. If a documented promise intentionally changed, record the supporting `intent` as a tracked `path`, unique source text in `contains`, and explanatory `meaning`. Pass `--approve-contract-change` with the approved SHA-256 of the replacement manifest file. The report retains old and new conditions.

```sh
python3 -m workbench.formal_verify maintain --repo "$TARGET_REPO" --revision "$REVISION" \
  --skill "$SKILL_DIR" --skill-sha256 "$BUNDLE_DIGEST" --review "$HISTORY_REVIEW" \
  --manifest "$REVISED_MANIFEST" --helpers "$REVISED_HELPERS" --out "$FINAL_EVIDENCE"
```

Omit the manifest and helpers when the installed checker already describes the code. Follow the recheck command in a newly generated bundle and attach the verification diff. A fresh mutation must be caught before advancement, and an unchanged revision must reproduce the passing outcome. Report `in-sync`, `updated`, `bug`, `review-required` or `blocked`. Only advance the sync point on a validated passing result. A confirmed bug needs a matching real-code replay. A differing replay needs investigation of the model, harness or environment. For a confirmed product defect, trace the witness through the examined revisions to locate the introducing commit when feasible, and explain its impact and remaining coverage in the handoff.
