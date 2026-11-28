---
name: in-your-own-words
description: Restate the user's goal, underlying problem, constraints and unknowns with citations to the original request.
---

# in-your-own-words

Use the user's actual request as `--request`. Keep constraints that were stated and mark unresolved decisions as unknown. Cite user lines for each claim. The result can accompany a prepared iBoard task.

From the iBoard root, run `python3 -m workbench.intent_review collect --mode in-your-own-words --request /absolute/request.txt --out /absolute/intent-evidence.json` with the input options above. Run `python3 -m workbench.intent_review review --evidence /absolute/intent-evidence.json --sha256 EVIDENCE_SHA --model MODEL --out /absolute/intent-review.json`. Read the result with `show --report /absolute/intent-review.json --sha256 REPORT_SHA` on the same module. Use the returned SHA-256 at each step. The selected text and excerpts are sent to the configured reviewer. Keep the files in a private task directory.

For an approved supplemental clarification, prepare a task with `workbench.start --prepare-only` and use `python3 -m workbench.intent_review attach --task /absolute/task.json --report /absolute/intent-review.json --sha256 REPORT_SHA`. The original request bytes and any selected repository baseline must match. The task keeps its original scope and acceptance criteria.
