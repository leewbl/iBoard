---
name: pickup
description: Identify where to resume an iBoard task, earlier project conversation, branch, or GitHub PR.
disable-model-invocation: true
---

# Pick up a retained task

Use the exact task path the user selected. From the iBoard plugin root run:

```sh
python3 -m workbench.pickup --task /path/to/task.json
```

Add `--check-pr` to read current GitHub state for the recorded candidate. Add `--since 2026-11-22T00:00:00Z` to limit the event timeline. If the task lock is held, report that a cooperating process owns the task. A historical publication or merge record indicates what already happened. A live PR read can confirm its current head and state.

Summarize inherited evidence, outstanding blockers, and the next safe step. Use `resume-from-…` as a recovery hint. The normal resume and delivery guards still apply. Avoid treating old review metadata or passing status checks as proof of a newly changed candidate. Do not restart a task during a catch-up.

## Pick up a prior conversation, branch, or PR

For a selected local Claude Code, Cursor or Codex conversation, use the [recall workflow](../recall/SKILL.md) to recover decisions and unfinished work. Give the resulting report and its digest to the pickup command. A report with one GitHub PR or just one local branch can supply the target. Choose the target explicitly when the report is ambiguous. If the user starts from a branch or GitHub PR, the recall report is optional.

```sh
python3 -m workbench.pickup_history --repo "$TARGET_REPO" \
  --forge "$GITHUB_REPO" --pr "$PR_NUMBER" \
  --recall-report "$REPORT" --recall-sha256 "$REPORT_SHA" --out "$RECEIPT"
```

For a local branch use `--branch NAME --base BASE`. For a pushed GitHub branch without a PR use `--forge OWNER/REPO --remote-branch NAME --base BASE`. The selected commits must be present locally. A PR supplies its current base automatically. Pass an earlier `--base` or the matching `--task` when the historical baseline matters. Use `--goal` to state the original request if it is available separately.

The receipt records which information came from the prior conversation, the current Git commit and worktree, what changed since the common base, whether the PR or base moved, and matching task evidence. Describe where the earlier work stopped, what was inherited, and the recommended next playbook. An active or held task waits, a closed PR routes to investigation, and a merged PR routes to comparing the landed behavior with the original goal. Do not repeat retained checks during pickup. The routed playbook is responsible for any further verification or delivery.
