# OpenClaw legacy skill metadata

OpenClaw should report a skill that still uses the legacy `metadata.clawdbot` block. The selected PR adds a JSON field and terminal migration guidance. See the [original issue](https://github.com/openclaw/openclaw/issues/202294) and the [existing PR](https://github.com/leewbl/myopenclaw/pull/5).

The primary probe runs `skills check --json` with modern and legacy fixtures. The separate probe uses the terminal output and checks the affected name and rename hint. Neither executes a skill or connects to a live OpenClaw instance. Both verify that the relevant fixture remains unchanged.

Run from iBoard with a clean evidence destination. Set `TARGET_REPO` to a local OpenClaw checkout containing the PR revisions. Set `BASE_IMAGE` to a local source image containing the PR base and its dependencies. Set `ORIGINAL_REPORT` to a UTF-8 copy of the issue and its source link.

```sh
sample=skills/reproduce-and-fix-issues/examples/openclaw-202294
staging=$(mktemp -d)
cp "$sample/observe-json.mjs" "$staging/"
cp skills/verify-openclaw/doctor.mjs "$staging/"
python3 -m workbench.verify_existing_fix --repo "$TARGET_REPO" \
  --artifact https://github.com/leewbl/myopenclaw/pull/5 --report "$ORIGINAL_REPORT" \
  --image "$BASE_IMAGE" --manifest "$sample/contract.json" --helpers "$staging" \
  --journey legacy-skills-metadata --oracle "$sample/observe-terminal.mjs" \
  --oracle-marker OPENCLAW_LEGACY_TERMINAL_PASS --oracle-failure-marker OPENCLAW_LEGACY_TERMINAL_FAIL \
  --expected-base 54710b123fe94a2139930c1d7c038e7623345a5e \
  --expected-head 1b7eba6645a3e8a07d03290204d2d09e7f64e8b8 --out "$staging/evidence"
```

Read `result.json`, then read the attached stdout observations in `baseline` and `candidate`. This is a CLI path, so the evidence is the successful command response and its classified observations.
