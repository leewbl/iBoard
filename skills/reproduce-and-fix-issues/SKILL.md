---
name: reproduce-and-fix-issues
description: Reproduce a reported behavior and independently verify the existing GitHub fix in offline Docker.
disable-model-invocation: true
---

# Verify an existing fix

## Start from a Slack report

Use the [Benny configuration](../setup-benny/SKILL.md) and select one report. The coordinator confirms the workspace, reads the complete root thread, and verifies the permalink. A verdict must come from the configured triage identity as a reply with one final marker line. The provider credential stays in the reader.

```sh
python3 -m workbench.benny freeze --config "$CONFIG" --token-file "$SLACK_KEY" \
  --channel "$CHANNEL_ID" --message-ts "$MESSAGE_TS" --out "$NEW_ROUTE_DIR"
```

Add `--thread-ts` for a reply event and `--wait` to poll within the configured budget. Explicit claims or common implementation commitments from people pause the new-fix route. Requests to diagnose and bot reports are not claims. A fix reference qualified by a `Fix:` or `[benny:fix]` line uses the read-only verification route. Add `--artifact` to select an exact approved GitHub URL when the thread has several fixes or the owner linked one elsewhere. The configured repository limits both kinds of selection.

Pass the exact `route.json` and returned hash to the existing-fix entry. The thread must still match the frozen source before and after execution.

```sh
python3 -m workbench.benny verify --token-file "$SLACK_KEY" --receipt "$ROUTE" \
  --sha256 "$ROUTE_SHA" --repo "$TARGET_REPO" --image "$BASE_IMAGE" \
  --manifest "$MANIFEST" --helpers "$HELPERS" --journey "$JOURNEY" \
  --oracle "$ORACLE" --oracle-marker OBSERVE_PASS --oracle-failure-marker OBSERVE_FAIL \
  --out "$NEW_EVIDENCE_DIR"
```

Keep the frozen route and evidence outside the target checkout. A source change retains the runtime verdict while withholding confirmation of that report. `eligible-for-reproduction` is an intake result. The command does not create a patch from it.

## Start from a GitHub artifact

Choose an open or merged pull request, or a full commit URL already on the repository's default branch. Use the artifact that addresses the original report. Preserve the source URL and observable behavior in a UTF-8 report. A branch name or a report that says a fix exists cannot identify the artifact.

Prepare a source-attested baseline image with the project's dependencies. Give the runner a [project verification manifest](../create-verification-skill/SKILL.md) containing a doctor and the failing user journey. The journey and a separate probe each need exact `*_PASS` and `*_FAIL` lines. Emit the fail line with exit `1` only after observing the stated symptom through the running application. A crash, failed launch or missing proof must not emit it. Use disposable test inputs, and verify any cleanup relevant to the check. Browser journeys must record the served commit and save a PNG on both outcomes. A non-UI probe can corroborate the outcome with an independent request or observable state check.

```sh
python3 -m workbench.verify_existing_fix --repo "$TARGET_REPO" --artifact "$GITHUB_ARTIFACT" \
  --report "$REPORT" --image "$BASE_IMAGE" --manifest "$MANIFEST" --helpers "$HELPERS" \
  --journey "$JOURNEY" --oracle "$ORACLE" --oracle-marker OBSERVE_PASS \
  --oracle-failure-marker OBSERVE_FAIL --out "$NEW_EVIDENCE_DIR" --timeout 900
```

The runner reads the artifact before and after verification. It uses the PR base and head for an open PR. For a merged PR it checks the landed revision. If it differs from the submitted head, the first parent supplies the pre-merge tree. For a merged full commit URL the first parent is the baseline. The baseline must be available locally and be an ancestor of the candidate. The runner derives the candidate image offline from the attested baseline. Each observation uses a new container with the same frozen helper and parameters for its probe. Checks leave the target checkout and GitHub PR unchanged, and never create a competing PR.

A result is `confirmed` only when both probes see the fault twice on the baseline and see the expected behavior twice on the existing fix. `insufficient-fix` means both probes still observe the same symptom twice. Mixed observations are `inconclusive`. Failed prerequisites are `environment-blocked`. If the artifact moved or cannot be read again, keep the runtime result and report `stale` or `artifact-unconfirmed`.

The output contains the frozen report and helper digests, raw baseline and candidate receipts, cleanup status, optional PNGs, and the final artifact recheck. Keep private evidence outside the target checkout. See the [OpenClaw CLI example](examples/openclaw-202294/README.md).

The source receipt includes the thread attachment IDs. [Triage](../triage-issue-reports/SKILL.md#attachments) can collect and inspect those files. Ambiguous ownership outside the supported phrases needs separate review. Source-thread replies, recordings of the verification run and handoff of a new fix to the delivery gate remain separate work.
