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
  --destination "$DESTINATION" --holds "$HOLDS" --measure-size --inspect-recent-chats
```

Supply `HOLDS` as an absolute file path. Gather active, pinned, and otherwise retained checkouts from the operator and available host records. Use schema `{"schema":1,"entries":[{"path":"/absolute/worktree","reason":"Active or pinned task"}]}`. An empty list represents a completed check finding no additional holds. Without a usage snapshot the inventory is advisory. The tool reads the selected local ref as-is. Refresh it before planning if the comparison should reflect a newer remote base.

The inventory samples the current user's local processes using `lsof` on macOS or procfs on Linux. It checks working directories, open file descriptors and executables, and resolves active Claude Code workspaces using local session manifests with a matching observed process. It records only the matching PID and access type for each registered worktree. A known failed or partial probe holds candidates. Check pinned chats separately using the host view and keep those paths in `HOLDS`.

`--inspect-recent-chats` checks the last four days of local Codex, Claude Code and Cursor transcripts for the selected repository's registered workspaces. It includes references to a sibling worktree found in a session started at the primary checkout. The inventory returns the session ID, transcript modification time, how the path matched and an evidence digest. A path reference is a lead for operator review. Use the available host view to check what is in use, including chats started from other projects. Once the referenced worktree is confirmed unused, add `{"path":"/absolute/worktree","sha256":"RECENT_CHAT_SHA256"}` to the optional `reviewed_recent` array in `HOLDS`. This records the exact reviewed evidence. Active and pinned entries still protect the path. Incomplete transcript coverage also keeps candidates out of cleanup.

Report each candidate, its size estimate, and why other paths are retained. Protect task records even after completion when they still point to a worktree. Treat ignored artifacts as potentially valuable. Git-tracked base ancestry is conservative for squash merges. Host sessions that cannot be checked must be reported as unknown, and their potentially occupied checkouts should be retained.

After explicit approval of exact paths, use `freeze --repo "$REPO" --base-ref REF --tasks-root "$TASKS" --destination "$DESTINATION" --holds "$HOLDS" --inspect-recent-chats --select ABSOLUTE_PATH --out ABSOLUTE_PLAN`. Repeat `--select` for each approved path. Keep the plan and receipt outside linked worktrees. When the plan uses transcript evidence, `apply` repeats the scan and holds a worktree if the evidence has changed since approval.

With the approved plan digest and a refreshed usage snapshot, use `apply --plan ABSOLUTE_PLAN --sha256 APPROVED_DIGEST --holds "$HOLDS" --out ABSOLUTE_RECEIPT`. The runner checks the plan, unchanged base, registered identity, task records, open PRs, usage and cleanliness before removal. It samples live usage again immediately before using ordinary `git worktree remove`. The receipt records the observation and any matches for the selected checkout. A previously blocked approval can resume after the process exits if the other checks still pass. The branch is retained. Review unresolved holds before further action.

The receipt distinguishes the checkout's `du` estimate from the observed change in free bytes on the repository volume. Other activity or storage accounting can change that delta. Live usage is a point-in-time observation. Continue using the holds list for pinned or dormant chats, remote hosts and sessions the local process probe cannot see. IDE and Xcode resource reclamation remains outside the current cleanup entry.
