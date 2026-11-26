---
name: shipping
description: Freeze the verified prefix of a PR stack and land it root-first on an authorized personal fork.
disable-model-invocation: true
---

# Ship a verified PR stack

Use the requested personal fork and the explicitly selected PRs. Put their
retained `task.json` paths in a JSON object keyed by PR number. To identify a
missing verification, use the `verify-pr` skill first.

```sh
python3 -m workbench.stack_ship plan --repo OWNER/FORK \
  --pr 12 --pr 13 --tasks retained-tasks.json --out shipping-plan.json
```

Report the verified prefix, ceiling, and first gap. If these tasks require
remote CI and carry no workflow policy, include the actual required workflow
names with `--ci-workflow` when freezing the plan. The existing local gate
applies when the task uses that policy.

Execute the frozen plan only under the operator's shipping authorization:

```sh
python3 -m workbench.stack_ship land --plan shipping-plan.json
```

The runner verifies the current PR, the evidence and the existing merge guards
before each write. The journal records requests before contacting GitHub and
confirms landing by reading the PR and main. If a request has an uncertain
outcome, inspect the same plan and remote state on the next invocation.

A child can target main automatically when main is exactly its verified parent
commit. When main has a different commit, replay and verify the blocked child
with the original request, acceptance history and local policy:

```sh
python3 -m workbench.stack_rebase verify --plan shipping-plan.json --id child-on-main \
  --tasks-root tasks --workspaces-root workspaces --seconds 900
```

Image-based verification takes `--image` for the new main commit. If the
rebase conflicts, preserve the worktree and report its path. With explicit
authorization to update the selected PR, apply the verified receipt:

```sh
python3 -m workbench.stack_rebase update --receipt tasks/child-on-main-restack/restack.json
```

The update checks main and the old PR head, and the push requires that exact
old head. Freeze a new shipping plan using the fresh task after confirmation.
Report each confirmed PR and the next blocked PR, if any. For an autopilot-stack
handoff that stops at verification, use `workbench.stack_verify`.
