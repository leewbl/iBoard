---
name: worktree-cleanup
description: Inventory Git worktrees and reclaim only a specifically approved subset that is clean, merged, and unused.
disable-model-invocation: true
---

# Worktree cleanup

Use the [pinned pstack cleanup playbook](https://github.com/cursor/plugins/blob/41e400932eca14f289e7db717a70455256b6d388/pstack/skills/poteto-mode/playbooks/worktree-cleanup.md) as the workflow reference. Start with a read-only inventory. Set `REPO` to an absolute path and choose the local tracking ref representing the intended PR base. Include every known iBoard task root with repeated `--tasks-root` options. When checking open PR protection, set `DESTINATION` to the exact GitHub owner and repository.

```sh
python3 -m workbench.worktree_cleanup inventory --repo "$REPO" \
  --base-ref refs/remotes/origin/main --tasks-root "$TASKS" \
  --destination "$DESTINATION" --holds "$HOLDS" --measure-size
```

Supply `HOLDS` as an absolute file path. Gather active, pinned, and otherwise retained checkouts from the operator and available host records. Use schema `{"schema":1,"entries":[{"path":"/absolute/worktree","reason":"Active or pinned task"}]}`. An empty list represents a completed check finding no additional holds. Without a usage snapshot the inventory is advisory. The tool reads the selected local ref as-is. Refresh it before planning if the comparison should reflect a newer remote base.

Report each candidate, its size estimate, and why other paths are retained. Protect task records even after completion when they still point to a worktree. Treat ignored artifacts as potentially valuable. Git-tracked base ancestry is conservative for squash merges. Host sessions that cannot be checked must be reported as unknown, and their potentially occupied checkouts should be retained.

After explicit approval of exact paths, use `freeze --repo "$REPO" --base-ref REF --tasks-root "$TASKS" --destination "$DESTINATION" --holds "$HOLDS" --select ABSOLUTE_PATH --out ABSOLUTE_PLAN`. Repeat `--select` for each approved path. Keep the plan and receipt outside linked worktrees.

With the approved plan digest and a refreshed usage snapshot, use `apply --plan ABSOLUTE_PLAN --sha256 APPROVED_DIGEST --holds "$HOLDS" --out ABSOLUTE_RECEIPT`. The runner checks the plan, unchanged base, registered identity, task records, open PRs, usage and cleanliness before removal. It uses ordinary `git worktree remove` and retains branches. Review blocked entries before taking any further action.

The receipt distinguishes the checkout's `du` estimate from the observed change in free bytes on the repository volume. Other activity or storage accounting can change that delta. The workflow does not inspect Cursor session metadata directly or reclaim IDE, Xcode, or system caches.
