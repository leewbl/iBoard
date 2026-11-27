---
name: bug-cause
description: Test a suspected bug mechanism with a matched control and require fresh, independent evidence before delivery.
disable-model-invocation: true
---

# Verify a bug mechanism

Use this workflow for a selected bug whose trigger can be varied by one argument to the same committed driver. Name the user's original symptom and acceptance conditions before running. Keep the task's usual limits and repository authorization. Add the following flags to the normal `workbench.start` command.

```sh
--kind bug --require-root-cause
```

The planner proposes a primary mechanism, a competing explanation, source locations on the baseline, and a matched control. Both hypotheses must be plausible for the reported symptom. The control must remove the proposed trigger while still exercising the competing explanation. Reproduction and resolution use the same driver and arguments on different commits. Only the control varies one input.

The implementer and independent verifier each run the trigger and the control on the baseline in offline Docker. The independent review checks the committed sources and actual outputs. If it cannot support the proposed mechanism and rule out the competing explanation, the task stops before editing.

Both roles repeat the trigger and control on the exact candidate. The candidate must leave the driver unchanged, produce the expected behavior, and omit the original fault on both healthy runs. The final review ties the change to the established mechanism. The publication and merge gates recheck the archived baseline verdict and execution hashes. A replay on a new main runs the comparison from that baseline again.

Report the original symptom, the supported mechanism, the eliminated explanation, the fix, and the four observations from the independent run. Link the raw reports and review inside the task record. State what is inconclusive if the trigger requires instrumentation or a broader comparison than this workflow can express.
