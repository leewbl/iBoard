---
name: poteto-help
description: Find an iBoard entry point for a task, explain what evidence it produces, and identify which pstack capabilities are still reference material.
disable-model-invocation: true
---

# iBoard help

Use `/agent-workbench:poteto-help` for setup, workflow selection or a question about an existing task. The help request authorizes lookup only. Return the relevant entry point, one example prompt when helpful, and the guide that explains its inputs and evidence. A separate request can authorize the work itself.

From the iBoard plugin checkout, use `python3 -m workbench.pstack_help list` to see the known situations. Use `python3 -m workbench.pstack_help scenario pr-status` for a matching situation or `python3 -m workbench.pstack_help show skills/poteto-mode/playbooks/babysit.md` for an exact pstack entry. Inspect the returned local guide before recommending a command. The returned commands are reference text.

Pick the scenario that matches the user's actual goal.

| Goal | Scenario |
| --- | --- |
| Load the local plugin | `setup` |
| Understand code, its design history, or both | `how`, `why` or `teach` |
| Reproduce and fix a bug, optionally test-first | `fix-bug` or `test-first` |
| Change code structure while keeping existing behavior | `refactor` |
| Build an agreed feature or choose between designs | `build-feature`, `plan-design`, `prototype` or `compare-patches` |
| Check why a PR is blocked, rerun its evidence, or plan a verified stack's delivery | `pr-status`, `verify-pr` or `ship-stack` |
| Find out where a saved task stopped or what changed since a timestamp | `inspect-task` or `catch-up` |
| Continue a previously authorized task | `resume-task` |
| Add or maintain a project’s executable verification | `generate-verify` or `maintain-verify` |
| Capture, read, or compare performance evidence | `capture-cpu`, `analyze-trace` or `check-performance` |

When the user asks about a named pstack skill, principle, or playbook, use `show` to check the exact entry. `native-entry` means the listed local files exist. The guide gives the current prerequisites and boundaries. `guidance-only` points to prompts used within other workflows. `catalog-only` means this lookup has no verified local runnable mapping. For pstack-specific tooling or a catalog-only workflow, cite the pinned source and explain that status. Do not infer availability from a title, a similar command, or successful CLI loading.

For blocked runs, ask for the saved task record or the first concrete failure. Use the pickup guide for persisted state, the PR status guide for a selected PR, and the performance guides for a suspected runtime issue. Describe what evidence would settle the question. For example, a failing acceptance check needs its saved run report and the target revision. A task request benefits from a clear goal, an observable outcome, available inputs and the permitted scope.

The upstream reference is [pstack poteto-help 0.15.48](https://github.com/cursor/plugins/blob/41e400932eca14f289e7db717a70455256b6d388/pstack/skills/poteto-help/SKILL.md). The lookup uses iBoard’s local catalog and CLI files and does not inspect credentials, invoke models, or contact a remote service.
