---
name: statechart
description: Check a TLA+ model in offline Docker and export Mermaid, SVG, and the shortest available rule violation traces.
disable-model-invocation: true
---

# Statechart

Model the behavior the reader must judge. Name each action in `Next` separately so the report can distinguish actions. Put each agreed safety rule in the TLC configuration as an invariant. Use temporal properties where a required outcome must eventually happen. Make the model finite. For an intended terminal state, give `Next` a labeled stuttering action to distinguish normal completion from a deadlock.

Run the model in a prepared local tool image. See [runtime setup and the queue example](../../examples/formal/openclaw-command-lane/README.md). A default configuration ends in `.cfg` next to the spec. Supply `--config` for another configuration and repeat `--support` for additional TLA+ modules or local view files.

```sh
python3 -m workbench.statechart check "$SPEC" --config "$TLC_CONFIG" \
  --view "$VIEW" --image "$RUNTIME_IMAGE" --out "$EVIDENCE_DIR"
```

The [pinned pstack engine](https://github.com/cursor/plugins/tree/41e400932eca14f289e7db717a70455256b6d388/pstack/skills/statechart) runs inside the container. The input snapshot is read-only. The network is disconnected. No package or image is pulled during the check.

A view module can export `state(vars)` for plain labels, nested states, or parallel regions. Return a string, an array, or a region-to-label object, respectively. Use `step(action, before, after)` to name an arrow from the reader's perspective. Add `group(vars)`, `colors`, and `title` when needed. Give every named group a color. Omit an arrow only if the reachable states and remaining paths still faithfully describe the model.

Read `artifacts/report.md` before presenting a chart. Report the violated rule, the shortest available witness, and its scope. Show `chart.mmd` and the rendered `chart.svg` when available. The engine draws only available violation paths above its 500-state chart budget. When it cannot draw a chart or finish checking every rule, use the report and raw TLC output to show exactly what was checked. `render-error.txt` preserves an SVG error separately from the model verdict. `--mermaid-only` requests text artifacts.

Exit 0 is a completed check with no reported violation. Exit 1 retains a counterexample as evidence. Exit 2 indicates that the check could not produce a verdict. Use `result.json` and `execution.json` to audit the tool, input hashes, exported files, exit and container cleanup. For a model of real software, replay a witness against the actual entry point before claiming a product defect.

The engine source and MIT license are in [vendor](vendor/). See [provenance](PROVENANCE.md) for its version and the changes around it.
