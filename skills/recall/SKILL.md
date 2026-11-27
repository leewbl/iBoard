---
name: recall
description: Recover project decisions and outstanding work from selected recent chats, Git, and GitHub.
disable-model-invocation: true
---

# Recall

Use when the user asks what happened on a project or wants to resume work on a named topic. Start with the requested workspace and time window. The usual window is seven days. Use all history or explicit dates when requested. Skip other projects, the current chat, and chats created for tests or subagent work.

Choose the relevant Claude Code transcripts from the workspace and pass their paths explicitly. Supply the current session ID if one of the selected files could refer to it. For a topic, select shared notes if available. Git searches matching commit messages on the current history. Give the GitHub repository name to verify same-repository PR and issue numbers. Explicitly pass local branches that matter.

```sh
python3 -m workbench.recall collect --repo "$TARGET_REPO" \
  --session "$CHAT_ONE" --session "$CHAT_TWO" --current-session "$CURRENT_ID" \
  --topic "$TOPIC" --forge "$GITHUB_REPO" --branch "$BRANCH" --out "$EVIDENCE"
python3 -m workbench.recall summarize --evidence "$EVIDENCE" \
  --sha256 "$EVIDENCE_SHA" --model "$OPENAI_MODEL" --out "$REPORT"
python3 -m workbench.recall show --path "$REPORT" --sha256 "$REPORT_SHA"
```

Inspect the local snapshot before using its digest in a read-only review. It includes selected text and excludes tool outputs. The reviewed report cites chat IDs and lines, Git commits, notes, and GitHub records. The lookup verifies open, draft, merged and closed PRs, issue state, and selected local branches. If GitHub reports a merge and the recorded base is present locally, the report can also flag a matching Git revert message. An unavailable lookup is marked unverified. End with one concrete next action.
