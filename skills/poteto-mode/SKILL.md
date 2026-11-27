---
name: poteto-mode
description: Select a pinned pstack playbook, record its goal and chosen stages, and execute the approved iBoard adapters.
disable-model-invocation: true
---

# Poteto mode

Start with the user's goal and the smallest change that meets it. For a reversible choice that evidence can settle, use a small prototype. Use Architect for design across boundaries, Swarm for independent coverage, Arena for competing changes, and Interrogate for contested acceptance. Apply the relevant leaf principles and state what each changed in the decision. Prefer real entry-point evidence over a proxy.

Use `python3 -m workbench.poteto_mode list` to see the pinned playbooks, available stage actions, and evidence. Use `select --request /absolute/request.txt --out /absolute/selection.json` on the same module. Supply one of `--playbook NAME`, `--model MODEL`, or `--profile /absolute/profile.json --profile-sha256 PROFILE_SHA`. Open the selected local guide to identify its prerequisites. The initial selection cites the original request and keeps authority with the operator and the established task record.

Write a stage manifest such as the following. Choose stages from the selected playbook and supply the native CLI flags. Add `inputs` for immutable request files, manifests, or receipts. Add `outputs` for evidence files that the stage must create for its result to count.

```json
{
  "playbook": "pause-safely",
  "seconds": 60,
  "steps": [
    {"action": "hold", "args": ["--task", "/absolute/task.json", "--reason", "Retain the next checkpoint"]},
    {"action": "show", "args": ["--task", "/absolute/task.json"]}
  ]
}
```

Run `freeze --selection /absolute/selection.json --sha256 SELECTION_SHA --manifest /absolute/dispatch.json --out /absolute/route-run`. Inspect the stages, then run `run --plan /absolute/route-run/plan.json --sha256 PLAN_SHA` with the approved digest. Add `--once` to run just the next stage. For an earlier declared artifact, use `{"step":1,"path":"/absolute/output.json","value":"sha256"}` in place of an argument. Use `"value":"path"` to pass the frozen artifact path.

For a native JSON result that has an explicit success status, declare an `expect` entry such as `{"path":"/absolute/output.json","field":["status"],"equals":"passed"}`. A stage with that check needs both a successful exit and the expected status in the exact output file.

Use `show --plan /absolute/route-run/plan.json --sha256 PLAN_SHA` for the journal. Each stage records the native exit code, logs, and declared artifacts. A completed stage reports only what its native action checked. If a process is interrupted, inspect the saved native checkpoint and resume through the corresponding workflow. Existing task, sandbox, delivery, and cleanup approvals govern all native actions.

For a delegated task, open [the worker brief](worker.md) and pass its absolute path with the task goal. Select role models from the approved iBoard profile. Keep worker inputs small by linking exact files or receipts. Use a fresh worker for an independent check and review the actual diff and outcomes before reporting.
