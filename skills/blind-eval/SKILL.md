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
| `criteria` | Three to six objects with `id`, `basis`, and `description`. Use one `runtime`, one `reading`, and criteria based on `artifact` source or `visual` screenshots. |
| `judge`, `auditor` | OpenAI review model IDs. Each review uses its own fresh session and sees the same neutral packet. |
| `check` | Exact Git `revision`, `file` containing a private Node driver, pinned local Docker `image` ID, and standalone success `marker`. The driver imports the specimen from `/workspace`. Set `"screenshot": true` when there is a `visual` criterion. |
| `seconds` | A single time limit for parallel work, execution and both reviews. |

```sh
python3 -m workbench.blind_eval freeze --manifest "$MANIFEST" --repo "$REPO" --out "$PLAN"
python3 -m workbench.blind_eval run --plan "$PLAN" --sha256 "$PLAN_SHA" --out "$OUT"
python3 -m workbench.blind_eval audit --plan "$PLAN" --sha256 "$PLAN_SHA" \
  --report "$OUT/result.json" --report-sha256 "$REPORT_SHA"
```

Each worker uses a new private directory, its assigned files and the exact shared request. The coordinator checks successful matching Read calls from the current Claude Code trace. Keep private driver code, scoring criteria, peer artifacts and provider identities out of all worker prompts. Docker runs the private check once against each original input and twice against each changed artifact, offline and in fresh read-only workspaces.

For a visual run, use a driver and an image with the browser or rendering tools already installed. Navigate through the same fixture, interactions, viewport, and final state. Use only local assets and keep option identities and credentials out of the rendered content. When `WORKBENCH_SCREENSHOT_PATH` is set, the driver must write a static PNG there before exiting with either success or a normal observed failure. The original run only needs its behavior result. The screenshot and private driver mounts are separate, and visual Docker runs are serialized to limit memory use.

The coordinator decodes each capture to RGBA, removes PNG metadata, and requires pixel equality between the two candidate runs before scoring. Use a fixed viewport of at most four million pixels. Reviewers each receive the same numbered PNGs and cite the corresponding attachment with a visible reason for each visual score. Both review receipts bind the normalized image digests, and audit rechecks the original captures. The capture fixture is responsible for keeping the rendered scene anonymous because the coordinator only checks textual input and PNG metadata.

Each reviewer scores all neutral options on one scale and cites exact source text for artifact scores. Runtime and reading scores must match the observed evidence. If reviews disagree, runtime or screenshots disagree, or no eligible option wins outright, the run records the outcome without promoting a variant. Review the actual files, the competing verdicts and the frozen question before interpreting the comparison. Keep attribution and traces local and pass only the selected evidence needed for the experiment.

The runner compares bounded text source edits, optional static screenshots, and explicit Read tool calls. Video, other media artifacts, and traces from other hosts require separate adapters. Recommendations are local records.
