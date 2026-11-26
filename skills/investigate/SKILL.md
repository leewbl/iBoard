---
name: investigate
description: Explain selected committed code or its design history with verifiable references. Use how, why, teach, or investigation.
disable-model-invocation: true
---

# Investigate committed code

Choose a repository and a small set of relevant tracked files from the user's question. Use `how` for code behavior, `why` for design history, `teach` for an explanation that draws on both, and `investigation` for a cited engineering answer. Select a Git revision when the user names one. Use narrower line ranges when a file is long.

From the iBoard plugin root, run the read-only entry point. Choose a new report path outside the repository being investigated.

```sh
python3 -m workbench.investigate how --repo /path/to/repo \
  --revision HEAD --file src/entry.ts:20-100 \
  --question 'How does the request reach its handler?' \
  --report /path/to/reports/request-flow.json
```

For `why`, `teach` or `investigation`, include an explicitly selected GitHub review with `--pr https://github.com/owner/repo/pull/123`. Its head or merge commit must appear in the collected Git evidence. The history stage uses the PR title and body plus the first five entries from each discussion category.

The selected committed text, history and requested discussion are sent to the configured GLM provider. The model cannot modify the repository or execute project commands. Read the report's final stage, resolve the cited evidence IDs to source lines, commits or discussion URLs, and state any limitations that affect the answer. A blocked report is a blocked investigation.
