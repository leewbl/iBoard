---
name: reflect
description: Review one project session and apply approved improvements to local copies of existing skills.
disable-model-invocation: true
---

# Reflect

Use a completed or current Claude Code session when the user asks to reflect. Identify its JSONL transcript in the current workspace and the existing skills relevant to the work. Include a missed skill if its trigger may need a more precise description.

Collect the session and selected skills locally. Inspect the evidence before passing its digest to the read-only reviewer. The snapshot records event line numbers, omitted text, both source and local file hashes, and each proposed destination.

```sh
python3 -m workbench.reflect collect --repo "$TARGET_REPO" --session "$SESSION_JSONL" \
  --skill "$FIRST_SKILL_MD" --skill "$SECOND_SKILL_MD" --out "$EVIDENCE"
python3 -m workbench.reflect propose --evidence "$EVIDENCE" \
  --sha256 "$EVIDENCE_SHA" --model "$OPENAI_MODEL" --out "$PROPOSAL"
```

Destinations default to the current project's `.claude/skills/<skill>/`. Pass `--user-scope` during collection when the user explicitly wants a personal copy. The current local copy supplies the editable text when one already exists. Installed plugin files can supply the initial template.

Present the proposal in three groups. For an edit, show its problem, citation, destination, and exact replacement. In the rejected group, explain which case is temporary or already covered. Put behavior a test or script can catch in the backlog. Obtain approval for specific edit IDs.

```sh
python3 -m workbench.reflect apply --proposal "$PROPOSAL" --sha256 "$PROPOSAL_SHA" \
  --approve R1 --out "$RECEIPT"
```

Repeat `--approve` for additional selections. An approved body edit must refer to a skill used in the session. A trigger edit changes the single-line description. Apply rechecks source and destination hashes and retains accompanying files. A new local copy includes the source folder. An update changes just the reviewed `SKILL.md` content. The local receipt records planned and written hashes for each selected skill. Keep these files with the review. Reflection delivers local skill copies.
