---
name: pause-safely
description: Honor an explicit operator pause by retaining the task at a verifiable boundary and requiring release before resumption.
disable-model-invocation: true
---

# Pause safely

Use this workflow after an explicit operator request to pause an identified iBoard task. Use the task record printed when the workbench created it.

```sh
python3 -m workbench.run_control hold --task "$TASK" --reason "$REASON"
python3 -m workbench.run_control show --task "$TASK"
```

The hold is stored separately from the task execution lock. The current admitted operation can complete. A successful implementation or repair writes its normal candidate commit and checkpoint before stopping. An interrupted edit retains the worktree for inspection. Before a new step starts, the workbench checks the hold again. Publication checks it at each separate GitHub write. A request GitHub has already accepted can still take effect. Check the retained receipt before acting on an ambiguous response.

Use `show` to distinguish a running step from a retained checkpoint. Report the phase, exact commit, worktree cleanliness, relevant evidence, and the first safe action after release. Link the task's `decisions.tsv` when it exists. Holds apply to the selected task. A multi-phase dispatcher also retains dependent phases that still require that task.

When the operator explicitly releases the hold, wait for the admitted worker to finish, then run the release. The existing resume command checks the original inputs, remaining limits, and recorded task state.

```sh
python3 -m workbench.run_control release --task "$TASK"
python3 -m workbench.resume --task "$TASK"
```

A released hold keeps the same request ID and original task clock. A dirty or interrupted worktree needs inspection. Separate approval and GitHub evidence gates still control delivery.
