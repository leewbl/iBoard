import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { mkdir, mkdtemp, open, readFile, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { promisify } from 'node:util';

const origin = 'http://127.0.0.1:18789';
const socketOrigin = 'ws://127.0.0.1:18789';
const token = 'iboard-disposable-local-ui-token';
const rename = process.argv[2] === 'session-rename';
const fixtureKey = 'agent:main:iboard-browser-verification';
const originalLabel = 'iBoard verification fixture';
const finalLabel = 'iBoard verification persisted';
const screenshot = process.env.WORKBENCH_SCREENSHOT_PATH;
assert.ok(screenshot, 'Use the sandbox screenshot output');
const expectedCommit = (await readFile('/workspace/expected-commit.txt', 'utf8')).trim();
assert.match(expectedCommit, /^[0-9a-f]{40}$/u);
const root = await mkdtemp('/tmp/iboard-control-ui-');
const home = join(root, 'home');
const state = join(root, 'state');
const config = join(state, 'openclaw.json');
const log = join(root, 'gateway.log');
let browser, child, closed, exit, launchError, passed = false;

const emit = (phase, fields) => console.log(JSON.stringify({ phase, ...fields }));
const refused = error => error.cause?.code === 'ECONNREFUSED';
const health = path => fetch(origin + path, { signal: AbortSignal.timeout(4500), redirect: 'error' });
const nodeSource = ['--import', '/app/node_modules/tsx/dist/loader.mjs'];
const fixtureEnv = {
  PATH: process.env.PATH, HOME: home, TMPDIR: root, XDG_CACHE_HOME: join(root, 'cache'),
  OPENCLAW_HOME: home, OPENCLAW_STATE_DIR: state, OPENCLAW_CONFIG_PATH: config,
  OPENCLAW_DISABLE_BONJOUR: '1',
  OPENCLAW_SKIP_CHANNELS: '1', OPENCLAW_SKIP_PROVIDERS: '1', OPENCLAW_SKIP_GMAIL_WATCHER: '1',
  OPENCLAW_SKIP_CRON: '1', OPENCLAW_SKIP_BROWSER_CONTROL_SERVER: '1', OPENCLAW_SKIP_CANVAS_HOST: '1',
};

async function launchGateway() {
  await mkdir(home);
  await mkdir(state);
  await writeFile(config, JSON.stringify({
    plugins: { enabled: false },
    cron: { enabled: false },
    gateway: {
      mode: 'local',
      auth: { mode: 'token', token },
      controlUi: { enabled: true, root: '/app/dist/control-ui' },
    },
  }), { mode: 0o600 });
  await assert.rejects(health('/healthz'), refused);
  const stream = await open(log, 'w', 0o600);
  try {
    child = spawn(process.execPath, [
      ...nodeSource, '/app/src/entry.ts',
      'gateway', '--port', '18789', '--bind', 'loopback',
    ], {
      cwd: '/app',
      env: fixtureEnv,
      stdio: ['ignore', stream.fd, stream.fd],
    });
    child.once('error', error => { launchError = error; });
    closed = new Promise(resolve => child.once('close', (code, signal) => {
      exit = { code, signal };
      resolve();
    }));
  } finally {
    await stream.close();
  }
  const deadline = performance.now() + 150_000;
  while (performance.now() < deadline && !exit && !launchError) {
    const ready = await health('/readyz').then(async response => response.ok && (await response.json()).ready === true)
      .catch(() => false);
    if (ready) {
      const live = await health('/healthz');
      assert.ok(live.ok && (await live.json()).ok === true, 'Gateway liveness did not pass');
      emit('doctor', { origin, ready: true, live: true });
      return;
    }
    await sleep(350);
  }
  assert.ifError(launchError);
  throw new Error('Gateway did not become ready');
}

async function observe(read, message) {
  const deadline = performance.now() + 30_000;
  while (performance.now() < deadline && !exit) {
    const value = read();
    if (value) return value;
    await sleep(100);
  }
  throw new Error(message);
}

async function seedSession() {
  // Seed through the installed Gateway client. The mutation under test comes from Chromium.
  const program = `
    import { callGateway } from '/app/src/gateway/call.ts';
    const [url, key, label] = process.argv.slice(1);
    const fixture = await callGateway({ url, token: process.env.OPENCLAW_GATEWAY_TOKEN,
      method: 'sessions.create', params: { agentId: 'main', key, label },
      scopes: ['operator.admin', 'operator.read', 'operator.write'], timeoutMs: 45_000 });
    console.log(JSON.stringify({ key: fixture.key, sessionId: fixture.sessionId }));`;
  const proc = await promisify(execFile)(process.execPath, [
    ...nodeSource, '--input-type=module', '-e', program, socketOrigin, fixtureKey, originalLabel,
  ], { cwd: '/app', env: { ...fixtureEnv, OPENCLAW_GATEWAY_TOKEN: token }, timeout: 65_000 });
  const fixture = JSON.parse(proc.stdout.trim().split('\n').at(-1));
  assert.equal(fixture.key, fixtureKey);
  assert.ok(typeof fixture.sessionId === 'string' && fixture.sessionId, 'Seeded session has no identity');
  return fixture;
}

async function openSessions() {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 }, colorScheme: 'light', reducedMotion: 'reduce',
    locale: 'en-US', timezoneId: 'UTC', serviceWorkers: 'block', acceptDownloads: false,
  });
  let outside;
  context.on('request', request => {
    if (request.redirectedFrom() && new URL(request.url()).origin !== origin) outside = true;
  });
  await context.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === origin || (!request.isNavigationRequest() && ['data:', 'blob:'].includes(url.protocol))) {
      await route.continue();
    } else {
      outside = true;
      await route.abort('blockedbyclient');
    }
  });
  await context.routeWebSocket('**/*', async route => {
    if (new URL(route.url()).origin === socketOrigin) route.connectToServer();
    else {
      outside = true;
      await route.close();
    }
  });
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);
  const writes = [];
  let sessionProof;
  page.on('websocket', socket => {
    const pending = new Map();
    let handshake;
    const parse = payload => {
      try { return JSON.parse(payload.toString()); } catch { return null; }
    };
    socket.on('framesent', ({ payload }) => {
      const frame = parse(payload);
      if (frame?.type !== 'req') return;
      if (frame.method === 'connect' || (frame.method === 'sessions.list' && frame.params?.source === 'sessions-page')) {
        pending.set(frame.id, { method: frame.method });
      } else if (rename && handshake && frame.method === 'sessions.patch' && frame.params?.key === fixtureKey) {
        const write = { method: frame.method, params: frame.params, socket, accepted: false };
        pending.set(frame.id, write);
        writes.push(write);
      }
    });
    socket.on('framereceived', ({ payload }) => {
      const frame = parse(payload);
      if (frame?.type !== 'res') return;
      const request = pending.get(frame.id);
      pending.delete(frame.id);
      if (request?.method === 'sessions.patch') request.accepted = frame.ok === true;
      if (!request || frame.ok !== true) return;
      if (request.method === 'connect' && frame.payload?.type === 'hello-ok') {
        const identity = { protocol: frame.payload.protocol, role: frame.payload.auth?.role,
          method: frame.payload.auth?.method };
        if (identity.method === 'token' && identity.role === 'operator') handshake = identity;
      }
      if (handshake && request.method === 'sessions.list' && Array.isArray(frame.payload?.sessions)) {
        sessionProof = { socket, handshake, sessions: frame.payload.sessions };
      }
    });
  });
  assert.equal((await page.goto(origin + '/sessions', { waitUntil: 'domcontentloaded', timeout: 60_000 }))?.status(), 200);
  const servedCommit = await page.evaluate(() => globalThis.OPENCLAW_CONTROL_UI_BUILD_INFO?.commit);
  assert.equal(servedCommit, expectedCommit, 'Served UI bundle does not identify the verified commit');
  await page.getByLabel('Gateway secret', { exact: true }).fill(token);
  await page.getByRole('button', { name: 'Connect', exact: true }).click();
  const proof = await observe(() => sessionProof && !sessionProof.socket.isClosed() && sessionProof,
    'The connected browser did not receive the Gateway session list');
  assert.ok(Number.isInteger(proof.handshake.protocol) && proof.handshake.protocol > 0);
  await page.locator('openclaw-sessions-page .sessions-table').waitFor({ state: 'visible' });
  const assertConnectedPage = () => {
    const url = new URL(page.url());
    assert.equal(url.origin, origin);
    assert.equal(url.pathname, '/sessions');
    assert.ok(!outside && !exit && !proof.socket.isClosed(), 'The UI left the isolated Gateway');
  };
  return { context, page, proof, servedCommit, writes, assertConnectedPage };
}

async function capture(view, evidence = {}) {
  view.assertConnectedPage();
  await view.page.screenshot({ path: screenshot, fullPage: false, animations: 'disabled', caret: 'hide' });
  view.assertConnectedPage();
  emit('ui', { route: '/sessions', servedCommit: view.servedCommit, handshake: view.proof.handshake,
    readback: { method: 'sessions.list', count: view.proof.sessions.length }, screenshot: true, ...evidence });
}

async function verifyRename(fixture, view) {
  function sessionRow(current, label) {
    const found = current.proof.sessions.find(entry => entry.key === fixture.key);
    assert.equal(found?.sessionId, fixture.sessionId, 'Gateway did not list the seeded session');
    assert.equal(found.label, label, 'Gateway session label does not match the expected value');
    return current.page.locator('.sessions-table tbody tr').filter({
      has: current.page.getByRole('checkbox', { name: `Select session: ${fixture.key}`, exact: true }),
    });
  }
  async function renameDialog(current, row) {
    await row.click({ button: 'right' });
    await current.page.getByRole('menuitem', { name: 'Rename…', exact: true }).click();
    const dialog = current.page.getByRole('dialog', { name: 'Rename session', exact: true });
    assert.equal(await dialog.getByRole('textbox').inputValue(), originalLabel);
    return dialog;
  }
  const row = sessionRow(view, originalLabel);
  await row.getByText(originalLabel, { exact: true }).waitFor();
  let dialog = await renameDialog(view, row);
  await dialog.getByRole('textbox').fill('iBoard canceled fixture edit');
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await row.getByText(originalLabel, { exact: true }).waitFor();
  assert.equal(view.writes.length, 0, 'Cancel sent a session mutation');

  dialog = await renameDialog(view, row);
  await dialog.getByRole('textbox').fill(finalLabel);
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  const write = await observe(() => view.writes.find(entry => entry.accepted), 'Gateway did not accept browser rename');
  assert.equal(view.writes.length, 1, 'Expected one session mutation for save');
  assert.equal(write.params.expectedSessionId, fixture.sessionId);
  assert.equal(write.params.label, finalLabel);
  assert.equal(write.socket, view.proof.socket);
  await dialog.waitFor({ state: 'hidden' });
  await row.getByText(finalLabel, { exact: true }).waitFor();
  view.assertConnectedPage();
  await view.context.close();

  const fresh = await openSessions();
  assert.notEqual(fresh.proof.socket, write.socket);
  await sessionRow(fresh, finalLabel).getByText(finalLabel, { exact: true }).waitFor();
  await capture(fresh, { mutation: { method: 'sessions.patch', key: fixture.key, sessionId: fixture.sessionId,
    originalLabel, finalLabel, canceledWithoutWrite: true, accepted: true, freshContextReadback: true } });
}

async function drive() {
  const fixture = rename ? await seedSession() : null;
  const requireFromImage = createRequire('/app/package.json');
  const { chromium } = requireFromImage('/app/node_modules/playwright');
  browser = await chromium.launch({
    headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || chromium.executablePath(),
  });
  const view = await openSessions();
  if (fixture) {
    await verifyRename(fixture, view);
  } else {
    assert.equal(view.proof.sessions.length, 0, 'Fresh Gateway state unexpectedly contains sessions');
    await view.page.getByText('No active sessions.', { exact: true }).waitFor({ state: 'visible' });
    await capture(view);
  }
}

try {
  await launchGateway();
  await drive();
  passed = true;
} finally {
  try {
    await browser?.close();
  } finally {
    try {
      if (child && !exit) {
        child.kill('SIGTERM');
        const alarm = setTimeout(() => child.kill('SIGKILL'), 20_000);
        try { await closed; } finally { clearTimeout(alarm); }
      }
      if (passed) {
        assert.deepEqual(exit, { code: 0, signal: null }, 'Gateway did not shut down gracefully');
        await assert.rejects(health('/healthz'), refused);
      } else {
        const contents = await readFile(log, 'utf8').catch(() => '');
        process.stderr.write(contents.slice(-3500).replaceAll(token, '[fixture token]'));
      }
    } finally {
      await rm(root, { recursive: true, force: true });
      emit('cleanup', { exit, scratchRemoved: true });
    }
  }
}

console.log(rename ? 'VERIFY_OPENCLAW_CONTROL_UI_SESSION_RENAME_PASS' : 'VERIFY_OPENCLAW_CONTROL_UI_CONNECTED_PASS');
