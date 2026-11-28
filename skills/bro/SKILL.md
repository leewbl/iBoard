---
name: bro
description: Rewrite the prior reply in plain language while retaining facts, numbers, commitments and uncertainty.
---

# bro

Save the prior reply verbatim as `--request`. Use no repository or reference inputs. Return the plain-language version from `answer.restatement`. This mode only edits text.

From the iBoard root, run `python3 -m workbench.intent_review collect --mode bro --request /absolute/request.txt --out /absolute/intent-evidence.json` with the input options above. Run `python3 -m workbench.intent_review review --evidence /absolute/intent-evidence.json --sha256 EVIDENCE_SHA --model MODEL --out /absolute/intent-review.json`. Read the result with `show --report /absolute/intent-review.json --sha256 REPORT_SHA` on the same module. Use the returned SHA-256 at each step. The selected text and excerpts are sent to the configured reviewer. Keep the files in a private task directory.

Read the prior reply alongside the restatement and check that uncertainty and commitments still have the same meaning.
