---
name: create-formal-verification-skill
description: Verify a property of concurrency, retries, parsers, or state transitions and generate a reproducible project-local skill.
disable-model-invocation: true
---

# Create a formal verification skill

Read the target implementation, callers, existing tests, and project guidance. Choose one promised behavior whose violation causes concrete harm and whose inputs or interleavings are easy to miss in example-based tests. Inspect source before proposing a property. Explain the property and harm in everyday language.

Choose the tool according to what has to be proved. Use a model checker for allowed interleavings, a verifier or proof assistant for a property over every input, or a bounded exhaustive check against the real entry point when a suitable code verifier is unavailable. If you model the implementation, map each atomic transition and invariant to tracked code. State the scope, assumptions and excluded cases. Retain the model or proof files as named `support` files in the manifest. Provision the verifier and dependencies in the local image before the run.

Use the [OpenClaw command lane example](../../examples/formal/openclaw-command-lane/manifest.json) for the input shape. Name the covered files and unique source anchors, the expected tool version, the minimum number of cases or obligations, and an exact, isolated mutation to the covered implementation. The negative control must violate the property without causing a syntax or setup failure. A separate replay helper can exercise the counterexample through the real implementation. Leave the product checkout untouched.

```sh
python3 -m workbench.formal_verify create --repo "$TARGET_REPO" --revision "$REVISION" \
  --manifest "$MANIFEST" --helpers "$HELPER_DIRECTORY" --image "$LOCAL_IMAGE" --out "$EVIDENCE_DIR"
```

Use standalone Node, Python or shell drivers against source under `/app`. Drivers emit one line beginning with `IBOARD_PROPERTY ` and a JSON object with `status`, `tool_version`, `explored`, and `counterexample`. Use exit 0 and an empty counterexample for `holds`. Use exit 1 and a list of concrete steps for `violated`. Report observed tool and version. Let setup errors fail without a property-result line. When configured, the independent replay receives the counterexample JSON path as its only argument. It reports whether the same concrete sequence violates the property in real code.

For a `proof` contract, use `explored` for discharged or checked obligations and keep each solver result in the raw log. A solver timeout, `unknown`, or a mismatched extraction fails verification. The [OpenClaw Z3 example](../../examples/formal/openclaw-command-lane/README.md#用-z3-证明容量约束) checks admission and settlement by induction and replays the reported counterexample in the actual queue.

The workbench validates source anchors and an immutable image. It runs the verifier on the selected revision, repeats it against a real source mutation in a temporary detached worktree and, if configured, drives that counterexample independently. If the independent driver uses a different tool, record its observed version as `replay_tool_version` in the manifest. It removes the scratch worktree and checks the unchanged revision again in a fresh container. Each execution retains a hash-bound receipt with exit, tool result and cleanup evidence. Use [statechart](../statechart/SKILL.md) to inspect a TLA+ model and its shortest rule violations.

Open the generated `SKILL.md` and follow its check command once. Hand off the generated bundle after `verified`, along with a plain-language description of the scope, the result, the counterexample and the remaining gaps. Use [maintenance](../maintain-formal-verification-skill/SKILL.md) when product code moves after the recorded sync commit.
