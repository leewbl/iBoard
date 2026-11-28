---
name: autonomous-run
description: Drive a selected task to a frozen verified, published, or merged outcome, with a resumable CI checkpoint.
disable-model-invocation: true
---

# Autonomous run

Agree on an observable exit condition and the exact task before execution. Use `verified` for local acceptance or a verification-only PR. Use `published` for a task authorized to publish only. Use `merged` for a task already authorized to deliver. A plan must use the existing destination and delivery authorization.

A new task can be staged with `workbench.start --prepare-only --criterion ID=TEXT`. Keep the normal repository, issue, limit and image options. Each acceptance criterion must be explicit before freezing.

```sh
python3 -m workbench.autonomous_run freeze --task "$TASK" \
  --predicate "$PREDICATE" --interval 30 --out "$EVIDENCE"
python3 -m workbench.autonomous_run run --plan "$EVIDENCE/plan.json" --sha256 "$PLAN_SHA"
```

Use the returned SHA-256 for every invocation. The supervisor retains an audit trail. `--once` evaluates one tick so an existing scheduler can invoke it. A long-running invocation uses the interval as a polling heartbeat. If the task already owns an active lock, the supervisor observes it without launching a second worker.

After publication, unfinished CI yields a checkpoint bound to the exact PR, head, base and task settings. Later polls check the same workflow identities. A completed success or a confirmed required CI failure wakes the task. The delivery or repair path checks the event again. The successful outcome still requires independent evidence on the exact commit and, when applicable, current GitHub confirmation. A completed verification-only check has its own PR identity.

A process restart can attach to the retained CI checkpoint or recover an owned local phase at its clean recorded commit. Interrupted edits, ambiguous remote outcomes, and permanent failures require inspection. A task hold stays in force until an operator releases it. The original task deadline and iteration limits apply to the entire run.

In the handoff, report the selected predicate, attempts, verified commit, what landed, and outstanding blockers. Link the run journal, task decision trail, and evidence. This controller drives one selected task. Multi-PR owner dispatch uses a separate workflow.
