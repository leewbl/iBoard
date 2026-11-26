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

Use this configuration as the `interrogate` object in a [Feature comparison](../feature-handoff/SKILL.md), or run the check independently. Store the evidence outside the target checkout. Supply the exact baseline, candidate and request file. The pinned candidate runs every probe twice in disconnected Docker, each run from a separate clean worktree. The two reviewing providers examine the actual observations and source. The record includes mismatches that repeated, runs that lacked conclusive evidence, both reviews and cleanup results. This adapter uses preapproved probes. Discovery and execution of newly authored probes is a separate capability.

```sh
python3 -m workbench.interrogate --repo "$TARGET_REPO" --baseline "$BASELINE" \
  --candidate "$CANDIDATE" --issue-file "$REQUEST" --config "$PROBE_CONFIG" \
  --out "$CHALLENGE_EVIDENCE"
```
