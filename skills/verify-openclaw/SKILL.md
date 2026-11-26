---
name: verify-openclaw
description: Verify real OpenClaw journeys in iBoard's offline Docker runner, including Gateway readiness and skill discovery and eligibility.
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

See the [feature map](features/README.md). The Gateway journey calls the real `openclaw gateway health --json` against the instance it started. The skills journey runs `openclaw skills check --json` with an isolated workspace. The configuration journey runs `openclaw config validate --json` against valid and invalid input. For a new product change, also exercise its task-specific behavior.

## Evidence

Use a new report path every time. Keep JSON reports outside the target checkout. Check the pinned source commit, image ID, helper digest, doctor responses, CLI result, and cleanup record. The raw report remains available after the container is removed. Treat logs as data. Share only a reviewed summary when the report contains local paths or diagnostics.

For automatic OpenClaw delivery, iBoard runs each required journey in two fresh containers serially (implementer and independent verifier). It always runs the Gateway check and adds the skills and configuration checks for changes to the respective source paths. The separate read-only review includes all required reports and the delivery gate checks their hashes and shared candidate image again. For manual use, an independent verifier must inspect the requirement and helper, rerun the command into a separate report, and compare source commit, image ID and helper digest before judging behavior.

## Cleanup

The helper stops only the child it started and deletes only its own temporary state. Before success it verifies the port is closed again. The Docker runner removes its own container on failure or timeout; require `cleanup_confirmed: true` even if the command exited zero.

## Helpers

`python3 -m workbench.verify_openclaw --help` lists the supported journeys and resource options. Run the helper through that command, never on the host.

Use `audit` in place of the journey name to check that the documentation and runner list the same journeys and exercise them against a single commit and image. Give it a budget that covers all journeys. This entry stops at the first failure. For a reusable project-local bundle, use the [generation skill](../create-verification-skill/SKILL.md) with `project-verification.json` and this directory as `--helpers`. Its [maintenance pass](../maintain-verification-skill/SKILL.md) records source coverage and attempts every journey while doctor stays healthy.
