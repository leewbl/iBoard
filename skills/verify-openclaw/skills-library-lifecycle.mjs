import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, open, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { promisify } from 'node:util';

const execute = promisify(execFile);
const entry = ['--import', '/app/node_modules/tsx/dist/loader.mjs', '/app/src/entry.ts'];
const origin = 'http://127.0.0.1:18789';
const socket = 'ws://127.0.0.1:18789';
const token = 'iboard-disposable-library-token';
const targetKey = 'agent:main:iboard-library-session';
const controlKey = 'agent:main:iboard-library-control';
const slug = 'iboard-library-lifecycle';
const root = await mkdtemp('/tmp/iboard-skills-library-');
const home = join(root, 'home');
const state = join(root, 'state');
const config = join(state, 'openclaw.json');
const log = join(root, 'gateway.log');
const bundle = join(root, 'skill');
const revisionFile = join(root, 'edited', 'SKILL.md');
const receipts = join(root, 'cli-receipts');
const firstContent = `---\nname: ${slug}\ndescription: Disposable library fixture\n---\n\nFirst approved procedure.\n`;
const secondContent = firstContent.replace('First approved procedure.', 'Second approved procedure.');
const supportContent = 'The support file should survive a single-file edit.\n';
const digest = value => createHash('sha256').update(value).digest('hex');
const emit = (phase, fields) => console.log(JSON.stringify({ phase, ...fields }));
const env = {
  PATH: process.env.PATH, HOME: home, OPENCLAW_HOME: home, OPENCLAW_STATE_DIR: state,
  OPENCLAW_CONFIG_PATH: config, TMPDIR: root, XDG_CACHE_HOME: join(root, 'cache'),
  OPENCLAW_DISABLE_BONJOUR: '1', OPENCLAW_SKIP_CHANNELS: '1', OPENCLAW_SKIP_PROVIDERS: '1',
  OPENCLAW_SKIP_CRON: '1', OPENCLAW_SKIP_GMAIL_WATCHER: '1', OPENCLAW_SKIP_BROWSER_CONTROL_SERVER: '1',
  OPENCLAW_SKIP_CANVAS_HOST: '1', NO_COLOR: '1',
  GOMAXPROCS: '2', UV_THREADPOOL_SIZE: '2', NODE_OPTIONS: '--v8-pool-size=2 --max-semi-space-size=4',
};
let gateway, closed, exit, launchError, passed = false;
const refused = error => error.cause?.code === 'ECONNREFUSED';
const health = path => fetch(origin + path, { signal: AbortSignal.timeout(4000), redirect: 'error' });

async function startGateway() {
  await assert.rejects(health('/healthz'), refused);
  exit = undefined;
  launchError = undefined;
  const logfile = await open(log, 'a', 0o600);
  try {
    gateway = spawn(process.execPath, [...entry, 'gateway', '--port', '18789', '--bind', 'loopback'],
      { cwd: '/app', env, stdio: ['ignore', logfile.fd, logfile.fd] });
    gateway.once('error', error => { launchError = error; });
    closed = new Promise(resolve => gateway.once('close', (code, signal) => {
      exit = { code, signal };
      resolve();
    }));
  } finally {
    await logfile.close();
  }
  const deadline = performance.now() + 130_000;
  while (performance.now() < deadline && !exit && !launchError) {
    const [ready, live] = await Promise.all([
      health('/readyz').then(async response => response.ok && (await response.json()).ready === true).catch(() => false),
      health('/healthz').then(async response => response.ok && (await response.json()).ok === true).catch(() => false),
    ]);
    if (ready && live) return { gatewayPid: gateway.pid, ready, live };
    await sleep(250);
  }
  assert.ifError(launchError);
  assert.fail('The disposable library Gateway failed its health check');
}

async function stopGateway() {
  if (!gateway || exit) return;
  gateway.kill('SIGTERM');
  const timer = setTimeout(() => gateway.kill('SIGKILL'), 30_000);
  try { await closed; } finally { clearTimeout(timer); }
}

async function cli(args, expectedError) {
  assert.ok(!exit, 'The Gateway exited before the CLI check');
  const command = [...entry, ...args, '--url', socket, '--token', token, '--timeout', '40000', '--json'];
  let run;
  try {
    run = await execute(process.execPath, command,
      { cwd: '/app', env, timeout: 55_000, killSignal: 'SIGKILL', maxBuffer: 256 * 1024 });
  } catch (error) {
    const diagnostics = String(error.stderr ?? '').replaceAll(token, '[fixture token]');
    if (expectedError && error.code === 1 && !error.killed && !error.signal) {
      assert.match(diagnostics + '\n' + String(error.stdout ?? ''), expectedError);
      return { exitCode: error.code };
    }
    throw new Error(`CLI ${args.slice(0, 3).join(' ')} failed with ${error.code ?? error.signal}: ${diagnostics.slice(-1500)}`);
  }
  assert.ok(!expectedError, 'The CLI accepted a request expected to fail');
  return JSON.parse(run.stdout);
}

const lib = (args, error) => cli(['skills', 'library', ...args], error);
const call = (method, params) => cli(['gateway', 'call', method, '--params', JSON.stringify(params)]);

async function localBackendConfig() {
  const id = 'iboard-library-cli';
  const plugin = join(home, id);
  const workspace = join(home, 'fixture-workspace');
  const backend = join(plugin, 'backend.mjs');
  await Promise.all([mkdir(plugin), mkdir(workspace), mkdir(receipts)]);
  const backendConfig = {
    command: process.execPath, args: [backend, state, receipts], output: 'text', input: 'stdin',
    sessionMode: 'none', systemPromptWhen: 'always', systemPromptArg: '--system-prompt',
  };
  await Promise.all([
    writeFile(join(plugin, 'package.json'), JSON.stringify({
      name: id, version: '1.0.0', type: 'module', openclaw: { extensions: ['./index.js'] },
    })),
    writeFile(join(plugin, 'openclaw.plugin.json'), JSON.stringify({
      id, name: id, cliBackends: [id], activation: { onStartup: true },
      configSchema: { type: 'object', properties: {}, additionalProperties: false },
    })),
    writeFile(join(plugin, 'index.js'), `export default {
      id: ${JSON.stringify(id)},
      register(api) {
        api.registerCliBackend({ id: ${JSON.stringify(id)}, config: ${JSON.stringify(backendConfig)} });
      },
    };`),
    writeFile(backend, String.raw`
      import assert from 'node:assert/strict';
      import { createHash } from 'node:crypto';
      import { readFileSync, writeFileSync } from 'node:fs';
      import { dirname, join, relative } from 'node:path';
      const [state, receipts, flag, system] = process.argv.slice(2);
      assert.equal(flag, '--system-prompt');
      assert.ok(system);
      let input = '';
      for await (const chunk of process.stdin) input += chunk;
      const runId = [...input.matchAll(/\bIBOARD_LIBRARY_TURN:([0-9a-f-]{36})\b/gu)].at(-1)?.[1];
      assert.ok(runId);
      const paths = [...system.matchAll(/<location>([^<]+)<\/location>/gu)]
        .map(match => match[1]).filter(path => path.includes('/skill-library/'));
      assert.ok(paths.length <= 1);
      const path = paths[0];
      const skillPath = path ? relative(state, path) : null;
      if (path) assert.match(skillPath, /^skill-library\/[^/]+\/revisions\/[0-9a-f]{64}\/SKILL\.md$/u);
      const sha = path => createHash('sha256').update(readFileSync(path)).digest('hex');
      writeFileSync(join(receipts, runId + '.json'), JSON.stringify({
        cliPid: process.pid, skillPath, contentSha: path ? sha(path) : null,
        supportSha: path ? sha(join(dirname(path), 'reference.txt')) : null,
      }), { flag: 'wx' });
      process.stdout.write('Offline CLI completed the library turn.\n');
    `),
  ]);
  return {
    agents: { defaults: { workspace, model: id + '/proof', timeoutSeconds: 90 }, entries: { main: {} } },
    plugins: { allow: [id], load: { paths: [plugin] }, entries: { [id]: { enabled: true } }, slots: { memory: 'none' } },
  };
}

async function observeTurn(stage, session, skillId, revision, contentSha) {
  const runId = randomUUID();
  const sessionKey = session.key;
  const started = await call('chat.send', {
    sessionKey, message: `IBOARD_LIBRARY_TURN:${runId}`, idempotencyKey: runId, deliver: false,
  });
  assert.equal(started.runId, runId);
  assert.equal(started.status, 'started');
  const result = await call('agent.wait', { runId, timeoutMs: 38_000 });
  assert.equal(result.runId, runId);
  assert.equal(result.status, 'ok', `The ${stage} CLI turn did not complete: ${JSON.stringify(result)}`);
  const receipt = JSON.parse(await readFile(join(receipts, `${runId}.json`), 'utf8'));
  assert.ok(Number.isInteger(receipt.cliPid) && receipt.cliPid > 1 && receipt.cliPid !== gateway.pid);
  assert.equal(receipt.skillPath, revision ? `skill-library/${skillId}/revisions/${revision}/SKILL.md` : null);
  assert.equal(receipt.contentSha, contentSha ?? null);
  assert.equal(receipt.supportSha, revision ? hashes.support : null);
  const current = await call('sessions.describe', { key: sessionKey, agentId: 'main' });
  assert.equal(current.session?.sessionId, session.sessionId);
  return { stage, sessionKey, sessionId: current.session.sessionId, runId, status: result.status, ...receipt };
}

async function selected(key) {
  const result = await lib(['list', '--scope', 'mine', '--session', key]);
  assert.equal(result.session?.sessionKey, key);
  assert.ok(Array.isArray(result.session.selections));
  return result;
}

const revisions = {};
const hashes = { original: digest(firstContent), updated: digest(secondContent), support: digest(supportContent) };
const steps = {};

function onlyPin(result, key, skillId) {
  assert.equal(result.sessionKey, key);
  assert.equal(result.selections.length, 1);
  assert.equal(result.selections[0].skillId, skillId);
  return result.selections[0].revision;
}

function contentProof(data, skillId, revision, content) {
  assert.equal(data.entry.skillId, skillId);
  assert.equal(data.entry.revision, revision);
  assert.equal(data.content, content);
  assert.equal(data.files.length, 1);
  const [file] = data.files;
  assert.equal(file.path, 'reference.txt');
  const support = file.encoding === 'base64' ? Buffer.from(file.content, 'base64') : Buffer.from(file.content, 'utf8');
  assert.equal(support.toString('utf8'), supportContent);
  return { contentSha: digest(data.content), supportSha: digest(support) };
}

async function drive() {
  const turns = [];
  const initial = await lib(['list', '--scope', 'mine']);
  assert.ok(typeof initial.profileId === 'string' && initial.profileId, 'The CLI did not receive the durable owner profile');
  assert.deepEqual(initial.entries, []);
  const profileId = initial.profileId;
  const target = await call('sessions.create', { key: targetKey, agentId: 'main', label: 'Library pin fixture' });
  const control = await call('sessions.create', { key: controlKey, agentId: 'main', label: 'Unchanged library control' });
  assert.equal(target.key, targetKey);
  assert.equal(control.key, controlKey);
  assert.ok(target.sessionId && control.sessionId && target.sessionId !== control.sessionId);
  const sessions = { targetKey, targetId: target.sessionId, controlKey, controlId: control.sessionId };
  const initialTarget = await selected(targetKey);
  const initialControl = await selected(controlKey);
  assert.deepEqual(initialTarget.session.selections, []);
  assert.deepEqual(initialControl.session.selections, []);
  steps.initial = { libraryCount: initial.entries.length, targetSelections: initialTarget.session.selections.length,
    controlSelections: initialControl.session.selections.length };

  const created = await lib(['create', bundle, '--slug', slug]);
  assert.equal(created.state, 'published');
  assert.equal(created.target, 'personal');
  assert.equal(created.sessionActivation, 'new-sessions');
  const { skillId, ownerProfileId, enabled, shared, revision: original } = created.entry;
  assert.ok(typeof skillId === 'string' && skillId);
  assert.equal(created.entry.slug, slug);
  assert.equal(ownerProfileId, profileId);
  assert.equal(enabled, true);
  assert.equal(shared, false);
  assert.match(original, /^[0-9a-f]{64}$/u);
  const originalRead = contentProof(await lib(['read', skillId]), skillId, original, firstContent);
  steps.created = { revision: original, ownerProfileId, enabled, shared, supportSha: originalRead.supportSha };
  revisions.original = original;
  const mine = await selected(targetKey);
  assert.equal(mine.profileId, profileId);
  assert.deepEqual(mine.entries.map(item => item.skillId), [skillId]);
  assert.deepEqual(mine.session.selections, [], 'Creating a skill silently attached it to an existing session');
  assert.deepEqual(mine.session.attachable.map(item => item.skillId), [skillId]);
  const attached = await lib(['attach', '--session', targetKey, '--skill-id', skillId, '--revision', original]);
  assert.equal(attached.sessionActivation, 'next-turn');
  assert.equal(onlyPin(attached, targetKey, skillId), original);
  contentProof(await lib(['read', skillId, '--session', targetKey, '--revision', original]), skillId, original, firstContent);
  steps.attached = { pin: original, activation: attached.sessionActivation };
  turns.push(await observeTurn('attached', target, skillId, original, hashes.original));

  const changed = await lib(['update', skillId, revisionFile, '--expected-revision', original]);
  assert.equal(changed.state, 'published');
  assert.equal(changed.entry.skillId, skillId);
  const updated = changed.entry.revision;
  assert.match(updated, /^[0-9a-f]{64}$/u);
  assert.notEqual(updated, original);
  revisions.updated = updated;
  const pinAfterUpdate = onlyPin((await selected(targetKey)).session, targetKey, skillId);
  assert.equal(pinAfterUpdate, original);
  const current = await lib(['read', skillId]);
  const updatedRead = contentProof(current, skillId, updated, secondContent);
  assert.deepEqual(current.revisions.map(row => row.revision).sort(), [original, updated].sort());
  steps.updated = { revision: updated, pin: pinAfterUpdate, supportSha: updatedRead.supportSha };
  turns.push(await observeTurn('after-update', target, skillId, original, hashes.original));

  const stale = await lib(['update', skillId, join(bundle, 'SKILL.md'), '--expected-revision', original],
    /Skill changed\. Read the current revision/u);
  contentProof(await lib(['read', skillId]), skillId, updated, secondContent);
  steps.stale = { exitCode: stale.exitCode, preservedRevision: updated, conflict: true };

  const refreshed = await lib(['refresh', '--session', targetKey, '--skill-id', skillId]);
  assert.equal(refreshed.sessionActivation, 'next-turn');
  assert.equal(onlyPin(refreshed, targetKey, skillId), updated);
  await lib(['read', skillId, '--session', targetKey, '--revision', original],
    /Session reads require an exact selected skillId and revision/u);
  assert.deepEqual((await selected(controlKey)).session.selections, []);
  await lib(['read', skillId, '--session', controlKey, '--revision', updated],
    /Session reads require an exact selected skillId and revision/u);
  steps.refreshed = { pin: updated, oldPinRejected: true, controlUnselected: true, controlReadRejected: true };
  turns.push(await observeTurn('refreshed', target, skillId, updated, hashes.updated));
  turns.push(await observeTurn('control', control));

  const disabled = await lib(['disable', skillId, '--expected-revision', updated]);
  assert.equal(disabled.entry.enabled, false);
  assert.equal(disabled.entry.revision, updated);
  const listedDisabled = (await lib(['list', '--scope', 'mine'])).entries.find(row => row.skillId === skillId);
  assert.equal(listedDisabled?.enabled, false);
  const reenabled = await lib(['enable', skillId, '--expected-revision', updated]);
  assert.equal(reenabled.entry.enabled, true);
  assert.equal(reenabled.entry.revision, updated);
  const listedEnabled = (await lib(['list', '--scope', 'mine'])).entries.find(row => row.skillId === skillId);
  assert.equal(listedEnabled?.enabled, true);
  steps.toggled = { disabled: listedDisabled.enabled, enabled: listedEnabled.enabled, revision: reenabled.entry.revision };

  const rollback = await lib(['rollback', skillId, '--expected-revision', updated, '--revision', original]);
  assert.equal(rollback.entry.revision, original);
  const restored = contentProof(await lib(['read', skillId]), skillId, original, firstContent);
  const rollbackPin = onlyPin((await selected(targetKey)).session, targetKey, skillId);
  assert.equal(rollbackPin, updated);
  steps.rollback = { revision: rollback.entry.revision, pin: rollbackPin, supportSha: restored.supportSha };

  const previousPid = gateway.pid;
  await stopGateway();
  assert.deepEqual(exit, { code: 0, signal: null });
  const previousExit = exit;
  const doctor = await startGateway();
  assert.notEqual(doctor.gatewayPid, previousPid);
  emit('restart', { ...doctor, previousPid, previousExit, portClosed: true });
  const durable = await selected(targetKey);
  assert.equal(durable.profileId, profileId);
  assert.equal(durable.entries.length, 1);
  assert.equal(durable.entries[0].skillId, skillId);
  assert.equal(durable.entries[0].revision, original);
  assert.equal(onlyPin(durable.session, targetKey, skillId), updated);
  assert.deepEqual((await selected(controlKey)).session.selections, []);
  const targetId = (await call('sessions.describe', { key: targetKey, agentId: 'main' })).session?.sessionId;
  const controlId = (await call('sessions.describe', { key: controlKey, agentId: 'main' })).session?.sessionId;
  assert.equal(targetId, target.sessionId);
  assert.equal(controlId, control.sessionId);
  const durableRead = contentProof(await lib(['read', skillId, '--session', targetKey, '--revision', updated]),
    skillId, updated, secondContent);
  steps.restarted = { profileId: durable.profileId, revision: durable.entries[0].revision, pin: updated,
    targetId, controlId, pinContentSha: durableRead.contentSha, supportSha: durableRead.supportSha };
  turns.push(await observeTurn('restarted', target, skillId, updated, hashes.updated));

  const removed = await lib(['remove', skillId, '--expected-revision', original]);
  assert.equal(removed.state, 'removed');
  assert.equal(removed.entry.removed, true);
  assert.equal(removed.entry.skillId, skillId);
  const afterRemove = await selected(targetKey);
  assert.deepEqual(afterRemove.entries, []);
  assert.equal(onlyPin(afterRemove.session, targetKey, skillId), updated);
  const removedOwnerRead = await lib(['read', skillId]);
  const direct = contentProof(removedOwnerRead, skillId, original, firstContent);
  assert.equal(removedOwnerRead.entry.removed, true);
  await lib(['update', skillId, revisionFile, '--expected-revision', original],
    /Removed skills cannot be edited or selected again/u);
  await lib(['attach', '--session', controlKey, '--skill-id', skillId, '--revision', updated],
    /Removed skill cannot be selected/u);
  assert.deepEqual((await selected(controlKey)).session.selections, []);
  const pinned = contentProof(await lib(['read', skillId, '--session', targetKey, '--revision', updated]),
    skillId, updated, secondContent);
  steps.removed = { listedCount: afterRemove.entries.length, directReadMarkedRemoved: removedOwnerRead.entry.removed,
    directRevision: removedOwnerRead.entry.revision, directContentSha: direct.contentSha, editRejected: true,
    reattachRejected: true, pin: updated, pinContentSha: pinned.contentSha };

  const detached = await lib(['detach', '--session', targetKey, '--skill-id', skillId]);
  assert.equal(detached.sessionKey, targetKey);
  assert.equal(detached.sessionActivation, 'next-turn');
  assert.deepEqual(detached.selections, []);
  const afterDetach = await selected(targetKey);
  assert.deepEqual(afterDetach.session.selections, []);
  await lib(['read', skillId, '--session', targetKey, '--revision', updated],
    /Session reads require an exact selected skillId and revision/u);
  steps.detached = { activation: detached.sessionActivation, remaining: afterDetach.session.selections.length, pinReadRejected: true };
  turns.push(await observeTurn('detached', target));
  return { profileId, skillId, revisions, hashes, sessions, steps, turns };
}

try {
  await mkdir(home);
  await mkdir(state);
  await mkdir(bundle);
  await mkdir(join(root, 'edited'));
  const backendConfig = await localBackendConfig();
  await Promise.all([
    writeFile(config, JSON.stringify({
      ...backendConfig, cron: { enabled: false }, update: { checkOnStart: false },
      gateway: { mode: 'local', auth: { mode: 'token', token }, controlUi: { enabled: false } },
    }), { mode: 0o600 }),
    writeFile(join(bundle, 'SKILL.md'), firstContent),
    writeFile(join(bundle, 'reference.txt'), supportContent),
    writeFile(revisionFile, secondContent),
  ]);
  emit('doctor', await startGateway());
  emit('library', await drive());
  passed = true;
} finally {
  try {
    await stopGateway();
    if (passed) {
      assert.deepEqual(exit, { code: 0, signal: null });
      await assert.rejects(health('/healthz'), refused);
    } else {
      const tail = await readFile(log, 'utf8').catch(() => '');
      process.stderr.write(tail.slice(-3500).replaceAll(token, '[fixture token]'));
    }
  } finally {
    await rm(root, { recursive: true, force: true });
    emit('cleanup', { exit: exit ?? null, scratchRemoved: true });
  }
}
console.log('VERIFY_OPENCLAW_SKILLS_LIBRARY_LIFECYCLE_PASS');
