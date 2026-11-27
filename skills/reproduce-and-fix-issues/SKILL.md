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

The worker policy can enable a [reply in the original report thread](../setup-benny/SKILL.md#optional-reproduction-reply-in-the-report-thread). The reply requires the confirmed baseline and a fresh read of the original thread. The worker preserves the exact reply receipt through the claim window, repair and draft checks. A reporter's in-window [selection of an approved setup](../setup-benny/SKILL.md#approve-a-corrected-setup-before-the-run) can trigger one corrected run. Other changes hold the workflow for inspection.

For an OpenClaw UI repair, declare `browser`, `screenshot` and `recording` on the primary journey. Supply `--baseline-visible` and `--candidate-visible` on `reproduce`, with two distinct conditions that name the precise location and distinguish the fault from the repaired state. Use the same browser driver for both revisions. On a declared fault, capture the PNG and recording before returning exit 1 with the exact failure marker. For each revision, two fresh media reviews confirm the relevant condition from sampled browser frames and the final PNG. An uncertain observation, missing recording or changed receipt blocks the repair or its delivery. The separate functional probe is still required.

```sh
python3 -m workbench.benny_reproduce verify-candidate --receipt "$REPRO_RESULT" \
  --sha256 "$REPRO_SHA" --revision "$CANDIDATE_COMMIT" --out "$NEW_CANDIDATE_DIR"
```

The candidate must descend from the approved baseline. The runner derives the candidate offline from the same baseline image, holds the drivers and settings fixed, and reruns both probes twice. All runs must pass in distinct containers. `verified` describes runtime evidence for the selected revision.

The coordinator can prepare a paused causal repair task once the claim window has elapsed. A UI repair requires the frozen recorded browser contract and two confirmed baseline media reviews. Supply one to four precise searches for an existing GitHub fix. An open or merged matching PR, incomplete search or new thread activity stops intake.

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

The runner stops at a verified local candidate once both frozen probes pass twice, the focused tests pass, and independent review confirms the correction and related behavior. For a UI repair it rebuilds the exact candidate's Control UI, checks both candidate recordings separately and requires the final independent review to cite the screenshot from the second frozen browser run. It retains the commit, the candidate and baseline receipts, and the independent review. A second call at this checkpoint verifies those files again and returns the same candidate. For older prepared tasks, supply the original `--fix-search` phrases.

## Submit the verified repair as a draft

```sh
python3 -m workbench.benny_draft --task "$TASK_JSON" --token-file "$SLACK_KEY"
```

Use this entry to run a prepared repair through draft delivery or to submit an already verified candidate. It uses only the report’s configured personal fork. Before pushing the report branch and before creating the draft, the coordinator rechecks the source, tracker, pinned base, actual worktree and independent evidence. For UI cases it also rechecks both revisions' media receipts, the original screenshots and recordings, and the final review's citation. OpenClaw requires both runs of each applicable offline Gateway check in the same independent review. If the task uses the local-only policy, the destination must still have Actions disabled.

The draft describes the reproduction, root cause, focused checks and related behavior. It includes hashes for the original and candidate observations and the independent review. A trusted issue in the target GitHub repo is linked with `Refs`. Slack coordinates, local paths, raw logs and private attachments stay in the task artifacts. The coordinator scans every outgoing commit and stops on probable credential material or a binary diff without review.

The operation keeps a receipt before branch and PR writes. An unavailable response triggers a remote read. A matching draft can be confirmed on a subsequent call without another write. If the outcome remains unknown or the draft was closed, the coordinator retains the branch and receipt for inspection. Benny stops at draft status. The [optional operations thread](../setup-benny/SKILL.md#optional-slack-operations-thread) receives the confirmed draft link and can answer direct questions from retained evidence. Live Slack-to-GitHub delivery still needs end-to-end acceptance.
