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

To use a [role profile](../setup-pstack/SKILL.md), replace the explicit
`runners` and `judge_model` fields with
`"model_profile":{"path":"/path/to/profile.json","sha256":"APPROVED_DIGEST"}`.
Freeze the Arena seats and the ordered confirmed cross-judge pool outside
the parent vendor. The parent model scores alongside the first available
judge. A failed Codex invocation with a structured `model_not_found` can
advance to the next approved model with the same prompt. The rejected
model is skipped in later review phases. Audit checks the order, failure
code, prompt digest and the model recorded on each resulting review.
Quota, authentication, ambiguous failures and a negative verdict keep
the original blocking behavior. The selected
feature/refactoring route synthesizes when synthesis is required. Each
invocation records the role route, requested effort and any CLI-observed
model ID. Failed provider calls block new calls to that provider. A
malformed implementation note can be reformatted once in a read-only
session while the original candidate stays subject to all checks.

An approved Feature Arena can use the same profile in its `arena` settings.
The receipt freezes the profile with the design and the winning commit.
When the Feature task supplies a profile and requests a new comparison,
the two approved profile digests must match.

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
verification. The command currently supports text code patches. Export
requires a successful cross-scorecard and an independent semantic review.
