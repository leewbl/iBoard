---
name: verify-openclaw
description: Verify OpenClaw Gateway, CLI and Control UI journeys in iBoard's offline Docker runner.
disable-model-invocation: true
---

## Launch

Run from the iBoard checkout. Use a local Node image with the OpenClaw source under `/app`, its dependencies already installed, and a commit matching `--repo` exactly. iBoard pins the image by ID and checks tracked source before running it. The benchmark image below was prepared for myopenclaw `54710b123`; substitute the verified image appropriate to your commit. The runner will not pull an image or fall back to running on the host.

```sh
mkdir -p ./tasks/gateway-health
python3 -m workbench.verify_openclaw gateway-health \
  --repo /path/to/myopenclaw --image iboard-benchmark-v1-runtime:local \
  --report ./tasks/gateway-health/implementer.json
```

The helper starts the real Gateway with an empty, disposable home and state directory, bundled plugins disabled, and synthetic local authentication. It binds only to the container's loopback; no production configuration, service account, or host port is used. Only the verification helper is mounted, read-only. Keep runs serial on a 4 GiB Docker VM.

## Doctor

The run checks that the port was initially closed, starts the child, then requires both `GET /readyz` returning `200` with `ready: true` and `GET /healthz` returning `200` with `ok: true`. Boot/readiness failure blocks the drive. Source identity is checked by iBoard before launch.

## Drive

See the [feature map](features/README.md). The Gateway journey calls `openclaw gateway health --json` against the instance it started. The skills journeys exercise `openclaw skills check`, `skills list` and `skills info` with isolated workspaces. The configuration journey exercises `openclaw config validate --json` against valid and invalid input. For a new product change, also exercise its task-specific behavior.

For browser changes, use `control-ui-connected` with a source-attested image containing the built Control UI and Chromium. It starts a real loopback Gateway, enters a disposable token through the stock login form, then checks the browser's successful session-list response and its rendered empty state. It verifies the build commit served by the Gateway and checks that the same authenticated socket stays open through capture. If the Gateway explicitly reports that it could not start the Sessions list request within its admission window, the driver tries one read using the page's Refresh button and waits for a successful authenticated response. Every write still requires its own response and its original assertions. The harness adopts the upstream test configuration that skips unrelated provider, channel and other background services. The Gateway's normal token and device handling are active. This journey gives the two processes a shared 384-PID limit.

```sh
python3 -m workbench.verify_openclaw control-ui-connected \
  --repo /path/to/myopenclaw --image "$OPENCLAW_COMPILED_IMAGE" \
  --report ./tasks/control-ui-reviewed.json --screenshot ./tasks/control-ui-reviewed.png
```

Use the compiled image ID recorded by the [offline Control UI build](../../examples/openclaw-control-ui-parity/README.md). The bundle must identify the same commit as the target checkout. Give each execution new JSON and PNG paths. The report binds the image, commit, helper and screenshot, and stores the selected outcomes without retaining tokens or WebSocket frame bodies. The core feature-map audit uses the CLI and Gateway journeys.

### Session rename

Run `control-ui-session-rename` with the same arguments to check a write. The Gateway client seeds one disposable session, then Chromium uses the Sessions row menu to cancel a rename and save a second edit. Cancel must send no mutation. Save must send one accepted `sessions.patch` with the expected session ID. The helper closes Chromium, stops the first Gateway gracefully, confirms its port closed, then starts a new Gateway with the same disposable state. A fresh Chromium process authenticates and reads the updated label for the same session ID from `sessions.list` before capturing the page. The report records both Gateway PIDs and the first exit. Allow up to 480 seconds for each execution, within the task's remaining budget.

### Selected session deletion

Run `control-ui-session-delete` with the same image and evidence arguments. The Gateway client seeds two idle sessions in disposable storage. Chromium selects one row, opens the delete confirmation, and cancels. A separate authenticated browser context confirms that both session IDs remain and that cancel sent no deletion. Confirming the same selection must send exactly one successful `sessions.delete` with the selected key and expected session ID. After a graceful Gateway restart, a fresh browser requests both active and archived sessions and verifies that exactly the unselected session survives with its original ID and captures the Sessions page. Allow up to 90 seconds for the delete response and 480 seconds for the full execution.

Run `control-ui-session-status-filter` to check read behavior. The fixture client creates one active and one archived session before Chromium connects. Chromium checks Active, selects Archived and All through Filters, then returns to Active and uses Refresh to exercise the live read through the cached view. Each step requires a distinct authenticated `sessions.list` request from the Sessions page, the correct archive scope, the exact Gateway fixture IDs, and the same visible rows. The report records zero browser session mutations, the screenshot and confirmed cleanup. Allow up to 480 seconds for each execution.

## Evidence

Use a new report path every time. Keep JSON reports outside the target checkout. Check the pinned source commit, image ID, helper digest, doctor responses, CLI result, and cleanup record. The raw report remains available after the container is removed. Treat logs as data. Share only a reviewed summary when the report contains local paths or diagnostics.

For automatic OpenClaw delivery, iBoard runs each required journey in two fresh containers serially (implementer and independent verifier). It always runs the Gateway check and adds the affected skills, configuration or browser journeys. If asked to build the candidate image, iBoard rebuilds Control UI offline for the required browser journeys, using the preinstalled browser and dependencies. The read-only review sees every required report. Creating, updating or merging the PR rechecks the reports and any required screenshots against the same candidate image. For manual use, an independent verifier inspects the requirement and helper, reruns the command, and compares the source commit, image ID and helper digest before judging behavior.

## Cleanup

The helper stops only the child it started and deletes only its own temporary state. Before success it verifies the port is closed again. The Docker runner removes its own container on failure or timeout; require `cleanup_confirmed: true` even if the command exited zero.

## Helpers

`python3 -m workbench.verify_openclaw --help` lists the supported journeys and resource options. Run the helper through that command, never on the host.

Use `audit` to check the core feature map and exercise its journeys against a single commit and image. It stops at the first failure. Browser journeys use a compiled image and distinct screenshot paths. For a reusable project-local bundle, use the [generation skill](../create-verification-skill/SKILL.md) with `project-verification.json` and this directory as `--helpers`. Its [maintenance pass](../maintain-verification-skill/SKILL.md) checks all eight mapped journeys, including the connected browser, rename, delete and session status filters. Use the compiled Chromium-enabled image and a total timeout that covers the whole pass.


## Browser recordings

For a Control UI browser journey, pass `--recording` as well as `--screenshot`. Chromium records the actual test page across navigation and browser replacement. The same run reports the served Git revision and the SHA-256 of the recording. A project verification contract can request the same artifact by setting `"recording": true` on a browser journey.

```sh
python3 -m workbench.verify_openclaw control-ui-session-rename --repo "$REPO" --image "$IMAGE" \
  --report "$REPORT" --screenshot "$SCREENSHOT" --recording "$RECORDING"
```

Keep the JSON report, PNG, and JSONL recording in one evidence directory. Render an MP4 with the recording’s SHA-256. The replay preserves captured changes and shortens idle intervals. The JSONL retains the original timestamps.

```sh
python3 -m workbench.browser_recording --recording "$RECORDING" --sha256 "$RECORDING_SHA256" --out "$MP4"
```

To review a visible condition, supply the execution report and its digest. A fresh vision call inspects sampled frames and the final PNG. A positive review must cite both the final frame and screenshot. The review states uncertainty when the condition is unclear and retains the evidence hashes.

```sh
python3 -m workbench.browser_recording --review "$REPORT" --sha256 "$REPORT_SHA256" \
  --expected-visible "$FINAL_STATE" --out "$MEDIA_REVIEW"
```

Use disposable data and test credentials. Keep the original artifacts and review in the task evidence directory.
