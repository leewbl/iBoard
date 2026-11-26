# OpenClaw Control UI visual parity

The stock Control UI shows **Gateway unreachable** when served on an isolated loopback address without a Gateway. The state manifest first checks that message, then captures the error details collapsed and after clicking **Raw error**. The 960 × 430 viewport and pointer actions scroll the details into view. The animated header is outside the captured region. Both captures are checked for exact decoded pixels.

The example runs OpenClaw's production Vite configuration. It serves compiled assets through the repository's Vite preview command. The bootstrap document supplies the same minimal public fields as the upstream preview test. The build skips preparation of packaged plugin settings because this connection screen never loads them. No Gateway, credentials or model calls are required for these two states.

## Prepare the original build

Select a clean OpenClaw checkout and a local image that contains all tracked files of that exact revision under `/app`. The image also needs the installed OpenClaw build dependencies, Playwright and a compatible Chromium binary. The observed run used the `54710b123fe94a2139930c1d7c038e7623345a5e` commit and the immutable image behind `iboard-benchmark-v1-browser:local`.

Run from the iBoard root. Set `OPENCLAW_REPO`, `OPENCLAW_BROWSER_IMAGE` and `ARTIFACTS`, an output directory outside the OpenClaw checkout. Use a local image tag for the Dockerfile. The actual capture independently verifies the image contents against Git and stores the immutable image ID.

```bash
BASE=$(git -C "$OPENCLAW_REPO" rev-parse HEAD)
mkdir -p "$ARTIFACTS/image-base"
cp examples/openclaw-control-ui-parity/build-login-ui.mjs "$ARTIFACTS/image-base/"
cat > "$ARTIFACTS/image-base/Dockerfile" <<EOF
FROM $OPENCLAW_BROWSER_IMAGE
WORKDIR /app
COPY build-login-ui.mjs /tmp/iboard-build-login-ui.mjs
RUN node --import /app/scripts/tsx.mjs /tmp/iboard-build-login-ui.mjs && rm /tmp/iboard-build-login-ui.mjs
EOF
docker build --network=none --pull=false -t iboard-control-ui:baseline "$ARTIFACTS/image-base"
python3 -m workbench.visual_parity baseline \
  --repo "$OPENCLAW_REPO" --revision "$BASE" --image iboard-control-ui:baseline \
  --manifest examples/openclaw-control-ui-parity/states.json \
  --out "$ARTIFACTS/baseline" --timeout 420 --memory-mb 1536
```

Freeze and retain the baseline record before editing. The capture containers use a read-only root filesystem. Assets are built in the offline image build because the project's ordinary dev launch prepares plugin assets under the app directory.

## Rebuild and compare the candidate

Create the candidate in a separate Git worktree. The example merges the duplicate `color: var(--muted)` declarations for the summary and raw text in `ui/src/styles/connection-surfaces.css`. As a sensitivity check, another candidate assigns `#b83a22` to the raw text. The extra color is observable only with the details expanded.

Set `CANDIDATE_REPO` to the committed candidate worktree. The commands below are scoped to that single stylesheet. For any other migration, overlay every affected tracked path and rebuild the application before comparing. A source-only overlay would preserve stale generated assets.

```bash
CANDIDATE=$(git -C "$CANDIDATE_REPO" rev-parse HEAD)
BASELINE_HASH=$(shasum -a 256 "$ARTIFACTS/baseline/baseline.json" | cut -d ' ' -f 1)
STYLE=ui/src/styles/connection-surfaces.css
mkdir -p "$ARTIFACTS/image-candidate"
cp examples/openclaw-control-ui-parity/build-login-ui.mjs "$ARTIFACTS/image-candidate/"
git -C "$CANDIDATE_REPO" show "$CANDIDATE:$STYLE" > "$ARTIFACTS/image-candidate/connection-surfaces.css"
cat > "$ARTIFACTS/image-candidate/Dockerfile" <<EOF
FROM iboard-control-ui:baseline
WORKDIR /app
COPY connection-surfaces.css /app/$STYLE
COPY build-login-ui.mjs /tmp/iboard-build-login-ui.mjs
RUN node --import /app/scripts/tsx.mjs /tmp/iboard-build-login-ui.mjs && rm /tmp/iboard-build-login-ui.mjs
EOF
docker build --network=none --pull=false -t iboard-control-ui:candidate "$ARTIFACTS/image-candidate"
python3 -m workbench.visual_parity compare \
  --repo "$CANDIDATE_REPO" --revision "$CANDIDATE" --image iboard-control-ui:candidate \
  --baseline "$ARTIFACTS/baseline/baseline.json" --baseline-sha256 "$BASELINE_HASH" \
  --out "$ARTIFACTS/candidate" --timeout 420 --memory-mb 1536
```

The CLI exits 0 when both states match and 1 when pixels differ. Review the difference masks and independent reports under the output directory.

| Observed state | Equivalent CSS | Deliberate color change |
| --- | --- | --- |
| Collapsed | 0 pixels in both captures | 0 pixels in both captures |
| Expanded | 0 pixels in both captures | 2,519 pixels in each capture |

The first 960 × 820 attempt was rejected because six pixels differed in the animated logo. The retained 960 × 430 baseline was frozen before either candidate commit. Both candidates were rebuilt from their own tracked stylesheet with the same build helper, then captured in separate offline containers. This scenario covers the disconnected login screen and the compiled stylesheet. Connected Gateway journeys and PR delivery need their own evidence.
