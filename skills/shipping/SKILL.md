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

Under the operator's authorization to land and update the selected PRs, run
the same frozen plan through every required replay:

```sh
python3 -m workbench.stack_rebase continue --plan shipping-plan.json \
  --tasks-root tasks --workspaces-root workspaces --seconds 1800
```

For image-based tasks, pass `--seed-image sha256:...` using a local image whose
tracked source matches the frozen root base. The workbench derives each new
baseline offline and verifies its source. Keep the same arguments when
resuming after pending checks or a lost response. The original deadline
continues to run. Renewed approval is needed when starting a new plan.

To stop at the first member that requires a replay, use the original land
command:

```sh
python3 -m workbench.stack_ship land --plan shipping-plan.json
```

The runner verifies the current PR, the evidence and the existing merge guards
before each write. The journal records requests before contacting GitHub and
confirms landing by reading the PR and main. If a request has an uncertain
outcome, inspect the same plan and remote state on the next invocation.

A child can target main using the original proof when main is exactly its
verified parent commit. If main differs, replay the child's original patch
range and verify it against current main with the original request, acceptance
history and local policy:

```sh
python3 -m workbench.stack_rebase verify --plan shipping-plan.json --id child-on-main \
  --tasks-root tasks --workspaces-root workspaces --seconds 900
```

Image-based verification takes `--image` for the new main commit or the
same pinned `--seed-image` used by continuous shipping. If the rebase
conflicts, report the retained worktree. Tasks with specialized evidence
that needs a different replay workflow stop before publication. With
explicit authorization to update the selected PR, apply the verified receipt:

```sh
python3 -m workbench.stack_rebase update --receipt tasks/child-on-main-restack/restack.json
```

The update accepts GitHub's automatic retarget to the inspected main. It
requires that exact base and the old head for the lease. Once GitHub confirms
the update, the original shipping journal records the fresh evidence. Run
`land` again with the same plan. If main advances after publication, stop at
that PR and report the stale base. Report each confirmed PR and the first
pending or blocked member. Use `workbench.stack_verify` for read-only handoff.
