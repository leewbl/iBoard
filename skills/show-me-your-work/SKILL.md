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

If the user supplies the specific run transcript, compare the decisions with
that transcript. Call out gaps and weak evidence. Record which reviewer and
which source you actually used. Only label the audit cross-vendor after a
reviewer from another model provider has performed it.
