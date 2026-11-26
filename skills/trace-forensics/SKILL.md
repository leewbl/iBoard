---
name: trace-forensics
description: Diagnose an existing CPU profile, V8 heap snapshot, Chrome trace, or macOS spindump or sample artifact without rerunning the workload.
disable-model-invocation: true
---

# Trace forensics

Use an existing capture. Confirm its origin, format, workload, and known SHA-256. Preserve the original bytes and keep raw data local. Profiles can include sensitive paths or application data.

Run `python3 -m workbench.trace_forensics --help` from the iBoard plugin root. Pass `--input`, a fresh `--out` directory, and `--sha256` when the capture has a recorded digest. The importer handles V8 CPU profiles, V8 heap snapshots, Chrome trace-event JSON, and the supported macOS spindump and sample text layouts. It detects gzip by its header and bounds both compressed and expanded input. Every capture receives an exact snapshot and a private SQLite database.

1. Query the database before interpreting large captures. CPU tables contain the frame tree, raw deltas, and leaf time between ordered sample timestamps. The final sample contributes zero measured interval. Heap tables preserve nodes and edges. The Chrome adapter stores events and their normalized durations or CPU profile samples. The macOS adapter preserves the observed process and thread stack.
2. Follow the affected call path or strong heap reference chain. Use `--class-name` to select a heap object class. Shallow size measures the objects themselves. A source location or allocation site can be reported only when the artifact carries supporting symbols.
3. For source verification, pass `--repo`, `--revision`, and the path root seen in the capture using `--source-root`. A verified Git location means that the recorded line exists in the selected revision. Browser bundle and unresolved locations retain their reported status.
4. Use `--compare` with a paired capture of the same format. Supply the recorded comparison digest and revision when available. The decoder recognizes duplicates even when their compression differs. Compare the measured signal only under comparable workloads, capture windows, and environments. A single capture supports a hypothesis. Paired observations can strengthen it when the controls support the inference. Review gaps in imported coverage before interpreting a numeric zero.
5. Cite the relevant SQLite row, artifact SHA-256 and source location or missing symbol. Include the reduced observation, the strength of the comparison and its limits. Hand the finding to a bug or performance task if the user asks for a change.

Use [runtime forensics](../runtime-forensics/SKILL.md) when the necessary capture does not yet exist.
