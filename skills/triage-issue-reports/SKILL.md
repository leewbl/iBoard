---
name: triage-issue-reports
description: Classify one Benny report against source and GitHub issues, then prepare one local verdict.
disable-model-invocation: true
---

# Triage a single report

Freeze the explicitly selected report with [Benny's Slack source reader](../reproduce-and-fix-issues/SKILL.md#start-from-a-slack-report). Triage reads the same source and confirms it has not already received a trusted verdict. Give it the approved source excerpts at a fixed Git revision, a tracker repository, and the exact search phrases to use. Choose phrases from the reported trigger, symptom or error signature. Use an optional routing map with entries containing `path`, `area` and `destination`.

```sh
python3 -m workbench.benny_triage --token-file "$SLACK_KEY" --receipt "$ROUTE" \
  --sha256 "$ROUTE_SHA" --repo "$TARGET_REPO" --revision "$COMMIT" \
  --source 'src/feature.py:12-40' --tracker-repo OWNER/REPO \
  --search-phrase 'exact observed phrase' --search-source --out "$NEW_EVIDENCE_DIR"
```

`--search-source` includes the exact Slack permalink in a search on the selected GitHub tracker. Use it when the tracker is authorized to receive the permalink. `--search-phrase` sends only the selected phrase to the tracker. The original report, selected source and tracker results go to the configured Claude Code + GLM model for classification. The model gets a temporary read-only workspace with no Slack credentials or action tools. Its verdict must cite exact excerpts to support the reported facts, code observations and tracker match.

The local packet contains the original thread text, the committed source excerpts and recent history, bounded [GitHub search results](https://docs.github.com/en/rest/search/search#search-issues-and-pull-requests), any unread attachment count, and the proposal. If the source changes between intake and the verdict, the proposal is withheld. Missing evidence, incomplete deduplication, or unclear behavior also withholds a new-issue draft.

The recommendation may identify an already linked issue, a live or historical match, a plausible existing fix to verify, a configured destination, or a new local draft. An ambiguous report can retain one focused question. The configured verdict marker is a proposal for a coordinator. This command does not post it, issue pings, or write to the tracker. The separate verification entry still requires a trusted verdict from the live source thread.

## Attachments

Add `--attachments` to read the file IDs in the selected thread. Text logs and JSON responses can support a finding through an exact excerpt. The private evidence directory keeps the original bytes and their hashes. Downloads must match the Slack file, workspace and expected byte length. Links to other hosts and redirects remain unread.

Add `--inspect-media` to send supported screenshots and MP4 frame samples to the configured GLM vision model. This includes the attachment read. Supported pictures are PNG, JPEG and WebP. With FFmpeg and FFprobe installed, MP4 clips up to 90 seconds can supply three timestamped samples. The notes refer to the retained frame bytes and the requested sampling times. A sampled clip remains partial evidence. File collection reads up to eight unique attachments, up to 32 MB total, with a separate limit per file type.

Review media notes together with the original screenshot or frame. They cannot substantiate a verbatim fact about the report. Incomplete, oversized, unsupported or uninspected attachments still count as unread for the draft decision. Provider failure stops further model calls and preserves the files already read. The runner checks attachment hashes again before confirming the proposal.
