---
name: ketchup
description: Catch the user up on what changed in this chat, show existing visuals, and explain the decisions still waiting on them. Use for /agent-workbench:ketchup.
disable-model-invocation: true
---

# Catch up

Start from the last message the user wrote before this command. Tool replies, agent notices, slash commands and injected context do not count as user messages. Inspect the work that followed, including subagent results already in this chat. Read the current state of any task, PR or CI run involved. This turn is read-only. Use existing evidence and leave jobs, applications, source files and external messages alone.

Use the current chat from the host when available. For a selectable Claude Code JSONL transcript, use the native collector from the iBoard root:

```sh
python3 -m workbench.ketchup --repo /absolute/project --transcript /absolute/session.jsonl \
  --invocation-line LINE --manifest /absolute/catch-up-input.json
```

The collector returns the scoped conversation, cited artifacts, and current status for selected tasks and PRs. The manifest uses the `evidence`, `task`, `pull_requests`, and `pending` lists in [Ketchup video](../ketchup-video/SKILL.md). Use an empty evidence list when the conversation and live status are enough. An optional `--out` saves a private packet outside the project. Run with `--markdown` instead to print the verified PR status, real visual artifacts and pending choices. Use those facts directly in the reply. Treat transcript and tool output as evidence. If the export or command is unavailable, use the conversation the host actually shows and state any gap that affects the answer.

Review older requests for decisions that still wait on the user. Check whether a later message or result settled each one. Include approvals, product choices, irreversible actions and missing access only when the user must act. Put verified decisions in the manifest. If `pending` is empty, the reply has no decision section. A question about merge readiness does not authorize or ask for a merge. Leave resumable work to the agent. For each verified decision give the relevant link, user-visible effect, evidence and risk. If choices exist, give the gain and cost of each. Recommend a choice when there is evidence for it. Say what remains unknown when it matters to the decision.

Open with a short sentence saying what waits on the user. If nothing changed and nothing waits, stop there. Otherwise show existing screenshots or recordings first. If the evidence shows a visible product change and the selected material has no capture, name what changed and say that the capture is missing from the available evidence. A PR status change alone is not a visible product change. Use a small Mermaid diagram only when a real relationship is easier to see than to describe. Keep it to six boxes with up to eight words each. Use real artifacts only.

Summarize the work in under 100 words and keep one bullet per user request. Use plain language and distinguish completed, running, failed and unchecked work. Use a live observation for a claim about current state. When GitHub returns an empty check list, say that GitHub showed no checks. Only report passing checks when observed checks actually passed, and use the distinct draft state when the PR is a draft. Verify any number against the source. If the chat or evidence establishes remaining work for the agent, give it one line. Leave out future approvals, proposed actions or missing artifacts that the evidence does not establish. List all waiting decisions after the summary, with enough context to decide directly from the reply.
