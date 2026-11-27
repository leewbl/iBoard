---
name: reproduce-and-fix-issues
description: Reproduce a trusted report and independently verify a candidate in offline Docker.
disable-model-invocation: true
---

# Reproduce a report and verify the fix

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

Keep the frozen route and evidence outside the target checkout. A source change retains the runtime verdict while withholding confirmation of that report. `eligible-for-reproduction` is an intake result. Use the new fault entry below for that route.

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

The source receipt includes the thread attachment IDs. [Triage](../triage-issue-reports/SKILL.md#attachments) can collect and inspect those files. Ambiguous ownership outside the supported phrases needs separate review. The coordinator can [post the reviewed triage verdict](../triage-issue-reports/SKILL.md#apply-a-reviewed-handoff).

## Reproduce an unclaimed fault

The selected route must have the `eligible-for-reproduction` verdict. Pin a local revision and its source-attested image, then use the same manifest, doctor and separate probe described above. The coordinator rereads the original thread before and after the offline runs. Both probes must observe the exact symptom twice, each in a fresh read-only container. The doctor reruns after each failure. Keep the report, driver and raw receipts together outside the target checkout.

```sh
python3 -m workbench.benny_reproduce reproduce --token-file "$SLACK_KEY" \
  --route "$ROUTE" --sha256 "$ROUTE_SHA" --repo "$TARGET_REPO" \
  --revision "$BASE_COMMIT" --image "$BASE_IMAGE" --manifest "$MANIFEST" \
  --helpers "$HELPERS" --journey "$JOURNEY" --oracle "$ORACLE" \
  --oracle-marker OBSERVE_PASS --oracle-failure-marker OBSERVE_FAIL --out "$NEW_REPRO_DIR"
```

Hash the reproduction `result.json` and pass its path and SHA-256 to the next step. A healthy baseline, a crash without the exact symptom, incomplete evidence or a changed thread does not authorize repair. Keep the files for diagnosis.

```sh
python3 -m workbench.benny_reproduce verify-candidate --receipt "$REPRO_RESULT" \
  --sha256 "$REPRO_SHA" --revision "$CANDIDATE_COMMIT" --out "$NEW_CANDIDATE_DIR"
```

The candidate must descend from the approved baseline. The runner derives the candidate offline from the same baseline image, holds the drivers and settings fixed, and reruns both probes twice. All runs must pass in distinct containers. `verified` describes runtime evidence for the selected revision.

For a non-UI report, the coordinator can prepare a paused causal repair task once the claim window has elapsed. Supply one to four precise searches for an existing GitHub fix. An open or merged matching PR, incomplete search or new thread activity stops intake.

```sh
python3 -m workbench.benny_reproduce prepare-repair --token-file "$SLACK_KEY" \
  --receipt "$REPRO_RESULT" --sha256 "$REPRO_SHA" --fix-search "$FIX_SEARCH_PHRASE" \
  --task-id "$TASK_ID" --tasks-root "$TASKS_ROOT" --workspaces-root "$WORKSPACES_ROOT"
```

Paused intake fixes the baseline, enables the root-cause requirement and retains the approved GitHub searches.

## Run the prepared repair

```sh
python3 -m workbench.benny_repair --task "$TASK_JSON" --token-file "$SLACK_KEY"
```

The coordinator rechecks the source thread and matching GitHub PRs before launch and before each edit. The model has workspace file tools. The coordinator holds the Slack reader, runs the causal comparison and checks the unchanged candidate in offline Docker. The independent root-cause review must support the mechanism and rule out a competing explanation before implementation. A failed frozen probe enters the bounded repair loop, with its original output retained. An execution failure pauses the task with evidence.

The runner stops at a verified local candidate once both frozen probes pass twice, the focused tests pass, and independent review confirms the correction and related behavior. It retains the commit, the candidate and baseline receipts, and the independent review. A second call at this checkpoint verifies those files again and returns the same candidate. For older prepared tasks, supply the original `--fix-search` phrases. The draft PR route and browser recording review are separate remaining integration work.
