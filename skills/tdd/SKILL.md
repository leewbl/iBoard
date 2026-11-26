---
name: tdd
description: Reproduce a selected bug with a focused failing test before implementing the fix, then verify the identical test on the candidate.
disable-model-invocation: true
---

# Focused regression

Use for an explicitly selected bug when the user asks for a regression test or the affected entry point has a cheap deterministic check. Start the bug through [optimize](../optimize/SKILL.md) and include the intended behavior and relevant entry point in the task request. In source-verified image mode for OpenClaw, prefer the native Vitest adapter when a registered project can discover the new `src/**/*.regression.test.ts` file. Select `unit`, `unit-fast`, `agents-core` or `gateway`. Plan one top-level test with the exact test name and a distinctive expectation message. Keep the original Vitest configuration and dependencies.

For a standalone check, supply a new `*.regression.test.mjs` path, ESM source, the expected assertion message, and a success marker. Resolve project files from the sandbox working directory. Choose an assertion against the real behavior and print success only after it passes.

The coordinator runs the same test bytes in two read-only, offline Docker containers on the clean parent revision. The native adapter mounts only the new file at its eventual source path, so existing test discovery and imports apply. Both runs must fail at the expected assertion. The independent baseline review checks that the assertion demonstrates the reported issue. Only then does the coordinator add the test to the worktree and ask the implementer to fix the code. For native green runs, Vitest executes the committed test in two fresh candidate containers with no test overlay. Keep the ordinary reproduction, resolution, and neighboring tests.

The final reviewer cites both independent runs for the criterion they support. Before a PR can be published or merged, the delivery gate checks those citations, all four report hashes, runtime identities, and the tracked test blob. The PR summarizes the failed-before and passed-after runs.

OpenClaw discovers native tests through its configured project selectors. The standalone script uses the dedicated iBoard gate. Register a standalone script with the project's own tooling if it should run in future routine checks.

If a cheap focused test is impractical, use the ordinary reproduction and resolution workflow with the closest meaningful executable scenario. State which evidence was obtained and why a focused red and green test could not be demonstrated.
