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
  --tasks-root tasks --workspaces-root workspaces --seconds 3600 --watch
```

For image-based tasks, pass `--seed-image sha256:...` using a local image whose
tracked source matches the frozen root base. The workbench derives each new
baseline offline and verifies its source. In watch mode, the runner records
the current wait, rechecks landed members and polls the next PR for readiness.
It waits for pending checks, required approval or confirmation of a recorded
write. A failed
check or a mismatched PR stops the run. Use `--poll-seconds` to set the
interval (30 seconds by default, up to 1800). Omit `--watch` to return at
the first wait. Resuming keeps the saved deadline, plan, roots and image.

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
requires that exact base and the confirmed PR head for the lease. The
shipping journal records each confirmed update and its independent evidence.
Run `land` again with the same plan, or use `continue` to replay again if
main has advanced while checks were pending. Each replay starts from the
original patch and verifies it against the latest main. It updates the same
PR only if its head still matches the last confirmed update. A rewritten
main or an outside head change stops shipping. An unconfirmed write only
permits read-only reconciliation until its outcome is known.
Report each confirmed PR and the first pending or blocked member. Use
`workbench.stack_verify` for read-only handoff.
