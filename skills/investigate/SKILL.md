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

Use `--discover-prs owner/repo` to discover the associated GitHub reviews for
the selected code. The lookup starts with the commits Git attributes the
selected lines to, then checks recent file history. It makes up to six
commit lookups and reads up to two associated PRs with the first two entries
from each discussion category. For a shallow checkout, it also checks that a
selected path appears in the PR's first page of changed files before using
the discussion. Empty lookups, unavailable records and partial discussion
are recorded separately. Use the upstream repository name when examining a
fork for upstream design history.

The selected committed text, history and requested discussion are sent to the configured GLM provider. The model cannot modify the repository or execute project commands. Read the report's final stage, resolve the cited evidence IDs to source lines, commits or discussion URLs, and state any limitations that affect the answer. A blocked report is a blocked investigation.

For a subsystem lesson, add `--lesson /path/to/reports/request-flow.html` to `teach`. Give the question a concrete user action or input and tell the workflow what the reader already knows when that matters. The lesson opens with a plain explanation. Its first figure shows two connected parts. Each later step keeps the earlier figure and adds one connection or one connected part. The underlying how and why findings are available with their evidence. Simple explanations can omit the figures. The HTML opens offline. Use a new output path outside the source repository.
