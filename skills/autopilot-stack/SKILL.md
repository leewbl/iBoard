---
name: autopilot-stack
description: Verify an explicitly selected stack and hand it over at the current verified revisions.
disable-model-invocation: true
---

# Verify and hand over a PR stack

Use the selected repository and PRs in parent-first order. Give each PR its
original acceptance criteria and request, or select its retained iBoard task.
Write a local JSON manifest:

```json
{
  "repo": "OWNER/REPO",
  "checkout": "/path/to/checkout",
  "image": "local-test-image",
  "seconds": 900,
  "members": [
    {
      "number": 12,
      "request": "root-request.txt",
      "criteria": [{"id": "behavior", "text": "The command emits the expected result."}],
      "kind": "feature",
      "surface": "non-ui",
      "required_ci_workflows": ["CI"]
    },
    {"number": 13, "task": "/path/to/retained/task.json"}
  ]
}
```

Relative paths resolve from the manifest. A new member can also specify
`seconds`, `image`, `source_mode`, `candidate_image`, `build_source_image`,
`memory_mb`, a frozen verification `plan`, or its explicit `local_gate`.
See the verify-pr skill for the baseline and candidate command requirements.

```sh
python3 -m workbench.stack_autopilot freeze --manifest stack-spec.json --out tasks/stack-run
python3 -m workbench.stack_autopilot run --plan tasks/stack-run/stack.json
```

The run serially verifies every selected PR and stays within the total
elapsed time from first execution. Rerun the same frozen plan to recheck
current gates or resume a paused task. If a PR moves, freeze current inputs
and require fresh verification for the changed revision.

Report each PR's head, base, independent evidence, and blocking gate.
Handoff is ready when the selected stack meets the verification and CI or
local-gate checks. This workflow ends at handoff. The shipping skill handles
separate authorization to update or merge a verified stack.
