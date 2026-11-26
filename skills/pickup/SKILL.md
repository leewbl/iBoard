---
name: pickup
description: Pick up a retained iBoard task or catch up on recent progress from its actual records.
disable-model-invocation: true
---

# Pick up a retained task

Use the exact task path the user selected. From the iBoard plugin root run:

```sh
python3 -m workbench.pickup --task /path/to/task.json
```

Add `--check-pr` to read current GitHub state for the recorded candidate. Add `--since 2026-11-22T00:00:00Z` to limit the event timeline. If the task lock is held, report that a cooperating process owns the task. A historical publication or merge record indicates what already happened. A live PR read can confirm its current head and state.

Summarize inherited evidence, outstanding blockers, and the next safe step. Use `resume-from-…` as a recovery hint. The normal resume and delivery guards still apply. Avoid treating old review metadata or passing status checks as proof of a newly changed candidate. Do not restart a task during a catch-up.
