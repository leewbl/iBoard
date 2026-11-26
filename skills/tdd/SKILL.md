---
name: tdd
description: Reproduce a selected bug with a focused failing test before implementing the fix, then verify the identical test on the candidate.
disable-model-invocation: true
---

# Focused regression

Use for an explicitly selected bug when the user asks for a regression test or the affected entry point has a cheap deterministic check. Start the bug through [optimize](../optimize/SKILL.md) and include the intended behavior and relevant entry point in the task request. The workbench planner can supply the `tdd` field with a new `*.regression.test.mjs` path, standalone ESM source, the expected assertion message, and a success marker. Resolve project files from the sandbox working directory. Choose an assertion against the real behavior and print success only after it passes.

The coordinator runs the same test bytes in two read-only, offline Docker containers on the clean parent revision. Both runs must fail at the expected assertion. The independent baseline review checks that the assertion demonstrates the reported issue. Only then does the coordinator add the test to the worktree and ask the implementer to fix the code. The candidate must retain the exact test source and pass it in two fresh containers. Keep the ordinary reproduction, resolution, and neighboring tests.

The final reviewer cites both independent runs for the criterion they support. Before a PR can be published or merged, the delivery gate checks those citations, all four report hashes, runtime identities, and the tracked test blob. The PR summarizes the failed-before and passed-after runs.

The dedicated iBoard gate runs the standalone script. If the project should run the script in future routine checks, register it with its own test tooling as part of the change. OpenClaw's standard Vitest projects select TypeScript test files.

If a cheap standalone Node test is impractical, use the ordinary reproduction and resolution workflow with the closest meaningful executable scenario. State which evidence was obtained and why a focused red and green test could not be demonstrated.
