---
name: runtime-forensics
description: Diagnose a slow, stuck, memory-heavy or visually unstable workload with offline Node or Chromium evidence.
disable-model-invocation: true
---

# Runtime forensics

Choose an explicitly selected, repeatable workload on a local, nonproduction image. Preserve its Git revision, CLI arguments, resource limits, and observable failure or behavior. This investigation ends with a diagnosis and evidence.

- For a slow or busy Node entry, run `python3 -m workbench.runtime_forensics cpu --help`. Use the tracked entry and its optional loader. The result summarizes leaf time between ordered sample timestamps and maps application frames to a file, line and symbol in the verified revision.
- For suspected retention, use the `heap` mode and give `--class-name` when there is a specific object to follow. The snapshot records object counts and shallow sizes. The tool follows strong references from the synthetic root to a representative matching object. Add `--track-allocations` to collect V8 stacks from process startup. The report groups live objects by their recorded stack and links the same representative to its stack when available. Tracked snapshots use a 64 MiB sandbox file limit and require additional CPU and memory. Keep the flag and workload fixed across comparative trials.
- For Docker failures, inspect `runtime_diagnostics` in the saved sandbox JSON. The container state and time-bounded events are bound to the exact container ID. A nested process can exhaust memory even when the wrapper reports a different exit code. Use a Docker OOM event or explicit OOM state as the evidence for that conclusion.

`--window-ms` captures a responsive diagnostic child during a bounded run and ends that disposable process so its profile can flush. Without a window, capture happens as the child exits. The outer sandbox timeout bounds an unresponsive run. The artifact can contain data from the workload. Use test inputs and share only the reduced finding unless the raw artifact is needed.

For allocation stacks, source locations refer to V8's recorded function positions. Use those frames to inspect the corresponding Git file. A frame can cover several allocation statements, and shallow bytes measure the live objects themselves. Estimating memory released by removing a reference requires separate retained-size analysis.

Use a comparison revision and local image when there is a specific Node hypothesis. Hold the entry, arguments and limits fixed. Compare the observed signals and cite the relevant source.

For a browser symptom, use the source-verified image with Playwright and
Chromium already installed. Select the tracked Node server and its loopback
page. Give the workload's real selectors when it needs an action or an observation.

```sh
python3 -m workbench.browser_forensics --repo /path/to/project \
  --revision HEAD --image LOCAL_IMAGE --entry server.mjs --port 18789 \
  --route / --click '#start' --observe '#animated-panel' \
  --window-ms 1200 --pause-animations --out /tmp/iboard-browser-evidence
```

The tool records Chrome Trace Events through the DevTools Protocol and imports
them into SQLite. The trace marks the action and the observation window. A
frame maps to a tracked line when the selected renderer observed the script
bytes, they match the Git blob, and the line is within it. A URL also seen on
another renderer thread stays unverified. Transformed bundles still need
source-map support.

With `--pause-animations`, both trials use the same image, source, action and
observation window. A fixed CDP intervention pauses the browser animation
clock in the second trial. Report a change only for the same unique named
animation observed across both trials. This tests the animation's clock and
does not by itself identify a separate rendering or application fault.
Keep raw traces and SQLite evidence with the run. They can contain application
test data, including request URLs.

Report the observed signal, source, comparison, raw artifact and cleanup status. Hand confirmed product findings to the bug or performance workflow. Keep an infrastructure failure attached to its run so automated repair can preserve the candidate without spending a repair iteration.
