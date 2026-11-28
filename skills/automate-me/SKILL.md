---
name: automate-me
description: Turn recurring project working conventions into a reviewed personal mode skill.
disable-model-invocation: true
---

# Automate me

Use this workflow when the user asks for a personal mode skill or wants to update one. Identify the project and the mode name. If the user already has a matching Claude Code mode, confirm the source skill or use the file they specified explicitly.

Select relevant Claude Code JSONL sessions from this project. Review the collected user messages locally before passing their digest to the read-only model reviewer. Use a date cutoff when the selected history needs one. Updates to tracked skills use the date of their latest Git edit. For an untracked skill, supply `--since` if you include transcripts.

```sh
python3 -m workbench.automate_me collect --repo "$TARGET_REPO" \
  --session "$SESSION_A" --session "$SESSION_B" --out "$EVIDENCE"
python3 -m workbench.automate_me mine --evidence "$EVIDENCE" \
  --sha256 "$EVIDENCE_SHA" --model "$OPENAI_MODEL" --out "$PROPOSALS"
```

Repeat `--session` for more sessions. Add `--existing "$MODE_SKILL"` for an update. The collector includes text turns attributed to the user in the selected workspace. It records citations by session and JSONL line, including how many turns exceeded the excerpt bound. The reviewer receives selected text without paths or IDs identifying the original session. It mines separate calendar weeks in parallel and proposes rules supported by at least two weeks and two sessions. A single session, one week, or an empty capture can still serve as a starting point for directly stated preferences.

Show the candidate conventions with their citations. Ask the user which areas matter and what should be added or removed. Use Claude Code's question tool when available and ordinary chat in print mode. Capture the confirmed choices in a small approval file. Keep the approval file and evidence local.

```json
{
  "mode": "example-mode",
  "selected": ["P1"],
  "direct": [{"area": "response", "rule": "Lead with the result."}],
  "remove": []
}
```

The areas are `response`, `autonomy`, `scoping`, `delegation`, `quality`, `verification`, `delivery` and `skills`. Put any direct corrections here instead of editing the proposal evidence. During an update, `remove` matches the exact text of one existing bullet without its leading dash.

```sh
python3 -m workbench.automate_me draft --proposal "$PROPOSALS" \
  --sha256 "$PROPOSAL_SHA" --approval "$APPROVAL" --out "$DRAFT_DIR"
```

Read the resulting `SKILL.md` as an authoring draft. Keep only specific, durable conventions. Use existing local skill references and incorporate the user's feedback by changing the approval and rendering into a fresh draft directory. For updates, the tool checks that the original skill still matches the collected version. The draft preserves its existing sections and applies confirmed additions and exact removals.

When asked to land the result, work in a separate Git worktree. Put a new project skill in the relevant Claude Code skills directory or update the selected path. Check the diff and skill metadata, then use the project's authorized PR workflow.
