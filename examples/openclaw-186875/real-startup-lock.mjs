/** Run only inside an isolated, source-verified OpenClaw image. No production data. */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import http from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { performance } from 'node:perf_hooks';
import { DatabaseSync } from 'node:sqlite';
import { pathToFileURL } from 'node:url';

export function isFatalStartupLockLine(line) {
  return /(?:\bgateway sidecars failed to start:|\[gateway\] sidecars failed to start:|\bGateway failed to start:)[^\n]*(?:database is locked|SQLITE_BUSY|SQLITE_LOCKED)/i.test(line);
}

async function main() {
const mode = process.argv[2];
assert(['baseline', 'transient', 'persistent'].includes(mode),
  'usage: node real-startup-lock.mjs baseline|transient|persistent');
const holdMs = 15_000;
const deadlineMs = 180_000;
const origin = performance.now();
const elapsed = () => Math.round(performance.now() - origin);
const root = await mkdtemp(join(tmpdir(), 'iboard-real-lock-'));
const state = join(root, 'state');
const home = join(root, 'home');
await Promise.all([mkdir(state), mkdir(home)]);
// Empty plugin selection avoids unrelated bundled plugin loading in this
// fresh-state scenario. Core worker startup and its reconciliation remain live.
const config = { plugins: { enabled: false } };
await writeFile(join(state, 'openclaw.json'), JSON.stringify(config));
const events = [];
const event = (type, details = {}) => {
  const item = { type, at: new Date().toISOString(), elapsedMs: elapsed(), ...details };
  events.push(item);
  console.log('IBOARD_LOCK_EVENT', JSON.stringify(item));
  return item;
};
function memorySnapshot() {
  const result = {};
  for (const name of ['memory.events', 'memory.current', 'memory.peak', 'memory.max']) {
    try { result[name] = readFileSync(`/sys/fs/cgroup/${name}`, 'utf8').trim(); }
    catch { result[name] = null; }
  }
  return result;
}
function oomKills(snapshot) {
  const value = snapshot['memory.events']?.match(/^oom_kill\s+(\d+)$/m)?.[1];
  return value === undefined ? null : Number(value);
}
function observedOomKill(before, after) {
  const first = oomKills(before);
  const last = oomKills(after);
  return first === null || last === null ? null : last > first;
}
const memoryBefore = memorySnapshot();
event('isolated-test-config', { config, skipChannels: false, skipProviders: false,
  deferredSidecars: false });
event('cgroup-memory-before', memoryBefore);
let output = '';
let afterAcquire = '';
let lock;
let acquired;
let released;
let readiness;
let exit;
let acquireError;
let releaseTimer;
let cleanupStarted = false;
const workspaceAttempts = [];
const targetLockErrors = [];
let workspaceFinished;
let activeStarted;
let workerStageFinished;
let gatewayReadyLog;
let fatalStartupLockFailure;
let prematureHttpResponses = 0;
let channelStartupCompleted;
const lineBuffers = { stdout: '', stderr: '' };
const childEnv = { ...process.env, HOME: home, OPENCLAW_STATE_DIR: state,
  OPENCLAW_CONFIG_PATH: join(state, 'openclaw.json'), OPENCLAW_GATEWAY_STARTUP_TRACE: '1' };
delete childEnv.OPENCLAW_SKIP_CHANNELS;
delete childEnv.OPENCLAW_SKIP_PROVIDERS;
const child = spawn(process.execPath, ['--import', '/app/node_modules/tsx/dist/loader.mjs',
  '/app/src/entry.ts', 'gateway', '--port', '18789', '--bind', 'loopback', '--allow-unconfigured'], {
  cwd: '/app',
  env: childEnv,
  stdio: ['ignore', 'pipe', 'pipe'],
});
let spawnError;
child.on('error', (error) => { spawnError = error.message; });
child.on('exit', (code, signal) => {
  exit = event('gateway-exit', { code, signal, cleanupStarted });
});
function releaseLock() {
  if (!lock || released) return;
  lock.exec('ROLLBACK');
  lock.close();
  lock = undefined;
  released = event('lock-released');
}
function inspectLine(line) {
  if (!acquired) return;
  if (/startup phase: sidecars\.worker-environments\.workspaces starting\b/.test(line)) {
    workspaceAttempts.push(event('workspace-recovery-started', { attempt: workspaceAttempts.length + 1 }));
  }
  if (workspaceAttempts.length && !activeStarted &&
      line.includes(`database=${join(state, 'state', 'openclaw.sqlite')} `) &&
      /\boperation=placementJournals\.prune\s/.test(line) &&
      /\bfailureKind=lock-contention\b/.test(line) &&
      /\bsqlitePrimaryCode=(?:5|6)\b/.test(line)) {
    targetLockErrors.push(event('workspace-prune-lock-error', { attempt: workspaceAttempts.length,
      operation: 'placementJournals.prune', diagnostic: line }));
  }
  if (/startup trace: sidecars\.worker-environments\.workspaces \d/.test(line)) {
    workspaceFinished = event('workspace-recovery-measurement-ended', { attempt: workspaceAttempts.length });
  }
  // A measurement's finally log also occurs on failure. Only reaching active
  // proves the awaited workspace recovery returned successfully in this path.
  if (/startup phase: sidecars\.worker-environments\.active starting\b/.test(line)) {
    activeStarted = event('active-reconciliation-started');
  }
  if (/startup trace: sidecars\.worker-environments \d/.test(line)) {
    workerStageFinished = event('worker-stage-measurement-ended');
  }
  if (/\[gateway\] ready\s*$/.test(line)) gatewayReadyLog = event('gateway-ready-log');
  if (!fatalStartupLockFailure && isFatalStartupLockLine(line)) {
    fatalStartupLockFailure = event('fatal-startup-lock-failure', {
      lockHeld: Boolean(lock && !released), diagnostic: line,
    });
  }
  if (/startup trace: sidecars\.channel-start \d/.test(line)) {
    channelStartupCompleted = event('channel-startup-measurement-ended');
  }
}
function collect(data, stream) {
  const text = data.toString();
  process.stdout.write(text);
  output += text;
  if (acquired) afterAcquire += text;
  if (output.length > 4_000_000) output = output.slice(-4_000_000);
  if (afterAcquire.length > 2_000_000) afterAcquire = afterAcquire.slice(-2_000_000);
  if (!acquired && !acquireError && output.includes('sidecars.worker-environments starting')) {
    event('target-stage-observed', { stage: 'sidecars.worker-environments' });
    try {
      lock = new DatabaseSync(join(state, 'state', 'openclaw.sqlite'));
      lock.exec('PRAGMA busy_timeout=0; BEGIN IMMEDIATE');
      acquired = event('lock-acquired', { statement: 'BEGIN IMMEDIATE' });
      // Persistent mode never releases the lock before the observed process exit.
      if (mode !== 'persistent') releaseTimer = setTimeout(releaseLock, holdMs);
    } catch (error) {
      acquireError = String(error);
      event('lock-acquire-failed', { error: acquireError });
    }
  }
  lineBuffers[stream] += text;
  const lines = lineBuffers[stream].split('\n');
  lineBuffers[stream] = lines.pop();
  for (const line of lines) inspectLine(line);
}
child.stdout.on('data', (data) => collect(data, 'stdout'));
child.stderr.on('data', (data) => collect(data, 'stderr'));
function probe() {
  return new Promise((resolve) => {
    const request = http.get('http://127.0.0.1:18789/readyz', (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on('error', () => resolve(false));
    request.setTimeout(500, () => request.destroy());
  });
}
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let failure;
try {
  while (elapsed() < deadlineMs && !exit && !spawnError && !acquireError) {
    if (await probe()) {
      if (released && activeStarted && workspaceFinished && workerStageFinished && gatewayReadyLog &&
          channelStartupCompleted) {
        readiness = event('gateway-ready-http', { status: 200 });
        break;
      }
      // Early HTTP 200 alone is not evidence that this startup path recovered.
      prematureHttpResponses++;
    }
    await wait(250);
  }
  assert(!spawnError, `Gateway spawn failed: ${spawnError}`);
  assert(observedOomKill(memoryBefore, memorySnapshot()) !== true,
    'Container cgroup recorded an OOM kill; application verification is not established');
  assert(acquired && !acquireError, 'Real lock was not acquired at the target startup stage');
  assert(targetLockErrors.length > 0,
    'No real lock-contention diagnostic for placementJournals.prune in workspace recovery');
  if (mode === 'transient') {
    assert(released, 'Lock release was not observed');
    assert(readiness && readiness.elapsedMs >= released.elapsedMs,
      'Gateway must become ready after release, not while the lock remains held');
    assert(!exit, 'Gateway exited before readiness');
    assert(workspaceAttempts.length >= 2, 'The failed workspace recovery was not retried');
    assert(activeStarted && workspaceFinished && workerStageFinished && gatewayReadyLog,
      'The same workspace recovery path and Gateway startup did not complete');
    assert(activeStarted.elapsedMs >= released.elapsedMs &&
      workspaceFinished.elapsedMs >= released.elapsedMs &&
      activeStarted.elapsedMs >= targetLockErrors.at(-1).elapsedMs,
      'Successful workspace recovery must follow target lock failure and release');
    assert(!fatalStartupLockFailure,
      'A fatal sidecar lock error was emitted');
    // Successful readiness after an observed real lock error proves application recovery;
    // this is not a claim that internal retry attempt counts were instrumented.
  } else {
    assert(fatalStartupLockFailure,
      'Expected fatal Gateway startup failure attributed to the real lock');
    assert(exit && !exit.cleanupStarted && exit.code !== null && exit.code !== 0,
      'Expected a spontaneous nonzero Gateway exit, not harness timeout or cleanup');
    assert(!readiness, 'Gateway became ready in the expected failure scenario');
    assert(!activeStarted && !gatewayReadyLog && prematureHttpResponses === 0,
      'Gateway advanced beyond failed workspace recovery or reported readiness');
    assert(fatalStartupLockFailure.lockHeld &&
      (!released || fatalStartupLockFailure.elapsedMs <= released.elapsedMs),
      'The fatal startup decision must occur while the real lock remains held');
    if (mode === 'persistent') {
      assert(!released, 'Persistent lock must remain held through spontaneous Gateway exit');
    }
  }
  event('scenario-pass', { mode, realApplicationLockError: true });
} catch (error) {
  failure = error;
  event('scenario-fail', { mode, error: error.message });
} finally {
  clearTimeout(releaseTimer);
  cleanupStarted = true;
  if (!exit && child.pid) {
    child.kill('SIGTERM');
    const until = performance.now() + 10_000;
    while (!exit && performance.now() < until) await wait(100);
    if (!exit) {
      child.kill('SIGKILL');
      const killUntil = performance.now() + 5_000;
      while (!exit && performance.now() < killUntil) await wait(50);
    }
  }
  try { releaseLock(); } catch (error) { failure ??= error; }
  if (child.pid && !exit) failure ??= new Error('Gateway cleanup was not confirmed');
  const memoryAfter = memorySnapshot();
  const oomKillObserved = observedOomKill(memoryBefore, memoryAfter);
  if (oomKillObserved) failure ??= new Error('Container cgroup recorded an OOM kill');
  event('cgroup-memory-after', memoryAfter);
  console.log('IBOARD_LOCK_RESULT', JSON.stringify({ mode, passed: !failure, events,
    prematureHttpResponses, targetOperation: 'placementJournals.prune',
    memoryBefore, memoryAfter, oomKillObserved,
    cleanupConfirmed: !child.pid || Boolean(exit), error: failure?.message }));
}
if (failure) process.exitCode = 1;
else console.log(mode === 'transient' ? 'REAL_LOCK_RECOVERY_PASS' : 'REAL_LOCK_FAILURE_PASS');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
