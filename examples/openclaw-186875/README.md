# Real SQLite startup-lock harness

`real-startup-lock.mjs` runs a real Gateway with fresh temporary state, acquires an actual SQLite write transaction when the `sidecars.worker-environments` startup phase appears, and probes the container-local `/readyz` endpoint. It never constructs or mocks a SQLite error.

It explicitly writes fresh-state config `{ "plugins": { "enabled": false } }` to avoid loading unrelated bundled plugins in the memory-limited container. This is the same plugin setting used by the source repository's startup-worker-recovery tests; the older archived lock command did not itself write a minimal config, so this is an explicit scenario change rather than a claim to reproduce an undocumented old config. It does not set either channel/provider skip flag or deferred sidecar startup; inherited skip flags are removed. Core worker reconciliation and the real database are still exercised and the strict operation/phase assertions must pass. Transient success additionally requires the channel-start phase to be observed, even though this fresh-state config has no configured production channel accounts. This proves a scoped fresh-state scenario, not plugin-enabled or production-channel coverage.

Run only against an immutable OpenClaw image whose tracked source has been verified against the intended baseline or candidate commit. Record the exact commit, image ID and harness hash with each raw result. The script does not perform image/source verification itself.

```sh
node --check examples/openclaw-186875/real-startup-lock.mjs
node --test examples/openclaw-186875/startup-lock-log.test.mjs
# Set OPENCLAW_VERIFIED_IMAGE to a verified immutable sha256 image ID.
docker run --rm --network none --memory 7g --pids-limit 256 \
  --mount "type=bind,source=$PWD/examples/openclaw-186875/real-startup-lock.mjs,target=/harness.mjs,readonly" \
  "$OPENCLAW_VERIFIED_IMAGE" node /harness.mjs transient
```

Modes:

- `baseline`: hold the real write lock for 15 seconds; require the target operation's real lock error and an explicit fatal startup decision while the lock is still held, followed by a spontaneous nonzero Gateway exit within the bounded observation window. Normal shutdown may finish after release; its duration does not change when startup was declared fatal. Expected success marker: `REAL_LOCK_FAILURE_PASS`.
- `transient`: hold the same lock for 15 seconds; require a real lock-contention diagnostic for the same database and `placementJournals.prune`, at least two `workspaces` recovery starts, release, recovery measurement followed by `active` reconciliation, the outer worker-stage completion log and Gateway's ready log, then HTTP 200 while alive. A measurement-only log is not treated as success because the application emits it from `finally` on failure too. Expected success marker: `REAL_LOCK_RECOVERY_PASS`.
- `persistent`: keep the transaction open until Gateway exits or the 180-second observation deadline expires. Require a spontaneous nonzero exit before releasing the lock and no readiness. Timeout or harness-initiated termination is a failure, not evidence of correct bounded retry. Expected success marker: `REAL_LOCK_FAILURE_PASS`.

Each mode records stage observation, acquisition, release, readiness and process exit as timestamped `IBOARD_LOCK_EVENT` records, then emits `IBOARD_LOCK_RESULT`. It attempts bounded child cleanup; unconfirmed cleanup fails the run. Run baseline and candidate modes separately, serially, with no host port publication and no production mounts.

The first matching fatal startup lock log is separately timestamped as `fatal-startup-lock-failure`, including whether the transaction was still held. Baseline compares this decision time to lock release, not the later process-exit time. Persistent mode still holds the lock until spontaneous exit; neither mode may count harness-initiated termination as the application's failure.

The fatal parser recognizes both the source message `gateway sidecars failed to start:` and the formatted subsystem output `[gateway] sidecars failed to start:`, as well as the later CLI summary. Parser tests can additionally inspect a previously saved transcript with `IBOARD_CAPTURED_LOCK_LOG=/path/to/run.log node --test examples/openclaw-186875/startup-lock-log.test.mjs`. This checks that the earliest formatted fatal decision is observed during the lock-held interval; it never changes the archived run's result. Any harness correction requires new baseline and candidate runs using the same corrected script.

Before and after the run, the result captures cgroup v2 `memory.events`, `memory.current`, `memory.peak` and `memory.max` when readable. An increase in `oom_kill` fails application verification and is reported as an observed container OOM. If these files are unavailable, `oomKillObserved` is null; a SIGKILL alone must not be labelled proven OOM. A process killed before the target lock is acquired cannot prove either reproduction or recovery.

Use the same adequate memory budget for both versions and run serially. A trial under a 4 GiB limit recorded a cgroup OOM kill even after reaching the target path, so that trial is invalid; increasing the limit requires a fresh run rather than reclassifying its result.

Global cron or other database errors cannot satisfy the target-path assertion: diagnostics must name this run's database, `operation=placementJournals.prune`, `failureKind=lock-contention`, and SQLite primary code 5 or 6 while workspace recovery has started and has not reached active reconciliation. Failure scenarios must not reach active reconciliation or report HTTP readiness. Early HTTP 200 in transient mode is counted but never terminates the run successfully; both the target path and Gateway readiness logs must subsequently complete. Times are harness observation times, not instrumentation inside the worker. This depends on the specified source version's diagnostic and phase log contracts; changed log contracts must fail closed and require review rather than weakening the assertions.

This validates fresh-state startup contention and the externally observed recovery/failure sequence. It does not instrument internal retry counts, prove existing reconciliation journals are retained, verify channels with production configuration, or replace independent semantic review. Existing-journal recovery and non-lock error checks need separate verified scenarios. The harness must itself pass actual image runs before its successful markers can be treated as evidence; syntax checking alone proves no runtime behavior.

## Writable test prerequisites in a read-only sandbox

The official `scripts/run-vitest.mjs` prepares Kysely types under `/app/.artifacts` before running tests. The source-only image does not contain that ignored directory; using it directly with the runner's read-only root fails before test collection. This is an environment failure, not a passing test or evidence of an OpenClaw regression.

`images/openclaw-test/Dockerfile.sandbox-cache` is a test-only overlay for a locally verified baseline image. It requires `/app/.artifacts`, `/app/.cache`, `/app/node_modules/.vite-temp` and `/app/node_modules/.vite` to be absent and links those generated-cache paths to `/tmp`. Kysely and Vite both need these writes before test collection. The runner already mounts `/tmp` as a private, bounded 1024 MiB tmpfs (charged against the container memory limit), so generated files live only for that container. Tracked source remains unchanged and read-only; networking stays disabled. The baseline and candidate must both inherit this overlay and be source-verified again, with new reports. Do not substitute an overlay on only one version or reuse prior successful reports as proof of the new image.

```sh
# Verify and tag the intended local baseline first. No image is pulled.
DOCKER_BUILDKIT=0 docker build --network=none --pull=false \
  --build-arg BASE_IMAGE=iboard-openclaw-baseline:local \
  -f images/openclaw-test/Dockerfile.sandbox-cache \
  -t iboard-openclaw-baseline-cache:local images/openclaw-test
```

This overlay fails if any of those cache paths already exists, to avoid deleting or shadowing source. It does not make `/app/dist`, dependency installations, or the whole image writable. A different prerequisite that writes elsewhere must be diagnosed separately.

A 256 MiB tmpfs trial reached real test execution but exhausted space while compiling Vitest subprocess workers; its report is a failure. The runner now bounds private `/tmp` at 1024 MiB, still within the cgroup memory cap. Tests and assertions are unchanged.

After the cache overlay and 1024 MiB tmpfs correction, the unchanged official launcher command completed both selected files and all 9 tests with exit 0 and confirmed container cleanup. This is an environment smoke check only. The controlled workflow must still rerun baseline reproduction, both candidate executions and independent review on its own pinned images; no smoke report is imported as acceptance evidence.
