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

Reproduction and resolution use the same tracked driver and arguments on different commits. Each probe varies one input.

With fixed controls, the planner names a mechanism and up to four competing explanations, with committed source anchors. Each control varies the suspected cause while still exercising its competing explanation.

For an adaptive investigation, supply `root_cause.hypotheses` with two to five anchored hypotheses and `root_cause.investigation` with up to six possible probes. Each probe records its `command`, mutually exclusive `outcomes` (marker and exit code), a `predictions` map from hypothesis ID to outcome ID, and the intended healthy `candidate` result. Every hypothesis must be distinguishable. The coordinator chooses the probe with the smallest worst-case survivor count, using the supplied probe order to resolve ties. After agreeing on one observed outcome, it narrows the hypothesis set and chooses again. The archived trace records the probe, outcome and hypotheses before and after each comparison.

The implementer and independent verifier run the trigger and selected controls on the baseline in separate read-only, offline Docker containers. Independent review checks the committed sources and quotes the trigger and each control verbatim. Each exclusion needs a reason. Malformed JSON or an inexact quotation allows one correction against the same evidence. A missing or ambiguous observation, disagreement, or unresolved mechanism stops the task before editing.

Both roles repeat the trigger and selected controls on the exact candidate. The driver must have the same bytes and each check must show the expected healthy behavior. The final review ties the change to the established mechanism. Publication and merge replay the original causal search against the archived observations and audit the baseline verdict, candidate controls, and hashes. A replay on a new main starts the investigation from that baseline again. Previously saved fixed and single-control plans remain readable.

Report the original symptom, surviving mechanism, why each competing explanation was excluded, the fix, and the baseline and candidate observations. Link the original reports and review. The adaptive search uses the committed driver and frozen probe matrix. New instrumentation and probing across different surfaces require a separate investigation.
