---
name: blind-eval
description: Compare two to four project variants with the same request, offline checks, and blinded scoring.
disable-model-invocation: true
---

# Blind evaluation

Use the [pinned pstack Eval playbook](https://github.com/cursor/plugins/blob/41e400932eca14f289e7db717a70455256b6d388/pstack/skills/poteto-mode/playbooks/eval.md) to frame the comparison. Define the change under study and a normal user request. If the question is about context, use the same model and vary only the committed context. If it is about models, use the same project revision and change just the model. Keep the approval, attribution map and rubric outside the worker inputs.

Prepare a schema-1 JSON manifest with these fields.

| Field | Requirement |
| --- | --- |
| `question`, `request` | An internal research question and a shared natural-language request. Only the request reaches the workers. Omit evaluation terminology and model or variant identities. |
| `files`, `editable` | Neutral project paths. All named files must exist as regular text at each revision. Workers may change the contents only of files named in `editable`. |
| `arms` | Two to four objects with `name`, exact Git `revision`, explicit GLM `model`, and `observe` containing the paths that must be read successfully for that arm. |
| `criteria` | Three to six objects with `id`, `basis`, and `description`. Use one `runtime`, one `reading`, and one or more `artifact` criteria. |
| `judge`, `auditor` | OpenAI review model IDs. Each review uses its own fresh session and sees the same neutral packet. |
| `check` | Exact Git `revision`, `file` containing a private Node driver, pinned local Docker `image` ID, and standalone success `marker`. The driver imports the specimen from `/workspace`. |
| `seconds` | A single time limit for parallel work, execution and both reviews. |

```sh
python3 -m workbench.blind_eval freeze --manifest "$MANIFEST" --repo "$REPO" --out "$PLAN"
python3 -m workbench.blind_eval run --plan "$PLAN" --sha256 "$PLAN_SHA" --out "$OUT"
python3 -m workbench.blind_eval audit --plan "$PLAN" --sha256 "$PLAN_SHA" \
  --report "$OUT/result.json" --report-sha256 "$REPORT_SHA"
```

Each worker uses a new private directory, its assigned files and the exact shared request. The coordinator checks successful matching Read calls from the current Claude Code trace. Keep private driver code, scoring criteria, peer artifacts and provider identities out of all worker prompts. Docker runs the private check once against each original input and twice against each changed artifact, offline and in fresh read-only workspaces.

Each reviewer scores all neutral options on one scale and cites exact artifact text. Runtime and reading scores must match the observed evidence. If the reviewers disagree, the runtime runs differ, or no eligible option wins outright, record that outcome without promoting a variant. Review the actual files, the competing verdicts and the frozen question before interpreting the comparison. Keep attribution and traces local and pass only the selected files needed for the experiment.

The runner compares bounded text source edits and observes explicit Read tool calls. Broader media artifacts and traces from other hosts need separate adapters. Recommendations are local records.
