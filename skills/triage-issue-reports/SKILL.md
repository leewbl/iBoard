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

The recommendation may identify an already linked issue, a live or historical match, a plausible existing fix to verify, a configured destination, or a new local draft. An ambiguous report can retain one focused question. The configured verdict marker is a proposal. The handoff below applies a reviewed proposal. The separate verification entry still requires a trusted verdict from the live source thread.

## Attachments

Add `--attachments` to read the file IDs in the selected thread. Text logs and JSON responses can support a finding through an exact excerpt. The private evidence directory keeps the original bytes and their hashes. Downloads must match the Slack file, workspace and expected byte length. Links to other hosts and redirects remain unread.

Add `--inspect-media` to send supported screenshots and MP4 frame samples to the configured GLM vision model. This includes the attachment read. Supported pictures are PNG, JPEG and WebP. With FFmpeg and FFprobe installed, MP4 clips up to 90 seconds can supply three timestamped samples. The notes refer to the retained frame bytes and the requested sampling times. A sampled clip remains partial evidence. File collection reads up to eight unique attachments, up to 32 MB total, with a separate limit per file type.

Review media notes together with the original screenshot or frame. They cannot substantiate a verbatim fact about the report. Incomplete, oversized, unsupported or uninspected attachments still count as unread for the draft decision. Provider failure stops further model calls and preserves the files already read. The runner checks attachment hashes again before confirming the proposal.

## Apply a reviewed handoff

Prepare from the exact approved `result.json` and its SHA-256. Use one private shared `STATE_DIR` for the coordinator. The source workspace, channel and root timestamp choose the journal directory, so repeated attempts use the same plan. Choose any configured GitHub issue labels at preparation time.

```sh
python3 -m workbench.benny_handoff prepare --triage "$RESULT" \
  --sha256 "$RESULT_SHA" --state-dir "$STATE_DIR"
```

Read the returned preview and approve the exact plan digest for the selected repository and thread. Run `apply` only when authorized to create or comment on the issue and send the Slack reply. Supply the read credential and a separate owner-only writer credential for the configured triage identity. The local GitHub CLI account must be authorized for the tracker. Before a new issue, the tracker must confirm access to the labels and permission to close the issue if handoff fails.

```sh
python3 -m workbench.benny_handoff apply --plan "$PLAN" --sha256 "$PLAN_SHA" \
  --read-token-file "$SLACK_READ_KEY" --write-token-file "$SLACK_WRITE_KEY"
```

New issues carry the original Slack link and an exact run marker. A confirmed duplicate gets a recurrence comment, preserving the issue body and state. The coordinator rereads the source immediately before a reply, sends only via Slack's [thread reply parameters](https://docs.slack.dev/reference/methods/chat.postMessage/), then checks the same thread for the posted verdict. Resume with the same plan to reconcile a lost acknowledgement. A request with an unknown outcome is not sent again. An explicit reply rejection and a fresh complete thread read can close an issue created by this run with `not_planned`. The run rereads the issue and checks that state. A source change before sending can also trigger that same scoped compensation. An existing issue is never closed by the handoff.

The local journal records every stage and whether GitHub or Slack needs reconciliation. Attachments remain local. Live tenant validation and attachment publishing are separate.

## Watch one follow-up window

To enable follow-up, include `--followup-seconds 600` when preparing the original handoff. The window starts with the confirmed verdict. After an authorized handoff, the coordinator can watch the original thread with the same plan and credentials.

```sh
python3 -m workbench.benny_followup --plan "$PLAN" --sha256 "$PLAN_SHA" \
  --read-token-file "$SLACK_READ_KEY" --write-token-file "$SLACK_WRITE_KEY" \
  --seconds 60 --poll-seconds 15
```

The coordinator responds only to human questions addressed to the configured triage identity. Claude Code + GLM can use the frozen report, verdict and tracker link. An answer needs an exact supporting excerpt and contains no new triage marker. A directed correction from the reporter can append a field-specific comment to the same open GitHub issue. Write it as `@Benny Correction: Environment: macOS 15`, or use `Version`, `Trigger`, `Expected`, `Observed` or `Frequency`. The comment links the correcting message and leaves existing issue fields intact.

Each outgoing effect is journaled against the source message. Resume within the same window to reconcile a lost response without sending it again. A stop request such as `Stop Benny.` closes the window. An altered or missing initial verdict also stops follow-up. Repeated invocations cannot move the deadline. The command consumes up to its own `--seconds` budget inside the original window.
