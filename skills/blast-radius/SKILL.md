---
name: blast-radius
description: Check what a proposed change could break downstream and prove its decisive impact facts in isolated runs.
disable-model-invocation: true
---

# Blast radius

Read the selected diff and follow downstream callers, response shapes, stored data, and the relevant pinned dependency or configuration. Select the source lines that explain the risk, including callers outside the diff.

```sh
python3 -m workbench.blast_radius analyze --repo "$REPO" \
  --baseline "$BASE" --candidate "$HEAD" --intent-file "$REQUEST" \
  --source src/client.ts:30-62 --model "$REVIEW_MODEL" --out "$ANALYSIS"
```

Decide which facts must hold before shipping. Bind each checked fact to one approved [Interrogate probe](../interrogate/SKILL.md). For example, a checks file with `{"checks":{"fact-1":"client-reply"},"required":["fact-1"],"interrogate":{...}}` maps the first analysis fact to the existing probe `client-reply`. Use a probe that calls the real downstream code and fails when the invariant fails. A missing probe leaves the fact unproven.

```sh
python3 -m workbench.blast_radius run --repo "$REPO" --analysis "$ANALYSIS" \
  --sha256 "$ANALYSIS_SHA" --probes "$CHECKS" --out "$EVIDENCE"
```

Report what changed, the decisive facts, and the source and runtime evidence for each. Keep reproducible regressions separate from unclear findings. Include the ordinary check that would settle a remaining risk. Runtime approval requires two distinct containers and both independent reviews. For a broader change, use the Interrogate model profile to give the same observations to an independent panel.

Use `blast_radius attach --task "$TASK" --report "$REPORT" --sha256 "$REPORT_SHA"` to hand a passing report to an approved iBoard task. The task request and exact candidate must match. Keep task logs local and put only public observations and evidence digests in the PR.
