import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { DatabaseSync } from 'node:sqlite';
import { isSqliteLockError } from '/app/src/infra/sqlite-error-diagnostics.ts';
import { openOpenClawStateDatabase } from '/app/src/state/openclaw-state-db.ts';
import { createWorkerSessionPlacementStore } from '/app/src/gateway/worker-environments/placement-store.ts';
import { advancePlacementFixtureToActive } from '/app/src/gateway/worker-environments/placement-test-fixtures.ts';
import { REQUEST } from '/app/src/gateway/worker-environments/placement-dispatch-test-fixtures.ts';
import { journalOwner } from '/app/src/gateway/worker-environments/placement-suite.test-support.ts';
import { applyStagedWorkerWorkspace } from '/app/src/gateway/worker-environments/workspace-reconcile-apply.ts';
import { recoverGatewayWorkerPlacementWorkspaces } from '/app/src/gateway/server-worker-placement-workspace-recovery.ts';
import { retryStartupSqliteLock } from '/app/src/gateway/server-startup-sqlite-retry.ts';

const state = '/tmp/workbench-journal-state';
const root = '/tmp/workbench-journal-local';
const staged = '/tmp/workbench-journal-staged';
await Promise.all([mkdir(state), mkdir(root), mkdir(staged)]);
execFileSync('git', ['-C', root, 'init', '--quiet']);
await writeFile(`${root}/file.txt`, 'base content\n');
await writeFile(`${staged}/file.txt`, 'remote content\n');
const manifest = (text) => ({version: 1, baseCommit: null, entries: [{
  path: 'file.txt', type: 'file', mode: 0o644, size: Buffer.byteLength(text),
  sha256: createHash('sha256').update(text).digest('hex'),
}]});
const db = openOpenClawStateDatabase({env: {OPENCLAW_STATE_DIR: state}});
const placements = createWorkerSessionPlacementStore({database: db, now: () => 1000});
const active = await advancePlacementFixtureToActive(placements, db, REQUEST, {
  environmentId: 'worker-1', remoteWorkspaceDir: '/worker/workspace', seedEnvironment: 'before-dispatch',
  workspaceBaseManifestRef: `sha256:${'a'.repeat(64)}`,
});
const owner = journalOwner(active);
await applyStagedWorkerWorkspace({
  root, stagingRoot: staged, base: manifest('base content\n'), current: manifest('remote content\n'),
  baseManifestRef: `sha256:${'a'.repeat(64)}`, currentManifestRef: `sha256:${'b'.repeat(64)}`,
  acceptance: {kind: 'reconcile'}, journal: {
    load: async () => undefined,
    begin: async (journal) => placements.beginWorkspaceReconciliation(owner, journal),
    commit: async () => {}, abort: async () => {},
  },
});
assert.equal(await readFile(`${root}/file.txt`, 'utf8'), 'remote content\n');
assert.ok(await placements.loadWorkspaceReconciliation(owner));
console.log('JOURNAL_EXISTS_BEFORE_LOCK');
const lock = new DatabaseSync(db.path);
lock.exec('PRAGMA busy_timeout=0; BEGIN IMMEDIATE');
let attempts = 0;
const shutdown = process.argv[2] === 'shutdown';
let closing = false;
try {
  await retryStartupSqliteLock(async () => {
    attempts++;
    try {
      await recoverGatewayWorkerPlacementWorkspaces({
        placements, isStopping: () => false,
        resolveWorkspace: async () => ({kind: 'local', path: root}),
      });
    } catch (error) {
      assert.ok(isSqliteLockError(error), 'expected actual SQLite contention, not an unrelated failure');
      assert.equal(attempts, 1);
      console.log('ACTUAL_LOCK_ERROR', String(error));
      lock.exec('ROLLBACK');
      assert.ok(await placements.loadWorkspaceReconciliation(owner), 'journal disappeared after lock');
      console.log('JOURNAL_RETAINED_AFTER_LOCK');
      if (shutdown) closing = true;
      throw error;
    }
  }, () => closing, async () => {});
  if (shutdown) {
    assert.equal(attempts, 1);
    assert.equal(await readFile(`${root}/file.txt`, 'utf8'), 'remote content\n');
    assert.ok(await placements.loadWorkspaceReconciliation(owner));
    console.log('SHUTDOWN_DURING_LOCK_PASS attempts=1 journal=retained');
  } else {
    assert.equal(attempts, 2);
    assert.equal(await readFile(`${root}/file.txt`, 'utf8'), 'base content\n');
    assert.equal(await placements.loadWorkspaceReconciliation(owner), undefined);
    console.log('RECOVERY_AFTER_REAL_LOCK_PASS attempts=2 restored=base journal=cleared');
  }
} finally {
  lock.close();
}
process.exit(0);
