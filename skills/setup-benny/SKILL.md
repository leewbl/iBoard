---
name: setup-benny
description: Install Benny in a Git project, receive authorized Slack events, and configure its workers.
disable-model-invocation: true
---

# Set up Benny report verification

## Install and verify the project package

```sh
python3 -m workbench.benny_deploy preview --repo "$TARGET_REPO"
python3 -m workbench.benny_deploy install --repo "$TARGET_REPO"
python3 -m workbench.benny_deploy verify --repo "$TARGET_REPO"
```

The installer writes the inactive package under `.iboard/automations/benny/`. Review the preview, retain destination-only files and resolve any managed-file conflicts before installation. Commit the package in the target repository before running the readiness check. Run the check again from a fresh checkout at that commit. It reads the committed role files and records the pinned revision, manifest digest and currently resolvable native dependencies.

## Run the two event roles

Copy [worker-policy.example.json](worker-policy.example.json) outside the package. Select the source files and tracker searches for triage, then name the exact offline image, behavior driver and separate probe for reproduction. Policy file references are relative to the file. The example enables local evidence only. Authorize `triage.apply_verdict` and supply a separate `--writer-token-file` for the guarded original-thread reply and tracker handoff. Enable a `reproduce.repair` object with `fix_search`, `seconds`, `iterations` and `draft` for approved repair and optional draft delivery. A recorded browser driver also requires `baseline_visible` and `candidate_visible`.

```sh
python3 -m workbench.benny_automation serve --repo "$TARGET_REPO" \
  --config "$BENNY_CONFIG" --state "$PRIVATE_STATE" --app-id "$SLACK_APP_ID" \
  --signing-secret-file "$SLACK_SIGNING_SECRET_FILE" --selected-root "$APPROVED_ROOT_TS"
python3 -m workbench.benny_automation watch --repo "$TARGET_REPO" \
  --config "$BENNY_CONFIG" --state "$PRIVATE_STATE" --policy "$WORKER_POLICY" \
  --token-file "$SLACK_READ_TOKEN_FILE" --seconds 900 --max-events 8
```

The receiver listens on loopback at `/slack/events`. Use an authorized HTTPS endpoint for Slack delivery. Choose exact report roots with repeatable `--selected-root` so intake stays within the reports approved for this run. The receiver authenticates the raw request, records only its coordinates and acknowledges once the event is durable. The worker reads the original thread through Slack, then chooses the corresponding workflow. A trusted triage reply can wake reproduction, which still checks the original verdict and ownership. [Slack request signatures](https://docs.slack.dev/authentication/verifying-requests-from-slack/) · [Slack event deliveries and retries](https://docs.slack.dev/apis/events-api/)

The worker pins the Git deployment, routing rules and worker policy. Duplicate deliveries share one event receipt and one result per source and role. `watch` admits a bounded batch and revisits the original reproduction when the claim window ends. Native per-task limits govern the active model and Docker work. A replacement worker can use the same inputs with `work --event-id` to reconcile an approved handoff within its original deadline, resume from a completed reproduction, or recover the exact prepared repair task. The original reproduction receipts, worktree and input are checked before repair resumes. Draft delivery consults the existing branch and draft journal before further writes. An interruption before the reproduction receipt or repair input is complete leaves its files available for inspection.

### Optional Slack operations thread

In the worker policy, add `"operations": {"channel": "G0123456789", "identity": "U0123456789"}` inside `reproduce` to enable status updates. Choose a different channel from the report source. Use `--operations-writer-token-file` for the configured identity, or reuse `--writer-token-file` when it authenticates as the same identity. The read credential must be able to see the operations channel history and its threads in the same workspace. Slack scopes and conversation membership depend on the token type and channel. [Read channel history](https://docs.slack.dev/reference/methods/conversations.history/) · [Update a message](https://docs.slack.dev/reference/methods/chat.update/)

The coordinator creates one status message per approved report in the operations channel and updates it as the run progresses. After the verified draft is confirmed, the coordinator adds its GitHub link as a reply to that message. The event retains `operations.json` with the root, updates, reply and pending write intent. Restarting after an uncertain response reconciles that intent against Slack. A missing or ambiguous create receipt stops automatic posting and preserves the journal. Without an operations channel, the CLI and event receipts carry the status.

### Optional reproduction reply in the report thread

Set `source_notice_identity` inside the `reproduce` worker policy to an approved Slack user ID. Pass the corresponding owner-only credential file with `--source-notice-writer-token-file`, or reuse `--writer-token-file` for the same identity. The coordinator posts one concise reply only after both offline probes confirm the report. When the operations thread is enabled, the coordinator updates it first and then includes its link in the reply. A trusted tracker issue is linked when present.

The claim window starts from the later of confirmed reproduction and reply time. Later ownership checks recognize only the exact confirmed reply recorded in `source-notice.json`. Any additional message, edit or claim still pauses repair or draft delivery. An uncertain write is reconciled with the original thread. The coordinator preserves the intent when Slack cannot confirm the reply.

### Approve a corrected setup before the run

With source notices enabled, set `corrections` inside `reproduce` to a map from setup names to operator-approved `manifest`, `helpers`, and `oracle` paths. Benny freezes each setup before launching the original verification. A setup may change how the app is prepared and driven. The project, expected observation, success and failure markers, independent oracle outcomes and media requirements stay fixed.

During the original claim window, the reporter can reply `Correction: setup: clean-state` to select the configured `clean-state` setup. At the end of that same window, Benny runs the selected setup once against the original commit and image. The original evidence and the corrected evidence remain in separate directories. Repair uses only a confirmed corrected result. A late or unapproved correction, a second correction, a claim, or an edit to the report holds the workflow for inspection.

## Read or verify one approved report

Copy [configuration.example.json](configuration.example.json) to a project-specific file outside the target checkout. Fill in the Slack workspace, source channel, configured triage identity, GitHub target and time limits. Use a separate configuration for another source channel. Store the Slack credential in an owner-only file and supply it to iBoard as a path through `--token-file`. Keep it out of the project, configuration and any model prompt.

Choose a specific, explicitly authorized source message. Use the read actions in [reproduce-and-fix-issues](../reproduce-and-fix-issues/SKILL.md) to confirm the credential's workspace, the root permalink, and the trusted verdict. If a reply triggered the run, keep both that reply's `message_ts` and the root `thread_ts`. Use a known test report when exercising the integration. A start without a verdict returns `awaiting-verdict` or waits within the optional configured budget.

The Slack reader uses [auth.test](https://docs.slack.dev/reference/methods/auth.test/), [conversations.replies](https://docs.slack.dev/reference/methods/conversations.replies/) and [chat.getPermalink](https://docs.slack.dev/reference/methods/chat.getPermalink/). Ensure the read token is authorized for the source conversation. To include attachments in [triage](../triage-issue-reports/SKILL.md#attachments), authorize `files:read` for [files.info](https://docs.slack.dev/reference/methods/files.info/). The optional [handoff coordinator](../triage-issue-reports/SKILL.md#apply-a-reviewed-handoff) handles authorized tracker writes and thread replies using separate credentials and a reviewed plan. Live subscription setup and Cursor automation editor deployment require those host environments.

For runtime verification, also provide a source-attested local Docker image, an executable verification manifest, and the two approved probes for the behavior. The existing GitHub artifact must reside in the configured repository. A valid source receipt by itself only qualifies the starting point. The verifier still requires the before/after observations and unchanged source and artifact identities.

Set a duration when preparing a handoff if the coordinator should [watch for follow-up](../triage-issue-reports/SKILL.md#watch-one-follow-up-window) after a confirmed verdict. It uses the same authorized writer and tracker account. A resumed watch stays within the original window.
