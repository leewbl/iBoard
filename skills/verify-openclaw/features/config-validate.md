# Validate local configuration

## Sub-features

The CLI accepts a valid configuration and names the invalid field when a value fails the schema. The command leaves both input files unchanged.

## How to get to it (user POV)

Run `openclaw config validate --json` from a local OpenClaw installation.

## Driving it with iBoard

Run `python3 -m workbench.verify_openclaw config-validate` with an attested image and a new report path. The helper uses a temporary home and separate configuration for each check. It compares command exit codes, JSON diagnostics, file contents and local port availability.

## Gotchas

The string value for `gateway.port` is intentionally invalid. This is a local configuration exercise with a disposable test profile.
