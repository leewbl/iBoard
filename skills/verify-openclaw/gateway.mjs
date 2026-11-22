import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { mkdir, mkdtemp, open, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { promisify } from 'node:util';

const url = 'http://127.0.0.1:18789';
const ws = 'ws://127.0.0.1:18789';
const entry = ['--import', '/app/node_modules/tsx/dist/loader.mjs', '/app/src/entry.ts'];
const emit = (phase, fields = {}) => console.log(JSON.stringify({ phase, ...fields }));
const root = await mkdtemp('/tmp/iboard-openclaw-');
const home = join(root, 'home');
const state = join(root, 'state');
const config = join(state, 'openclaw.json');
const log = join(root, 'gateway.log');
let server, closed, result, launchError;
let passed = false;

async function probe(path) {
  const response = await fetch(url + path, {
    signal: AbortSignal.timeout(6000), redirect: 'error',
  });
  return { status: response.status, body: await response.json() };
}

try {
  const env = {
    PATH: process.env.PATH, HOME: home, OPENCLAW_HOME: home,
    OPENCLAW_STATE_DIR: state, OPENCLAW_CONFIG_PATH: config,
    TMPDIR: root, XDG_CACHE_HOME: join(root, 'cache'),
    OPENCLAW_DISABLE_BONJOUR: '1',
  };
  await mkdir(home);
  await mkdir(state);
  const token = 'iboard-disposable-local-test-token';
  await writeFile(config, JSON.stringify({
    plugins: { enabled: false },
    gateway: { mode: 'local', auth: { mode: 'token', token } },
  }), { mode: 0o600 });
  await assert.rejects(fetch(url + '/healthz', { signal: AbortSignal.timeout(1000) }),
    error => error.cause?.code === 'ECONNREFUSED');
  const handle = await open(log, 'w');
  try {
    server = spawn(process.execPath, [...entry, 'gateway', '--port', '18789', '--bind', 'loopback'],
      { cwd: '/app', env, stdio: ['ignore', handle.fd, handle.fd] });
    closed = new Promise(resolve => {
      server.on('error', error => { launchError = error; });
      server.once('close', (code, signal) => { result = { code, signal }; resolve(); });
    });
  } finally {
    await handle.close();
  }
  emit('launch', { surface: url, pid: server.pid, freshState: true });
  const deadline = Date.now() + 150_000;
  let ready;
  while (Date.now() < deadline && !result && !launchError) {
    ready = await probe('/readyz').catch(() => undefined);
    if (ready?.status === 200 && ready.body.ready === true) break;
    await delay(350);
  }
  assert.ifError(launchError);
  assert.ok(!result, 'Gateway exited before readiness');
  assert.equal(ready?.status, 200, 'Gateway never became ready');
  assert.equal(ready.body.ready, true);
  let live;
  while (Date.now() < deadline && !result) {
    live = await probe('/healthz').catch(() => undefined);
    if (live?.status === 200 && live.body.ok === true) break;
    await delay(350);
  }
  assert.equal(live?.status, 200, 'Gateway never responded to the liveness probe');
  assert.equal(live.body.ok, true);
  emit('doctor', { readiness: ready, liveness: live });

  const { stdout } = await promisify(execFile)(process.execPath,
    [...entry, 'gateway', 'health', '--url', ws, '--token', token, '--timeout', '30000', '--json'],
    { cwd: '/app', env, timeout: 45000, killSignal: 'SIGKILL', maxBuffer: 512 * 1024 });
  const health = JSON.parse(stdout);
  assert.equal(health.ok, true, 'CLI did not return a healthy Gateway');
  assert.ok(!result, 'Gateway exited during the CLI check');
  emit('evidence', { command: ['gateway', 'health', '--url', ws, '--timeout', '30000', '--json'], health });
  passed = true;
} finally {
  try {
    if (server) {
      if (!result) {
        server.kill('SIGTERM');
        const timer = setTimeout(() => server.kill('SIGKILL'), 30000);
        try { await closed; } finally { clearTimeout(timer); }
      }
      if (passed) {
        assert.deepEqual(result, { code: 0, signal: null }, 'Gateway did not stop gracefully');
        await assert.rejects(fetch(url + '/healthz', { signal: AbortSignal.timeout(1000) }),
          error => error.cause?.code === 'ECONNREFUSED');
      }
    }
    // Logs survive temp state deletion via the outer Docker runner.
    const contents = await readFile(log, 'utf8').catch(error => {
      if (error.code !== 'ENOENT') throw error;
      return '';
    });
    emit('gateway-log', { tail: contents.slice(-4000) });
  } finally {
    await rm(root, { recursive: true, force: true });
    emit('cleanup', { exit: result ?? null, scratchRemoved: true });
  }
}
console.log('VERIFY_OPENCLAW_GATEWAY_HEALTH_PASS');
