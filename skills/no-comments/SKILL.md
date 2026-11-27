---
name: no-comments
description: Review scoped comments and suppressions, apply accepted comment-only deletions, and identify underlying code changes that still need verification.
disable-model-invocation: true
---

# Review comments and their underlying constraints

Use the caller's files, or the current diff against the chosen base. Select extra committed source or history with `--context PATH:START-END` when a claimed constraint needs evidence. Python comments are parsed locally. JavaScript and TypeScript need an existing local image and the absolute path of its Babel parser package below `/app`.

```sh
python3 -m workbench.no_comments collect --repo "$REPO" --base "$BASE" \
  --image "$IMAGE" --babel "$PARSER_PACKAGE" --out "$EVIDENCE"
python3 -m workbench.no_comments review --evidence "$EVIDENCE" \
  --sha256 "$EVIDENCE_SHA" --model "$REVIEW_MODEL" --out "$REVIEW"
```

Review what the fresh reviewer proposes. Check legal text, formatting or compiler directives, public document contracts and evidence of unavoidable platform behavior. The report keeps correctness suppressions actionable. Trace any doubtful constraint to the real code or its history. Keep the scope fixed.

For an existing GLM configuration, pass `--provider glm` and the configured model name. The receipt records the selected provider.

```sh
python3 -m workbench.no_comments apply --review "$REVIEW" --sha256 "$REVIEW_SHA" \
  --approve C1 --out "$APPLICATION"
```

Each approved ID refers to one ordinary comment. The helper reparses the result and checks the staged index before applying. Run the applicable project tests and checks before delivery. Protect pragmas and doc-comment blocks unless their semantics have been checked separately.

Use [Architect](../architect/SKILL.md) for scoped changes to the code shape and [Correct](../correct/SKILL.md) for an approved enforcement test. Treat unresolved rules, suppressions and needed encodings as open actions. In the handoff, report the deletion count, retained exceptions, review retries and the remaining code changes or tests.
