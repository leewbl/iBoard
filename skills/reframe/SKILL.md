---
name: reframe
description: Connect a request and selected source to verified terminology, related methods and a more precise task brief.
---

# reframe

Read the operator’s original request and relevant committed source. Derive a short public topic that describes the technical problem and related fields. Supply it as `--search-topic` together with `--search-model MODEL`. The live search session receives only the public topic. Keep internal identifiers, source excerpts and the original request out of the topic. Search results are candidates. The collector opens the returned pages independently and freezes the matched excerpts. `show` includes queries and the disposition of each suggested page.

For explicitly chosen sources, use `--references` instead. Supply public primary URLs and passage anchors, for example `[{"url":"https://git-scm.com/docs/git-worktree","anchors":["linked worktree"]}]`. Terms in either mode require an exact quotation containing the term. If any quotation fails, the reviewer gets one chance to correct it using the same excerpts. Show the references, what transfers from related fields and where each analogy ends. Partial results preserve verified terms and withhold the rewritten brief.

From the iBoard root, run `python3 -m workbench.intent_review collect --mode reframe --request /absolute/request.txt --out /absolute/intent-evidence.json` with the input options above. Run `python3 -m workbench.intent_review review --evidence /absolute/intent-evidence.json --sha256 EVIDENCE_SHA --model MODEL --out /absolute/intent-review.json`. Read the result with `show --report /absolute/intent-review.json --sha256 REPORT_SHA` on the same module. Use the returned SHA-256 at each step. The selected text and excerpts are sent to the configured reviewer. Keep the files in a private task directory.

For an approved supplemental clarification, prepare a task with `workbench.start --prepare-only` and use `python3 -m workbench.intent_review attach --task /absolute/task.json --report /absolute/intent-review.json --sha256 REPORT_SHA`. The original request bytes and any selected repository baseline must match. The task keeps its original scope and acceptance criteria.
