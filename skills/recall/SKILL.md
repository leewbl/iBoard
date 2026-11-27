---
name: recall
description: Recover project decisions and outstanding work from local Claude Code, Cursor or Codex chats, Git, and GitHub.
disable-model-invocation: true
---

# Recall

Use when the user asks what happened on a project or wants to resume work on a named topic. Start with the requested workspace and time window. The usual window is seven days. Use all history or explicit dates when requested. Skip other projects, the current chat, and chats created for tests or subagent work.

Choose local hosts with repeatable `--discover claude`, `--discover cursor` or `--discover codex`. Use `--session` for an exact Claude Code export, `--cursor-session` for a Cursor project transcript or `--codex-session` for a Codex rollout. Supply `--current-session` to exclude the active conversation. Codex reads its normal active and archived stores and defaults the exclusion to the host's current thread ID when available.

Discovery uses the selected Git root and sorts matching files by modification time. Cursor uses its canonical project directory and rejects ambiguous project paths. When a Cursor message has no timestamp, the evidence labels its time as inferred from the file's modification time. Select a narrower topic or time window when the bounded scan exceeds the limits. For a topic, select shared notes if available. Git searches matching commit messages on the current history. Give the GitHub repository name to verify same-repository PR and issue numbers. Explicitly pass local branches that matter.

```sh
python3 -m workbench.recall collect --repo "$TARGET_REPO" \
  --discover claude --discover cursor --discover codex --current-session "$CURRENT_ID" \
  --topic "$TOPIC" --forge "$GITHUB_REPO" --branch "$BRANCH" --out "$EVIDENCE"
python3 -m workbench.recall summarize --evidence "$EVIDENCE" \
  --sha256 "$EVIDENCE_SHA" --model "$OPENAI_MODEL" --out "$REPORT"
python3 -m workbench.recall show --path "$REPORT" --sha256 "$REPORT_SHA"
```

Inspect the local snapshot before using its digest in a read-only review. It keeps visible user and assistant messages, with host, session ID and original line numbers. It excludes tool output, model reasoning and host-injected messages. The reviewed report cites chat IDs and lines, Git commits, notes, and GitHub records. The lookup verifies open, draft, merged and closed PRs, issue state, and selected local branches. If GitHub reports a merge and the recorded base is present locally, the report can also flag a matching Git revert message. An unavailable lookup is marked unverified. End with one concrete next action. Cursor cloud-agent links still require separate source access.
