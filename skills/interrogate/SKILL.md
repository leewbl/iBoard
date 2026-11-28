---
name: interrogate
description: Challenge an exact candidate with independently repeated offline functional probes and preserve reproducible findings.
disable-model-invocation: true
---

# Challenge an exact candidate

Choose observable product behavior that could fail at a boundary of the design. Name the intent and use trusted, tracked probe scripts whose bytes are identical at the baseline and the candidate. Put commands, expected markers, and expected exits in the approved configuration.

```json
{
  "image": "local-node-image",
  "review_model": "<configured OpenAI model>",
  "source_mode": "workspace",
  "seconds": 300,
  "probes": [
    {
      "id": "empty-input",
      "command": ["node", "/workspace/probe.mjs", "empty-input"],
      "marker": "EMPTY_INPUT_OK",
      "exit_code": 0,
      "intent": "The public API preserves an empty input."
    }
  ]
}
```

Use this configuration as the `interrogate` object in a [Feature comparison](../feature-handoff/SKILL.md), or run the check independently. Store the evidence outside the target checkout. Supply the exact baseline, candidate and request file. The pinned candidate runs every probe twice in disconnected Docker, each run from a separate clean worktree. The reviewers examine the actual observations and source. The record retains repeated mismatches, inconclusive runs and cleanup evidence.

To use the same [role profile](../setup-pstack/SKILL.md) as the main task, replace `review_model` with `"model_profile":{"path":"/path/to/profile.json","sha256":"APPROVED_DIGEST"}`. Configure two to five `interrogate-reviewers` with both GLM and OpenAI represented. Each seat records its approved model and CLI effort. Profiled runs confirm distinct container IDs and the original output hashes. They count a finding only when the two runs show the same mismatch.

```sh
python3 -m workbench.interrogate --repo "$TARGET_REPO" --baseline "$BASELINE" \
  --candidate "$CANDIDATE" --issue-file "$REQUEST" --config "$PROBE_CONFIG" \
  --out "$CHALLENGE_EVIDENCE"
```

## Review uncommitted work

Create a local snapshot from an exact baseline that is an ancestor of the working branch. Repeat `--file` to name specific paths, or omit it to capture the worktree diff and unignored new files. The visible file bytes take precedence over staged versions. The temporary index starts at HEAD, so changes outside an explicit selection stay in the original checkout. A local `refs/iboard/interrogate/` ref retains the candidate for auditing.

```sh
python3 -m workbench.interrogate_snapshot --repo "$TARGET_REPO" --baseline "$BASELINE" \
  --file path/to/changed-source --out "$SNAPSHOT"
python3 -m workbench.interrogate --repo "$TARGET_REPO" --baseline "$BASELINE" \
  --snapshot "$SNAPSHOT" --snapshot-sha256 "$SNAPSHOT_SHA" \
  --issue-file "$REQUEST" --config "$PROBE_CONFIG" --out "$CHALLENGE_EVIDENCE"
```

Use the SHA-256 printed by the snapshot command. Discovery accepts the same snapshot options in place of `--candidate`. With a snapshot, discovery can derive the scope from its selected paths or its full baseline-to-candidate diff. The plan and result retain the snapshot digest. Each independent worktree must reproduce the captured bytes and modes before it runs a probe. Use the committed-candidate entry point when there are no draft changes. Snapshot paths support bounded regular files and deletions. Resolve index conflicts or select a full checkout before capturing work.

## Discover functional checks for a diff

Create a catalog using the same profiled configuration with `"schema":1`. Include vetted, tracked probes for ordinary behaviors such as defaults, repeated operations, compatibility and boundary inputs. Reviewers receive the selected diff and the catalog case names and behavior descriptions. Each proposal must select an existing case and cite a changed line by ID. The coordinator preserves every required probe and combines the reviewers' selections in catalog order.

```sh
python3 -m workbench.interrogate_discovery propose --repo "$TARGET_REPO" --baseline "$BASELINE" \
  --candidate "$CANDIDATE" --issue-file "$REQUEST" --catalog "$CATALOG" \
  --file path/to/changed-source --required stable-case --out "$DISCOVERY"
python3 -m workbench.interrogate_discovery execute --plan "$DISCOVERY/plan.json" \
  --sha256 "$APPROVED_PLAN_SHA" --out "$CHALLENGE_EVIDENCE"
```

Review the selected cases before approving execution. The plan binds the exact request, diff, tracked probe bytes, model profile and proposal receipts. It runs only approved catalog commands. It retains empty or invalid proposals without treating them as verified coverage. Findings come from the repeated runtime observations and remain separate from what reviewers nominated.
