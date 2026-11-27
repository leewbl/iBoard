# Statechart engine provenance

The 28 runtime TypeScript files and their package metadata are reproduced from Lauren Tan's pstack 0.15.48, [cursor/plugins at 41e400932eca14f289e7db717a70455256b6d388](https://github.com/cursor/plugins/tree/41e400932eca14f289e7db717a70455256b6d388/pstack/skills/statechart/scripts). The [upstream MIT license](vendor/LICENSE) applies to those files. `vendor-files.json` records the exact bytes retained. The upstream test corpus stays in its source repository.

The upstream engine runs unchanged. The iBoard wrapper supplies the prepared tool paths, runs the engine in a read-only offline sandbox, renders SVG with Pele, and exports its raw evidence. Node executes the retained TypeScript directly. The upstream development-only package dependency is unused at runtime.
