---
name: verify-pr
description: Independently replay an existing pull request against its parent and find the verified frontier of a stack.
disable-model-invocation: true
---

# Verify an existing pull request

Select the PR, the local checkout containing its head and base commits, and the
original request and acceptance criteria. The criteria file contains a JSON
array of objects with `id` and `text`.

```sh
python3 -m workbench.verify_pr --repo OWNER/REPO --pr 12 --checkout /path/to/repo \
  --id verify-pr-12 --request request.txt --criteria criteria.json --kind feature --surface non-ui \
  --image local-test-image --tasks-root tasks --workspaces-root workspaces --seconds 600
```

Use the normal image-source verification settings for OpenClaw. For a feature,
the optional frozen `--plan` needs a `baseline` command that observes real
prior behavior, plus candidate `resolution` and `tests`. A bug uses
`reproduction` for its baseline. The task retains both baseline runs, the
candidate checks and the independent decision. A failed verification stops at
the exact candidate with evidence. Inspect it before starting a repair task.

To audit a stack, supply a JSON object mapping each available PR number to
its retained `task.json`. Repeat `--pr` with parents first or discover the
stack from one PR.

```sh
python3 -m workbench.stack_verify --repo OWNER/REPO --pr 12 --discover-stack --tasks tasks.json
```

Report the verified prefix, its ceiling and the first missing or stale
verification. Check current GitHub readiness separately. Use a fresh
verification after a base or head change. For autopilot-stack, hand the
verified PRs back to the operator. The verification command cannot publish
or merge a PR.
