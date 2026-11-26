---
name: runtime-forensics
description: Diagnose a slow, stuck, or memory-heavy Node workload using offline Docker evidence and tracked source.
disable-model-invocation: true
---

# Runtime forensics

Choose an explicitly selected, repeatable workload on a local, nonproduction image. Preserve its Git revision, CLI arguments, resource limits, and observable failure or behavior. This investigation ends with a diagnosis and evidence.

- For a slow or busy Node entry, run `python3 -m workbench.runtime_forensics cpu --help`. Use the tracked entry and its optional loader. The result summarizes leaf time between ordered sample timestamps and maps application frames to a file, line and symbol in the verified revision.
- For suspected retention, use the `heap` mode and give `--class-name` when there is a specific object to follow. The snapshot records object counts and shallow sizes. The tool follows strong references from the synthetic root to a representative matching object. Allocation-site attribution and retained-size analysis require further evidence.
- For Docker failures, inspect `runtime_diagnostics` in the saved sandbox JSON. The container state and time-bounded events are bound to the exact container ID. A nested process can exhaust memory even when the wrapper reports a different exit code. Use a Docker OOM event or explicit OOM state as the evidence for that conclusion.

`--window-ms` captures a responsive diagnostic child during a bounded run and ends that disposable process so its profile can flush. Without a window, capture happens as the child exits. The outer sandbox timeout bounds an unresponsive run. The artifact can contain data from the workload. Use test inputs and share only the reduced finding unless the raw artifact is needed.

Use a comparison revision and local image when there is a specific hypothesis. Hold the entry, arguments and limits fixed. Compare the observed signals and cite the relevant source. If the cause depends on a browser trace or a different runtime, record the gap and pick an appropriate isolated capture.

Report the observed signal, source, comparison, raw artifact and cleanup status. Hand confirmed product findings to the bug or performance workflow. Keep an infrastructure failure attached to its run so automated repair can preserve the candidate without spending a repair iteration.
