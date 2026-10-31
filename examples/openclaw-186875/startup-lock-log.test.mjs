import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { isFatalStartupLockLine } from './real-startup-lock.mjs';

// Minimal lines from the observed 7 GiB baseline run; no task logs are committed.
const observedEarlyFatal = '2026-10-31T17:48:08.980+00:00 [gateway] sidecars failed to start: Error: database is locked';
const observedLateSummary = '2026-10-31T17:48:26.636+00:00 Gateway failed to start: database is locked | ERR_SQLITE_ERROR | Spawn broker cleanup did not complete';

test('recognizes formatted Gateway sidecar failure before late CLI cleanup summary', () => {
  assert.equal(isFatalStartupLockLine(observedEarlyFatal), true);
  assert.equal(isFatalStartupLockLine(observedLateSummary), true);
  assert.equal(isFatalStartupLockLine('gateway sidecars failed to start: Error: SQLITE_BUSY'), true);
  assert.equal(isFatalStartupLockLine('[gateway] sidecars failed to start: Error: SQLITE_LOCKED'), true);
});

test('does not classify unrelated transaction errors or non-lock startup failures as fatal lock startup', () => {
  for (const line of [
    '[sqlite/transaction] operation=cron.history-maintenance database is locked',
    '[gateway] sidecars failed to start: Error: disk I/O error',
    '[gateway] startup trace: sidecars.worker-environments 5000ms',
    '[gateway] ready',
  ]) assert.equal(isFatalStartupLockLine(line), false, line);
});

test('saved baseline transcript attributes the first fatal decision to the lock-held interval',
  { skip: !process.env.IBOARD_CAPTURED_LOCK_LOG }, async () => {
    const text = await readFile(process.env.IBOARD_CAPTURED_LOCK_LOG, 'utf8');
    const lines = text.split('\n');
    const events = lines.filter((line) => line.startsWith('IBOARD_LOCK_EVENT '))
      .map((line) => JSON.parse(line.slice('IBOARD_LOCK_EVENT '.length)));
    const acquired = events.find((item) => item.type === 'lock-acquired');
    const released = events.find((item) => item.type === 'lock-released');
    const firstFatal = lines.find((line) => !line.startsWith('IBOARD_') && isFatalStartupLockLine(line));
    assert(acquired && released && firstFatal);
    const at = Date.parse(firstFatal.split(' ')[0]);
    assert(Number.isFinite(at));
    assert(at >= Date.parse(acquired.at), 'fatal log preceded acquisition');
    assert(at < Date.parse(released.at), 'parser missed the fatal decision during the real lock');
    assert(firstFatal.includes('[gateway] sidecars failed to start:'),
      'must recognize the formatted subsystem log, not only the late cleanup summary');
    // This only verifies parsing of a captured run; it must never rewrite its result.
  });
