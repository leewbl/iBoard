---
name: prototype
description: Compare throwaway behavioral or visual variants and preserve observations for a later design decision.
disable-model-invocation: true
---

# Compare a design decision with a throwaway instrument

Name the decision the prototype should resolve and the observed behavior that separates the alternatives. When the design space is open, gather references and agree which directions to try. Keep this experiment outside the production source tree. Use the result in Architect, Feature, or a multi-phase plan for real implementation.

The manifest fixes the local `repo`, Git `revision`, a `question`, `criteria`, an installed `image`, and two to four `variants`, each with an `id`, `label`, and `approach`. Optional `sources` name committed files and line ranges for grounding. Set `surface` and `entry` together:

- For `behavior`, use a standalone Node `.mjs` entry. It receives the variant ID and emits one JSON observation for the work performed. Use `kind: "iboard-prototype"`, the actual `variant`, `surface: "behavior"`, and a bounded text or JSON object in `observation`.
- For `visual`, use a standalone HTML page with one labeled switcher. Give each control its exact `data-variant` and the visible result its exact `data-active-variant`. Use an installed image with Playwright and Chromium, such as the prepared iBoard browser image. The driver clicks each requested variant in offline Chromium and captures its resulting screen.

```sh
python3 -m workbench.prototype --manifest "$PROTOTYPE_MANIFEST" \
  --model "$OPENAI_MODEL" --out "$PROTOTYPE_EVIDENCE"
```

The author creates one bounded source file when you omit `--prepared`. For an existing instrument, pass a separate scratch directory via `--prepared` and list any additional local `assets`. The runner provides a read-only copy to a fresh offline container per variant. Each result carries its observed output, raw execution and screenshot digest when applicable. The reviewer compares the actual observations and attaches screenshots in variant order. A recommendation reflects the stated question and evidence. Preserve inconclusive results when the experiment does not settle the choice.

Retain the question, tradeoffs, recommendation and artifact location. Keep `scratch/` as disposable evidence. For a [multi-phase plan](../multi-phase-plan/SKILL.md), reference `result.json` as a native prototype with its SHA-256 and exact conclusion. Review the scope of a prototype recommendation before deciding on production behavior.
