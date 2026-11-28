---
name: show-me-your-work
description: Keep and audit an append-only trail of iBoard task decisions, using actual commits and evidence.
disable-model-invocation: true
---

# Show the decisions for an iBoard task

Use the task record explicitly selected by the user. iBoard records workflow
decisions in `decisions.tsv` alongside task evidence. To inspect them, run from
the plugin root:

```sh
python3 -m workbench.decisions audit --task /path/to/task.json
```

Use `--run ID` for one run. Follow the original evidence. The audit confirms
artifact bytes and local commit identities. PR links and mutable task records
are marked separately so they can be inspected directly. A file hash alone
cannot prove that a decision or assertion was correct.

Record an additional decision or correction under the actual run identifier:

```sh
python3 -m workbench.decisions record --task /path/to/task.json --run RUN_ID \
  --phase reproduce --decision 'Selected the user-facing entry point' \
  --why 'It exercises the reported behavior' --artifact baseline-report.json \
  --result 'Verified the baseline behavior'
```

Use `--commit` with the full SHA, or `--pr` with an exact GitHub pull request
URL when appropriate. A different run receives a start marker that names the
time range of inherited rows. Append a correction when a row is wrong.

Before handing back a run, use a configured Codex model from OpenAI to review
the GLM stage receipts and the corresponding evidence:

```sh
python3 -m workbench.decision_review --task /path/to/task.json --run RUN_ID --model OPENAI_MODEL
```

This command sends the selected run's decisions, bounded transcript excerpts,
and evidence observations to the OpenAI reviewer. Use a run that the user
has authorized for that review. For an earlier run, explicitly select its
transcript inside the task directory with `--transcript FILE --work-provider VENDOR`.
The report distinguishes an observed GLM provider from a declared provider.

Report the reviewer's model and its specific flags under **Attention**.
An exit status of 1 indicates that the saved report contains flags. If no
review could be performed, report the missing review. Append corrections with
links to the review evidence and keep the prior rows.
