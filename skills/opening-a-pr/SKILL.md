---
name: opening-a-pr
description: Prepare and open a ready GitHub pull request from an independently verified iBoard task.
---

# Open a verified PR

Use the task's isolated worktree. Before the candidate is handed to the independent verifier, review the diff for unnecessary changes and keep related commits together. Use the normal task evidence for the exact candidate.

Write a short JSON brief with the request and intended public wording.

```json
{"type":"fix","scope":"gateway","subject":"Keep readSession bounded","why":"Large reads could leave the session stuck.","changes":["Bound the read and preserve its result contract."],"symbol":{"name":"readSession","path":"src/session.ts"}}
```

Include `tradeoff` only if a rejected alternative affects review. Include `symbol` when a changed symbol carries the change and name it in the subject. The compiler checks that the symbol occurs in the changed file. The task diff supplies scope and the independent review supplies observed impact.

For a stacked follow-up, set `parent_pr` to the open PR number in the same fork. Its head must be the base commit used to verify the child task. The handoff targets that parent branch and checks the exact parent again before publishing. If the parent moves or is merged first, update the task's base, independently reverify, and prepare a new receipt.

```sh
python3 -m workbench.pr_brief prepare --task "$TASK" --repository "$GITHUB_REPO" \
  --manifest "$PUBLIC_BRIEF" --out "$BRIEF_RECEIPT"
python3 -m workbench.pr_brief open --task "$TASK" --repository "$GITHUB_REPO" \
  --briefing "$BRIEF_RECEIPT" --sha256 "$BRIEF_SHA"
```

Prepare and inspect the receipt first. Use `attach` with the same flags as `open` to hand it to an already authorized publication step. The PR title, short ordered sections, commit, review digest and screenshot or documentation links are checked before publication. The task retains the original logs. A changed candidate or review needs a new receipt. Existing draft PRs require a separate ready decision.

Opening returns the PR URL. Use the [stack workflow](../autopilot-stack/SKILL.md) when the PRs are ready for stack verification and delivery.
