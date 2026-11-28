---
name: sharpen
description: Deepen the existing task brief with concrete methods, source material, independent checks and decisive experiments.
---

# sharpen

Use the original brief as `--request`. For code-backed tasks select committed excerpts with `--repo`, `--revision` and one or more `--source path:start-end` arguments. Explain how to check the outcome independently and which decisions can be settled by an experiment. Leave product preferences with the user.

From the iBoard root, run `python3 -m workbench.intent_review collect --mode sharpen --request /absolute/request.txt --out /absolute/intent-evidence.json` with the input options above. Run `python3 -m workbench.intent_review review --evidence /absolute/intent-evidence.json --sha256 EVIDENCE_SHA --model MODEL --out /absolute/intent-review.json`. Read the result with `show --report /absolute/intent-review.json --sha256 REPORT_SHA` on the same module. Use the returned SHA-256 at each step. The selected text and excerpts are sent to the configured reviewer. Keep the files in a private task directory.

For an approved supplemental clarification, prepare a task with `workbench.start --prepare-only` and use `python3 -m workbench.intent_review attach --task /absolute/task.json --report /absolute/intent-review.json --sha256 REPORT_SHA`. The original request bytes and any selected repository baseline must match. The task keeps its original scope and acceptance criteria.

An independently verified replay onto a descendant baseline can carry the same approved report. The workbench checks the original request and report again, then compares the contents and Git mode of every file used as source evidence between the two baselines. A changed or missing file blocks the replay before the patch is applied. The replay keeps the original approval digest and runs the frozen acceptance plan again.
