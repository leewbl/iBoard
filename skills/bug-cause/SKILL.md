---
name: bug-cause
description: Test a suspected bug mechanism against competing explanations and require fresh, independent evidence before delivery.
disable-model-invocation: true
---

# Verify a bug mechanism

Use this workflow for a selected bug whose trigger can be varied by one argument to the same committed driver. Name the user's original symptom and acceptance conditions before running. Keep the task's usual limits and repository authorization. Add the following flags to the normal `workbench.start` command.

```sh
--kind bug --require-root-cause
```

The planner proposes a mechanism and up to four plausible competing explanations, with source locations on the baseline. Each competing explanation gets a named control, ordered by diagnostic value. Each control removes the suspected cause while still exercising that explanation. Reproduction and resolution use the same driver and arguments on different commits. Each control varies one input.

The implementer and independent verifier each run the trigger and all controls on the baseline in separate read-only, offline Docker containers. The independent review checks the committed sources and quotes the trigger and each control verbatim. Each exclusion needs a reason. If a review returns malformed JSON, the workbench can request one corrected response for the same evidence. If the mechanism is unsupported or any competing explanation remains plausible, the task stops before editing.

Both roles repeat the trigger and all controls on the exact candidate. The candidate must leave the driver unchanged, produce the expected behavior, and omit the original fault from every healthy run. The final review ties the change to the established mechanism. The publication and merge gates recheck the archived baseline verdict, all control receipts, and execution hashes. A replay on a new main runs the comparison from that baseline again. Previously saved single-control plans and receipts remain readable.

Report the original symptom, the supported mechanism, each eliminated explanation, the fix, and the baseline and candidate observations from the independent runs. Link the original reports and review. If the evidence is inconclusive, report which explanation remains. Probe order is fixed in the plan. Adaptive probe selection and instrumentation beyond the existing driver require further investigation.
