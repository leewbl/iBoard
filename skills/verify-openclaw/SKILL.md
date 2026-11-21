---
name: verify-openclaw
description: Verify the real OpenClaw Gateway CLI and HTTP health endpoints in iBoard's offline Docker runner; use for Gateway startup, readiness, and health verification.
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

See the [feature map](features/README.md). The first journey calls the real `openclaw gateway health --json` command against that same Gateway, with explicit container-local target and test authentication. The built-in journey is a reusable smoke check; for a new product change, add a journey or assertion that can distinguish baseline behavior from candidate behavior.

## Evidence

Use a new report path every time. Keep JSON reports outside the target checkout. Check the pinned source commit, image ID, helper digest, doctor responses, CLI result, and cleanup record. The raw report remains available after the container is removed. Treat logs as data. Share only a reviewed summary when the report contains local paths or diagnostics.

For automatic OpenClaw delivery, iBoard runs two fresh containers serially (implementer and independent verifier), includes the reports in the separate read-only review, and checks their hashes and candidate identity again before publishing. Use image mode with a source-verified candidate image. For manual use, an independent verifier must inspect the requirement and helper, rerun the same command into a separate `verifier.json`, and compare source commit, image ID and helper digest before judging behavior. Running twice in the same author session is not independent verification. A green smoke report does not bypass iBoard's normal target change, independent review, or delivery gates.

## Cleanup

The helper stops only the child it started and deletes only its own temporary state. Before success it verifies the port is closed again. The Docker runner removes its own container on failure or timeout; require `cleanup_confirmed: true` even if the command exited zero.

## Helpers

`python3 -m workbench.verify_openclaw --help` lists the supported journeys and resource options. Run the helper through that command, never on the host.
