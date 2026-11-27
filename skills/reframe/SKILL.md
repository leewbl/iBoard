---
name: reframe
description: Connect a request and selected source to verified terminology, related methods and a more precise task brief.
---

# reframe

Pass a reference manifest with public primary URLs and passage anchors. For example, `[{"url":"https://git-scm.com/docs/git-worktree","anchors":["linked worktree"]}]`. Add it with `--references`. The collector opens each page and freezes the excerpts. Terms require an exact quotation containing the term. Show the references and explain which related methods transfer and where the analogy ends. Partial results preserve verified terms and withhold the rewritten brief. Reference discovery takes place before collection.

From the iBoard root, run `python3 -m workbench.intent_review collect --mode reframe --request /absolute/request.txt --out /absolute/intent-evidence.json` with the input options above. Run `python3 -m workbench.intent_review review --evidence /absolute/intent-evidence.json --sha256 EVIDENCE_SHA --model MODEL --out /absolute/intent-review.json`. Read the result with `show --report /absolute/intent-review.json --sha256 REPORT_SHA` on the same module. Use the returned SHA-256 at each step. The selected text and excerpts are sent to the configured reviewer. Keep the files in a private task directory.

For an approved supplemental clarification, prepare a task with `workbench.start --prepare-only` and use `python3 -m workbench.intent_review attach --task /absolute/task.json --report /absolute/intent-review.json --sha256 REPORT_SHA`. The original request bytes and any selected repository baseline must match. The task keeps its original scope and acceptance criteria.
