# OpenClaw Control UI visual parity

The stock Control UI shows **Gateway unreachable** when served on an isolated loopback address without a Gateway. The state manifest first checks that message, then captures the error details collapsed and after clicking **Raw error**. The 960 × 430 viewport and pointer actions scroll the details into view. The animated header is outside the captured region. Both captures are checked for exact decoded pixels.

The example runs OpenClaw's production Vite configuration. It serves compiled assets through the repository's Vite preview command. The bootstrap document supplies the same minimal public fields as the upstream preview test. The build skips preparation of packaged plugin settings because this connection screen never loads them. No Gateway, credentials or model calls are required for these two states.

## Prepare the original build

Select a clean OpenClaw checkout and a local image that contains all tracked files of that exact revision under `/app`. The image also needs the installed OpenClaw build dependencies, Playwright and a compatible Chromium binary. The observed run used the `54710b123fe94a2139930c1d7c038e7623345a5e` commit and the immutable image behind `iboard-benchmark-v1-browser:local`.

Run from the iBoard root. Set `OPENCLAW_REPO`, `OPENCLAW_BROWSER_IMAGE` and `ARTIFACTS`, an output directory outside the OpenClaw checkout. The state manifest approves the built-in `openclaw-control-ui-login` profile and fixes its recipe digest. The baseline command verifies the image against Git, compiles the original UI offline, then freezes the two Chromium states.

```bash
BASE=$(git -C "$OPENCLAW_REPO" rev-parse HEAD)
python3 -m workbench.visual_parity baseline \
  --repo "$OPENCLAW_REPO" --revision "$BASE" --image "$OPENCLAW_BROWSER_IMAGE" \
  --manifest examples/openclaw-control-ui-parity/states.json \
  --out "$ARTIFACTS/baseline" --timeout 420 --memory-mb 1536
```

The baseline record gives the immutable image ID and binds it to the selected Git commit, source image, recipe digest and screenshots. Freeze and retain that record before editing.

## Rebuild and compare the candidate

Create the candidate in a separate Git worktree. The example merges duplicate `color: var(--muted)` declarations for the summary and raw text in `ui/src/styles/connection-surfaces.css`. As a sensitivity check, another candidate assigns `#b83a22` to the raw text. The extra color is visible only with the details expanded.

Set `CANDIDATE_REPO` to the committed candidate worktree. With the rebuild profile enabled, pass the frozen baseline image. The compare command overlays tracked changes, clears the inherited UI bundle inside the image, and rebuilds offline with the exact candidate commit as build metadata. It checks the new output exists and validates the image against Git before taking screenshots. The profile uses the frozen installed dependencies and rejects changes to dependency manifests, the lockfile and dependency patches.

```bash
CANDIDATE=$(git -C "$CANDIDATE_REPO" rev-parse HEAD)
BASELINE_HASH=$(shasum -a 256 "$ARTIFACTS/baseline/baseline.json" | cut -d ' ' -f 1)
BASELINE_IMAGE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image"])' "$ARTIFACTS/baseline/baseline.json")
python3 -m workbench.visual_parity compare \
  --repo "$CANDIDATE_REPO" --revision "$CANDIDATE" --image "$BASELINE_IMAGE" \
  --baseline "$ARTIFACTS/baseline/baseline.json" --baseline-sha256 "$BASELINE_HASH" \
  --out "$ARTIFACTS/candidate" --timeout 420 --memory-mb 1536
```

For an automated UI task, pass the frozen record with `workbench.start --visual-baseline ... --visual-baseline-sha256 ... --source-mode image --build-source-image`. Configure the same manifest in a phase's `visual_parity` entry to rebuild during staged dispatch. Unit and live acceptance and the performance measurement use the same rebuilt candidate image.

The CLI exits 0 when both states match and 1 when pixels differ. Review the difference masks and independent reports under the output directory.

| Observed state | Equivalent CSS | Deliberate color change |
| --- | --- | --- |
| Collapsed | 0 pixels in both captures | 0 pixels in both captures |
| Expanded | 0 pixels in both captures | 2,519 pixels in each capture |

The first 960 × 820 attempt was rejected because six pixels differed in the animated logo. The retained 960 × 430 baseline was frozen before either candidate commit. Both candidates were rebuilt from their own tracked stylesheet with the same build helper, then captured in separate offline containers.

## Authenticated Sessions

Use [connected-states.json](connected-states.json) with the same baseline and compare commands for the real Sessions page. Set `--timeout 1800` to budget for separate Gateway launches and use `--memory-mb 3712` with a Docker VM sized for that limit. Each capture launches an isolated Gateway, waits for readiness, authenticates the browser, and checks the expected rows. Populated states seed two disposable fixtures and verify the Active, Archived and All transitions. The snapshot uses the selected transition's authenticated reply. Every screenshot covers the complete 1280 × 800 viewport.

The Gateway fixture sets `gateway.controlUi.communityInvite` to `false` before capture and checks the served presentation setting. This controls the timed invitation outside the Sessions states under test. The manifest freezes the Gateway driver digest along with the visual build recipe. Retain the original baseline and both per-state receipts as the reference for the migration.
