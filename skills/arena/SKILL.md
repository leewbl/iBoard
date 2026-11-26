---
name: arena
description: Compare code alternatives in isolated worktrees, score them blindly and verify a synthesized patch.
disable-model-invocation: true
---

# Compare alternative implementations

Open a checklist for framing, candidate execution, cross-judging, selection,
synthesis and verification. Freeze one request, a small editable scope, a
tracked offline acceptance command and three to six concrete rubric items.
The acceptance script is outside the editable scope.

```json
{
  "repo": "/path/to/repository",
  "request": "Implement the requested behavior within the selected scope.",
  "files": ["src/feature.js"],
  "rubric": [
    {"id": "behavior", "text": "Implements the requested behavior."},
    {"id": "api", "text": "Preserves the existing API."},
    {"id": "maintenance", "text": "Keeps the implementation straightforward."}
  ],
  "runners": [{"provider": "glm"}, {"provider": "openai", "model": "OPENAI_MODEL"}],
  "judge_model": "OPENAI_MODEL",
  "image": "local-node-image",
  "concurrency": 2,
  "seconds": 600,
  "check": {"command": ["node", "/workspace/test.js"], "marker": "PASS", "exit_code": 0}
}
```

```sh
python3 -m workbench.arena --manifest arena.json --out /path/outside/checkout/arena-run
```

Use the same frozen task for every candidate. Candidate generation can run
concurrently. The acceptance containers are disconnected and run serially.
In image source mode the baseline image is verified against Git, and the
acceptance command uses a tracked entry under `/app`.

The anonymous comparison includes candidate patches and whether their frozen
checks passed. GLM and OpenAI score each rubric item, and the selection records
how it resolved any disagreement. Synthesis records which parts it retained
and rejected. The final candidate must pass a sandbox run and a second run
from a separate clean checkout. The independent reviewer receives the frozen
script, exact patch, changed paths and all three sandbox results. Its verdict
gates the exported patch. The report
links to candidate patches, raw checks, scorecards, synthesis notes and final
verification. The command currently supports text code patches. A missing
judge or a failed review blocks export.
