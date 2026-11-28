---
name: technical-writing
description: Draft or review technical documentation, README pages, RFCs, PR descriptions, or commit messages. Ground edits in project evidence.
disable-model-invocation: true
---

# Technical writing

For an article, choose one purpose. A tutorial teaches through visible results. A how-to guide leads the reader to a goal. Reference material organizes the exact interface for lookup. An explanation gives the reasoning behind a bounded topic. Use `--structure` to set the purpose or let the review propose one. A PR brief and a commit message lead with why the change was needed and link supporting evidence. They use no article structure.

From the iBoard root, run `python3 -m workbench.technical_writing collect --mode technical-writing --action rewrite --format doc --input /absolute/document.md --out /absolute/writing-input.json`. Use `--action audit` to review existing prose or `--action draft` to write from a brief. Select `doc`, `readme`, `rfc`, `pr`, or `commit` as the format. Use `--audience` for the intended reader. Pass `--repo`, `--revision`, and `--source path:start-end` to include exact source excerpts for claims and counts.

Use the same Python module for the remaining stages. The selected text and excerpts are sent to the configured reviewer. Keep the receipts outside the target checkout.

1. Run `review --evidence /absolute/writing-input.json --sha256 INPUT_SHA --model MODEL --out /absolute/writing-review.json`. The review applies the technical writing and unslop rules.
2. Run `show --report /absolute/writing-review.json --sha256 REVIEW_SHA` to see findings and the proposed text or diff.
3. Run `verify --report /absolute/writing-review.json --sha256 REVIEW_SHA --model MODEL --out /absolute/writing-verification.json`. A fresh session compares the exact draft with the original evidence. Audit reports end after inspection.
4. Run `export --verification /absolute/writing-verification.json --sha256 VERIFICATION_SHA --out /absolute/reviewed-document.md` to create a separate file when every check passes.

For rewrites, keep the commands, code blocks, links, exact values, warnings, and scope. Resolve any change to those facts in a separately approved edit to the source material. Pick the repository spelling for every symbol and verify counts against the revision that will land. Use sentence-case headings and give each instruction one main action.
