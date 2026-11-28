---
name: typescript-best-practices
description: Review TypeScript types, runtime boundaries, compiler coverage, and real behavior checks.
disable-model-invocation: true
---

# TypeScript best practices

Use the simplest total type. Model alternatives with a literal discriminant and handle every variant. Give primitive values distinct brands when their meanings can be confused. Validate the brand where it is created. Prefer constructive types when the representation itself can enforce the invariant.

Treat input from JSON, RPC, files, CLI, environment variables, or storage as `unknown`. Parse it once at the boundary with the project's existing schema or a predicate that verifies the claim. Derive types from that source or the existing signature. Prefer narrowing and `satisfies` to unverified assertions. Use named object arguments when positional arguments are ambiguous. Respect measured hot paths and use structured diagnostics.

Review the intended caller and run the project's real behavior tests. Use the compiler check when an offline baseline source image is available with an installed TypeScript Compiler API module and the project dependencies.

1. From the iBoard root, run `python3 -m workbench.typescript_discipline check --repo /absolute/repo --base BASE_SHA --candidate HEAD_SHA --image LOCAL_BASELINE_IMAGE --compiler /app/node_modules/typescript --project tsconfig.json --out /absolute/ts-evidence`. Give full commit IDs and a committed project config. Use repeated `--file` to select a sub-scope, or leave it unset to cover all changed TypeScript files within the command's limit.
2. Inspect `summary.json` and the raw baseline, candidate, and independent receipts. The runner overlays the candidate on the verified baseline, checks the configured project without emitting files, and requires each selected candidate file to be in the compiled program. Existing project settings and prebuilt dependencies may affect project-reference resolution. Use `--behavior-script /absolute/check.cjs --behavior-marker EXPECTED_RESULT` to run an approved caller test twice in separate offline candidate containers. The Node driver uses the verified source under `/app`.
3. Run `python3 -m workbench.typescript_discipline review --report /absolute/ts-evidence/summary.json --sha256 REPORT_SHA --model MODEL --source src/selected.ts:20-50 --out /absolute/ts-review.json`. Include relevant source ranges from every selected candidate file. A fresh reviewer sees the selected excerpts. The review checks whether guards, casts, construct validity, schema ownership, and behavior evidence support the change.

A clean compiler run proves type checking for the observed project and source. Candidate findings such as an unconditional predicate or unsafe assertion still need review. A runtime claim requires a separate test at the actual caller boundary. Use the regular iBoard delivery flow for publication.
