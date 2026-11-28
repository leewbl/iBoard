---
name: benchmark-checklist
description: Validate benchmark results against pinned workloads, independent profiling and alternating offline measurements.
disable-model-invocation: true
---

# Validate a performance comparison

Write the claim before measuring. Read the helper and confirm what its timed region awaits, what it counts as one operation, and how it verifies the output. Record the build, flags, cache state, workload, concurrency, limits and any production tuning shared by both sides. Confirm the rate ceiling is meaningful for the operations being measured. Inspect host load before and after, and record competing workloads that may affect results.

Use a tracked baseline and descendant candidate. iBoard verifies the local baseline image against Git and overlays the candidate source on that same runtime. This workflow measures source changes using the baseline dependencies. If dependencies or compiled outputs change, build and verify the appropriate measurement environment before making a release claim.

Use the [OpenClaw contract](../verify-openclaw/skills-list-benchmark.json) as a starting point. The helper emits one JSON line with `kind: "iboard-benchmark"`, `unit: "ms"`, `elapsed_ms`, `completed`, `errors` and `digest`. Count completed work and check the expected output. Compute the expected digest independently and include it in the contract. Keep fixture setup, output comparison and teardown outside the timed region unless the user actually waits on them.

```sh
python3 -m workbench.benchmark --repo "$TARGET_REPO" --base "$BASELINE" --revision "$CANDIDATE" \
  --image "$BASELINE_IMAGE" --manifest "$BENCHMARK_CONTRACT" \
  --helpers "$BENCHMARK_HELPERS" --out "$EVIDENCE_DIR"
```

Use at least five alternating runs per side. iBoard records the median and range and only treats a non-overlapping difference above the threshold as directional. Compare the same commit against itself to check noise. When it shows an apparent directional difference, report the comparison as inconclusive.

Record measured limiter evidence from a separate `profile` helper. Its `iboard-benchmark-profile` record includes the same workload counts and digest plus a `limiter`, a `hotspot` with tracked `path` and `contains`, and numeric `observations` with `metric`, `value` and `unit`. These runs are excluded from latency samples. Read their evidence and check whether the measured limit binds the app, runtime or load generator. Document production tuning in the contract. For a component result, add an `end_to_end` workload and explain how the component relates to the path users wait on. A directional observation that lacks this evidence remains inconclusive.

Report the verdict, unit, counts, range, limiter, configuration and evidence location. Scope conclusions to the measured workload. The supplied OpenClaw workload includes source CLI startup and an isolated synthetic workspace.
