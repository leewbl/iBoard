---
name: refactor
description: Change code structure while preserving public behavior, checked commit by commit against a frozen contract in offline Docker.
disable-model-invocation: true
---

# Behavior-preserving refactoring

Use this workflow for a focused structural change. Record the structure that should improve, the affected paths and the public behavior that must remain intact. Route a behavior change to the feature or bug workflow. Use the architect skill before a change that crosses module or function boundaries.

Trace affected callers before editing. Write a standalone helper that exercises an actual entry point with disposable inputs. Cover successful and failing inputs where they matter. Capture return values, output or observable side effects in the helper's output. Keep fixture timestamps and paths fixed where they are incidental. The helper should exercise the target installed at `/app`. It is copied separately to read-only `/workspace`, so the candidate cannot replace the helper with one that reports success.

Select an entry point that runs the changed source under `/app`. The source-image build overlays committed files and retains frozen dependencies. If the app runs generated assets, verify those assets with a separate build bound to each commit before using them as evidence.

Create a JSON manifest with the target structure, allowed source paths and case commands. The helper extension selects Node, Python or sh. The tool adds the declared arguments after the script name.

```json
{
  "schema": 1,
  "intent": "Consolidate the branches that format diagnostics",
  "target_shape": "Group optional diagnostic fields in one branch",
  "scope": ["src/config/issue-format.ts"],
  "cases": [
    {
      "id": "invalid-config",
      "helper": "config-output.mjs",
      "args": ["invalid"],
      "exit_code": 1,
      "behavior": "Return the same diagnostic and allowed values"
    }
  ]
}
```

Freeze before editing. Use an installed baseline image with tracked Git source and dependencies under `/app`. Keep the reported SHA-256 with the approved task.

```sh
python3 -m workbench.refactor freeze --repo "$TARGET_REPO" \
  --base "$BASE_COMMIT" --image "$BASELINE_IMAGE" \
  --manifest "$MANIFEST" --helpers "$DRIVERS" --out "$BASELINE_EVIDENCE"
```

Make small commits in an isolated worktree. Remove redundant branches and migrate affected callers together. Check each committed step against the original contract. The verifier walks the ordered commits, rejects edits outside the approved paths, builds each candidate offline and runs the unchanged helpers twice in fresh containers. Each case compares the exit code and exact stdout and stderr bytes. An unstable case or incomplete capture is inconclusive.

```sh
python3 -m workbench.refactor verify --repo "$TARGET_REPO" \
  --contract "$BASELINE_EVIDENCE/contract.json" --contract-sha256 "$CONTRACT_SHA256" \
  --candidate "$CANDIDATE_COMMIT" --out "$CANDIDATE_EVIDENCE"
```

For an automated iBoard task, give `workbench.start` the frozen record and its SHA-256. Use the same image and memory limit that were recorded in the contract.

```sh
python3 -m workbench.start --id "$TASK_ID" --kind refactor --surface non-ui \
  --repo "$TARGET_REPO" --issue-file "$REQUEST" --source 'approved refactor' \
  --iterations 2 --seconds 1800 --image "$BASELINE_IMAGE" --memory-mb "$MEMORY_MB" \
  --source-mode image --build-source-image \
  --refactor-contract "$BASELINE_EVIDENCE/contract.json" --refactor-contract-sha256 "$CONTRACT_SHA256"
```

The task uses the frozen baseline and adds a permanent behavior criterion. The independent reviewer must cite the comparison report. Report the simplified structure, any unexplored behavior, the exact revision and evidence paths. The task pauses after verification when no delivery repository was provided. For an authorized fork, the normal publish and merge path revalidates the contract, exact candidate, ordered receipts and the same independent review before each remote write. With `--publish-only`, it stops after PR publication.

The comparison checks the behaviors covered by the frozen cases. If an ordered commit changes one, the task retains its report and stops. A follow-on repair cannot make that earlier commit pass, so review the retained sequence before trying again.
