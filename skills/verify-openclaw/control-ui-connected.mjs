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
const journey = process.argv[2] ?? 'connected';
assert.ok(['connected', 'session-rename', 'session-delete'].includes(journey), 'Unknown Control UI journey');
const rename = journey === 'session-rename';
const deleting = journey === 'session-delete';
const fixtureKey = 'agent:main:iboard-browser-verification';
const originalLabel = 'iBoard verification fixture';
const finalLabel = 'iBoard verification persisted';
const survivorKey = 'agent:main:iboard-browser-survivor';
const survivorLabel = 'iBoard unselected fixture';
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

async function prepareFixture() {
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
  }), { mode: 0o600, flag: 'wx' });
}

async function launchGateway() {
  await assert.rejects(health('/healthz'), refused);
  exit = undefined;
  launchError = undefined;
  const stream = await open(log, 'a', 0o600);
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
      return { origin, ready: true, live: true, gatewayPid: child.pid };
    }
    await sleep(350);
  }
  assert.ifError(launchError);
  throw new Error('Gateway did not become ready');
}

async function stopGateway() {
  if (!child || exit) return;
  child.kill('SIGTERM');
  const alarm = setTimeout(() => child.kill('SIGKILL'), 20_000);
  try { await closed; } finally { clearTimeout(alarm); }
}

async function assertGatewayStopped() {
  assert.deepEqual(exit, { code: 0, signal: null }, 'Gateway did not shut down gracefully');
  await assert.rejects(health('/healthz'), refused);
}

async function observe(read, message, timeout = 30_000) {
  const deadline = performance.now() + timeout;
  while (performance.now() < deadline && !exit) {
    const value = read();
    if (value) return value;
    await sleep(100);
  }
  throw new Error(message);
}

async function prepareSessions(fixtures = []) {
  // Start the Gateway's session-list workers before Chromium's initial parallel reads.
  // Only Chromium performs the mutation being checked.
  const program = `
    import { GatewayClient } from '/app/src/gateway/client.ts';
    const [url, input] = process.argv.slice(1);
    const { seeds, query } = JSON.parse(input);
    let connect, fail;
    const connected = new Promise((resolve, reject) => { connect = resolve; fail = reject; });
    const client = new GatewayClient({ url, token: process.env.OPENCLAW_GATEWAY_TOKEN,
      clientName: 'cli', mode: 'cli', role: 'operator', caps: ['ultrafast'],
      scopes: ['operator.admin', 'operator.read', 'operator.write'],
      onHelloOk: connect, onConnectError: fail,
      onClose: code => fail(new Error('Fixture connection closed: ' + code)) });
    const timer = setTimeout(() => fail(new Error('Fixture gateway handshake timed out')), 45_000);
    try {
      client.start();
      await connected;
      const fixtures = [];
      for (const [key, label] of seeds) {
        const result = await client.request('sessions.create', { agentId: 'main', key, label }, { timeoutMs: 45_000 });
        fixtures.push({ key: result.key, sessionId: result.sessionId, label });
      }
      await client.request('sessions.list', query, { timeoutMs: 75_000 });
      console.log(JSON.stringify(fixtures));
    } finally {
      clearTimeout(timer);
      await client.stopAndWait();
    }`;
  const query = { source: 'sessions-page', rowMode: 'compact', agentId: 'main', configuredAgentsOnly: true,
    includeGlobal: true, includeUnknown: true, excludeDock: true, limit: 50,
    ...(deleting ? { archived: 'all' } : {}) };
  const proc = await promisify(execFile)(process.execPath, [
    '--max-old-space-size=256', ...nodeSource, '--input-type=module', '-e', program,
    socketOrigin, JSON.stringify({ seeds: fixtures, query }),
  ], { cwd: '/app', env: { ...fixtureEnv, OPENCLAW_GATEWAY_TOKEN: token }, timeout: 140_000 });
  const seeded = JSON.parse(proc.stdout.trim().split('\n').at(-1));
  assert.deepEqual(seeded.map(({ key, label }) => [key, label]), fixtures);
  for (const { sessionId } of seeded) assert.ok(typeof sessionId === 'string' && sessionId, 'Fixture has no session ID');
  return seeded;
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
  let sessionProof, listAdmissionTimeout;
  page.on('websocket', socket => {
    const pending = new Map();
    let handshake;
    const parse = payload => {
      try { return JSON.parse(payload.toString()); } catch { return null; }
    };
    socket.on('framesent', ({ payload }) => {
      const frame = parse(payload);
      if (frame?.type !== 'req') return;
      if (frame.method === 'connect' ||
          (frame.method === 'sessions.list' && frame.params?.source === 'sessions-page'
           && (!deleting || frame.params.archived === 'all'))) {
        pending.set(frame.id, { method: frame.method, archived: frame.params?.archived });
      } else if (handshake && (
        (rename && frame.method === 'sessions.patch' && frame.params?.key === fixtureKey)
        || (deleting && ['sessions.delete', 'sessions.deleteMany'].includes(frame.method))
      )) {
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
      if (request && 'accepted' in request) {
        request.accepted = frame.ok === true;
        request.deleted = frame.payload?.deleted === true;
        request.responseKey = frame.payload?.key;
      }
      if (handshake && request?.method === 'sessions.list' && frame.ok === false &&
          frame.error?.code === 'UNAVAILABLE' &&
          frame.error.message?.includes('The server could not start the request within 30 seconds.')) {
        listAdmissionTimeout = socket;
      }
      if (!request || frame.ok !== true) return;
      if (request.method === 'connect' && frame.payload?.type === 'hello-ok') {
        const identity = { protocol: frame.payload.protocol, role: frame.payload.auth?.role,
          method: frame.payload.auth?.method };
        if (identity.method === 'token' && identity.role === 'operator') handshake = identity;
      }
      if (handshake && request.method === 'sessions.list' && Array.isArray(frame.payload?.sessions)) {
        sessionProof = { socket, handshake, archived: request.archived, sessions: frame.payload.sessions };
      }
    });
  });
  const route = deleting ? '/sessions?status=all' : '/sessions';
  assert.equal((await page.goto(origin + route, { waitUntil: 'domcontentloaded', timeout: 60_000 }))?.status(), 200);
  const servedCommit = await page.evaluate(() => globalThis.OPENCLAW_CONTROL_UI_BUILD_INFO?.commit);
  assert.equal(servedCommit, expectedCommit, 'Served UI bundle does not identify the verified commit');
  await page.getByLabel('Gateway secret', { exact: true }).fill(token);
  await page.getByRole('button', { name: 'Connect', exact: true }).click();
  const activeProof = () => sessionProof && !sessionProof.socket.isClosed() && sessionProof;
  const missingList = 'The connected browser did not receive the Gateway session list';
  const listResponseTimeout = 110_000;
  await observe(() => activeProof() || (listAdmissionTimeout && !listAdmissionTimeout.isClosed()),
    missingList, listResponseTimeout);
  if (!activeProof()) {
    // Retry only the observed read admission timeout, before any scenario action.
    const refresh = page.getByRole('button', { name: 'Refresh', exact: true })
      .and(page.locator('.sessions-toolbar__refresh'));
    await refresh.click({ timeout: 65_000 });
    process.stderr.write('Retried the Sessions list after a Gateway admission timeout\n');
  }
  const proof = await observe(activeProof, missingList, listResponseTimeout);
  assert.ok(Number.isInteger(proof.handshake.protocol) && proof.handshake.protocol > 0);
  await page.locator('openclaw-sessions-page .sessions-table').waitFor({ state: 'visible', timeout: 65_000 });
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
  emit('ui', { route: '/sessions', gatewayPid: child.pid, servedCommit: view.servedCommit, handshake: view.proof.handshake,
    readback: { method: 'sessions.list', count: view.proof.sessions.length, archived: view.proof.archived },
    screenshot: true, ...evidence });
}

function rowFor(view, key) {
  return view.page.locator('.sessions-table tbody tr').filter({
    has: view.page.getByRole('checkbox', { name: `Select session: ${key}`, exact: true }),
  });
}

async function assertSession(view, fixture, label = fixture.label) {
  const found = view.proof.sessions.find(entry => entry.key === fixture.key);
  assert.equal(found?.sessionId, fixture.sessionId, 'Gateway did not list the expected fixture identity');
  assert.equal(found.label, label, 'Gateway fixture label does not match');
  await rowFor(view, fixture.key).getByText(label, { exact: true }).waitFor({ state: 'visible' });
}

async function restartSessions() {
  // Keep only the disposable on-disk state while replacing both application processes.
  await browser.close();
  browser = undefined;
  const previousPid = child.pid;
  await stopGateway();
  await assertGatewayStopped();
  const previousExit = exit;
  const restarted = await launchGateway();
  assert.notEqual(restarted.gatewayPid, previousPid, 'Gateway process was not replaced');
  emit('restart', { ...restarted, previousPid, previousExit, portClosed: true });
  await prepareSessions();
  await launchBrowser();
  return openSessions();
}

async function verifyRename(fixture, view) {
  async function renameDialog(current, row) {
    await row.click({ button: 'right' });
    await current.page.getByRole('menuitem', { name: 'Rename…', exact: true }).click();
    const dialog = current.page.getByRole('dialog', { name: 'Rename session', exact: true });
    assert.equal(await dialog.getByRole('textbox').inputValue(), originalLabel);
    return dialog;
  }
  const row = rowFor(view, fixture.key);
  await assertSession(view, fixture);
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

  const fresh = await restartSessions();
  assert.notEqual(fresh.proof.socket, write.socket);
  await assertSession(fresh, fixture, finalLabel);
  await capture(fresh, { mutation: { method: 'sessions.patch', key: fixture.key, sessionId: fixture.sessionId,
    originalLabel, finalLabel, canceledWithoutWrite: true, accepted: true, freshContextReadback: true } });
}

async function verifyDelete(target, survivor, canceledView) {
  async function confirmation(view) {
    await rowFor(view, target.key).getByRole('checkbox').check();
    await view.page.locator('.data-table-bulk-bar').getByRole('button', { name: 'Delete', exact: true }).click();
    const dialog = view.page.locator('openclaw-modal-dialog dialog[open]').filter({ hasText: 'Delete 1 session?' });
    await dialog.waitFor({ state: 'visible' });
    return dialog;
  }

  assert.equal(canceledView.proof.sessions.length, 2);
  assert.notEqual(target.sessionId, survivor.sessionId);
  await assertSession(canceledView, target);
  await assertSession(canceledView, survivor);
  const canceled = await confirmation(canceledView);
  await canceled.getByRole('button', { name: 'Cancel', exact: true }).click();
  await canceled.waitFor({ state: 'hidden' });
  canceledView.assertConnectedPage();
  await canceledView.context.close();
  assert.equal(canceledView.writes.length, 0, 'Cancel sent a session deletion');

  const view = await openSessions();
  const afterCancelCount = view.proof.sessions.length;
  assert.equal(afterCancelCount, 2, 'Cancel changed the Gateway session list');
  await assertSession(view, target);
  await assertSession(view, survivor);
  view.assertConnectedPage();

  const dialog = await confirmation(view);
  await dialog.getByRole('button', { name: 'Delete', exact: true }).click();
  const write = await observe(() => view.writes.find(entry => entry.accepted && entry.deleted),
    'Gateway did not accept the selected session deletion', 90_000);
  assert.equal(view.writes.length, 1, 'Deleting one selection issued more than one request');
  assert.equal(write.method, 'sessions.delete');
  assert.equal(write.params.key, target.key);
  assert.equal(write.responseKey, target.key);
  assert.equal(write.params.expectedSessionId, target.sessionId);
  assert.equal(write.socket, view.proof.socket);
  await dialog.waitFor({ state: 'hidden' });
  await rowFor(view, target.key).waitFor({ state: 'hidden' });
  await rowFor(view, survivor.key).getByText(survivor.label, { exact: true }).waitFor({ state: 'visible' });
  view.assertConnectedPage();

  const fresh = await restartSessions();
  assert.notEqual(fresh.proof.socket, write.socket);
  assert.equal(fresh.proof.sessions.length, 1, 'Deletion did not persist for exactly one session');
  assert.ok(!fresh.proof.sessions.some(entry => entry.key === target.key || entry.sessionId === target.sessionId),
    'Deleted session reappeared after the Gateway restarted');
  await rowFor(fresh, target.key).waitFor({ state: 'hidden' });
  await assertSession(fresh, survivor);
  await capture(fresh, { mutation: { method: write.method, key: target.key, sessionId: target.sessionId,
    expectedSessionId: write.params.expectedSessionId, responseKey: write.responseKey, requestCount: view.writes.length,
    canceledWithoutWrite: true, afterCancelCount, deleted: write.deleted,
    freshContextReadback: true, targetAbsent: true, survivor: { key: survivor.key, sessionId: survivor.sessionId } } });
}

async function launchBrowser() {
  const requireFromImage = createRequire('/app/package.json');
  const { chromium } = requireFromImage('/app/node_modules/playwright');
  browser = await chromium.launch({
    headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || chromium.executablePath(),
  });
}

async function drive() {
  const seeds = deleting ? [[fixtureKey, originalLabel], [survivorKey, survivorLabel]]
    : rename ? [[fixtureKey, originalLabel]] : [];
  const fixtures = seeds.length ? await prepareSessions(seeds) : [];
  await launchBrowser();
  const view = await openSessions();
  if (rename) {
    await verifyRename(fixtures[0], view);
  } else if (deleting) {
    await verifyDelete(...fixtures, view);
  } else {
    assert.equal(view.proof.sessions.length, 0, 'Fresh Gateway state unexpectedly contains sessions');
    await view.page.getByText('No active sessions.', { exact: true }).waitFor({ state: 'visible' });
    await capture(view);
  }
}

try {
  await prepareFixture();
  emit('doctor', await launchGateway());
  await drive();
  passed = true;
} finally {
  try {
    await browser?.close();
  } finally {
    try {
      await stopGateway();
      if (passed) {
        await assertGatewayStopped();
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

console.log(`VERIFY_OPENCLAW_CONTROL_UI_${journey.replace('-', '_').toUpperCase()}_PASS`);
