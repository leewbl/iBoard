---
name: reproduce-and-fix-issues
description: Reproduce a reported behavior and independently verify the existing GitHub fix in offline Docker.
disable-model-invocation: true
---

# Verify an existing fix

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

Benny's intake via Slack, ownership decisions, recording and source-thread replies require separate integration. To implement a new authorized fix after confirming ownership, use the normal bug workflow and the existing independent delivery gate.
