## Sub-features

Start a local Gateway, detect when it is ready, and query health using the CLI.

## How to get to it (user POV)

Run `openclaw gateway` locally, then run `openclaw gateway health --json` from another terminal against the running instance.

## Driving it with iBoard

From iBoard run `python3 -m workbench.verify_openclaw gateway-health` with the pinned OpenClaw source checkout, local image and an unused report path as shown in [the verification skill](../SKILL.md). It exercises an explicit 30-second RPC timeout, matching the example in the OpenClaw health reference pilot. Inspect the readiness and CLI health records, not just the overall exit code.

## Gotchas

Liveness by itself does not imply readiness. This fresh-state journey covers the core Gateway, not configured channels or plugins, model inference, remote connectivity, or production data.
