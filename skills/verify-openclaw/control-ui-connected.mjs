import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, open, readFile, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { promisify } from 'node:util';

const origin = 'http://127.0.0.1:18789';
const socketOrigin = 'ws://127.0.0.1:18789';
const token = 'iboard-disposable-local-ui-token';
const journey = process.argv[2] ?? 'connected';
assert.ok(['connected', 'session-rename', 'session-delete', 'session-status-filter'].includes(journey),
  'Unknown Control UI journey');
const rename = journey === 'session-rename';
const deleting = journey === 'session-delete';
const filtering = journey === 'session-status-filter';
const fixtureKey = 'agent:main:iboard-browser-verification';
const originalLabel = 'iBoard verification fixture';
const finalLabel = 'iBoard verification persisted';
const survivorKey = 'agent:main:iboard-browser-survivor';
const survivorLabel = 'iBoard unselected fixture';
const archivedKey = 'agent:main:iboard-browser-archived';
const archivedLabel = 'iBoard archived fixture';
const sessionReadMethods = new Set([
  'sessions.list', 'sessions.subscribe', 'sessions.groups.list', 'sessions.groups.defaults',
  'sessions.describe', 'sessions.resolve', 'sessions.usage', 'sessions.catalog.list', 'sessions.catalog.read',
  'sessions.processes.list',
  // These calls manage a connection's subscriptions or presence, never the stored sessions.
  'sessions.observer.visibility', 'sessions.viewers.set', 'sessions.messages.subscribe', 'sessions.messages.unsubscribe',
]);
const screenshot = process.env.WORKBENCH_SCREENSHOT_PATH;
assert.ok(screenshot, 'Use the sandbox screenshot output');
const expectedCommit = (await readFile('/workspace/expected-commit.txt', 'utf8')).trim();
assert.match(expectedCommit, /^[0-9a-f]{40}$/u);
const recording = process.env.WORKBENCH_RECORDING_PATH;
const viewport = { width: 1280, height: 800 };
const recordingWidth = 960;
const recordingHeight = 600;
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

class BrowserRecording {
  constructor(path) {
    this.path = path;
    this.lines = [JSON.stringify({ schema: 1, producer: 'chromium-cdp', served_commit: expectedCommit, journey })];
    this.bytes = Buffer.byteLength(this.lines[0]) + 1;
    this.frames = 0;
    this.pages = 0;
    this.lastMs = -1;
    this.lastJpeg = '';
    this.different = new Set();
  }

  retain(data, track, force = false) {
    const ms = Math.floor(performance.now() - this.started);
    if (!force && (ms - track.lastMs < 250 || data === this.lastJpeg)) return;
    const jpeg = Buffer.from(data, 'base64');
    assert.ok(jpeg.length >= 4 && jpeg.length <= 150_000 && jpeg.toString('base64') === data &&
      jpeg[0] === 0xff && jpeg[1] === 0xd8 && jpeg.at(-2) === 0xff && jpeg.at(-1) === 0xd9,
    'Chromium returned an invalid or oversized recording frame');
    const time = Math.max(ms, this.lastMs + 1);
    assert.ok(this.frames < 240 && time <= 600_000, 'Browser recording reached its frame or time bound');
    const line = JSON.stringify({ ms: time, page: track.page, jpeg: data });
    this.bytes += Buffer.byteLength(line) + 1;
    assert.ok(this.bytes + 32 <= 24_000_000, 'Browser recording reached its size bound');
    this.lines.push(line);
    this.frames += 1;
    this.lastMs = time;
    this.lastJpeg = data;
    track.lastMs = time;
    if (this.different.size < 3) this.different.add(data);
  }

  async start(page) {
    assert.ok(!this.track && this.pages < 32, 'Finish the current recording page before opening another');
    this.started ??= performance.now();
    const session = await page.context().newCDPSession(page);
    const track = { page: ++this.pages, session, lastMs: -Infinity };
    this.track = track;
    track.listener = ({ data, sessionId }) => {
      void session.send('Page.screencastFrameAck', { sessionId }).catch(error => {
        if (this.track === track && !track.stopping) this.error ??= error;
      });
      if (track.stopping || this.error) return;
      try { this.retain(data, track); } catch (error) { this.error ??= error; }
    };
    session.on('Page.screencastFrame', track.listener);
    await session.send('Page.startScreencast', {
      format: 'jpeg', quality: 60, maxWidth: recordingWidth, maxHeight: recordingHeight, everyNthFrame: 2,
    });
  }

  async checkpoint() {
    if (this.error) throw this.error;
    const track = this.track;
    const { data } = await track.session.send('Page.captureScreenshot', {
      format: 'jpeg', quality: 60, captureBeyondViewport: false,
      clip: { x: 0, y: 0, ...viewport, scale: recordingWidth / viewport.width },
    });
    this.retain(data, track, true);
  }

  async stop({ finalFrame = false } = {}) {
    const track = this.track;
    if (!track) return;
    track.stopping = true;
    try {
      await track.session.send('Page.stopScreencast');
      if (finalFrame) await this.checkpoint();
    } finally {
      this.track = undefined;
      track.session.off('Page.screencastFrame', track.listener);
      await track.session.detach();
    }
    if (this.error) throw this.error;
  }

  async save() {
    assert.ok(!this.track && this.different.size === 3, 'Browser recording lacks three distinct browser frames');
    const digest = createHash('sha256');
    const handle = await open(this.path, 'wx', 0o600);
    try {
      for (const line of [...this.lines, JSON.stringify({ end: this.frames })]) {
        const content = `${line}\n`;
        await handle.writeFile(content);
        digest.update(content);
      }
    } finally {
      await handle.close();
    }
    return digest.digest('hex');
  }
}

const recorder = recording ? new BrowserRecording(recording) : undefined;

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
    const value = await read();
    if (value) return value;
    await sleep(100);
  }
  throw new Error(message);
}

async function prepareSessions(fixtures = [], archiveKey = null) {
  // Start the Gateway's session-list workers before Chromium's initial parallel reads.
  // Only Chromium performs the mutation being checked.
  const program = `
    import { GatewayClient } from '/app/src/gateway/client.ts';
    const [url, input] = process.argv.slice(1);
    const { seeds, query, archiveKey } = JSON.parse(input);
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
      if (archiveKey !== null) {
        const fixture = fixtures.find(({ key }) => key === archiveKey);
        await client.request('sessions.patch', {
          key: fixture.key, expectedSessionId: fixture.sessionId, archived: true,
        }, { timeoutMs: 45_000 });
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
    socketOrigin, JSON.stringify({ seeds: fixtures, query, archiveKey }),
  ], { cwd: '/app', env: { ...fixtureEnv, OPENCLAW_GATEWAY_TOKEN: token }, timeout: 140_000 });
  const seeded = JSON.parse(proc.stdout.trim().split('\n').at(-1));
  assert.deepEqual(seeded.map(({ key, label }) => [key, label]), fixtures);
  for (const { sessionId } of seeded) assert.ok(typeof sessionId === 'string' && sessionId, 'Fixture has no session ID');
  return seeded;
}

function filterRows(request, payload, cache) {
  const { sessions, rowsRevision, rowsUnchanged, rowsDelta } = payload;
  const previous = cache.get(request.membership);
  let rows = sessions;
  if (rowsUnchanged || rowsDelta) {
    assert.ok(previous && request.ifRowsRevision === `delta:${previous.revision}`,
      'Gateway returned a conditional session list for an unknown query revision');
    if (rowsUnchanged) {
      assert.ok(!rowsDelta && sessions.length === 0, 'Unchanged session list unexpectedly includes rows');
      assert.equal(rowsRevision, previous.revision);
      rows = previous.rows;
    } else {
      assert.equal(rowsDelta.baseRevision, previous.revision);
      const available = [...previous.rows, ...sessions];
      rows = rowsDelta.order.map(index => {
        assert.ok(Number.isInteger(index) && index >= 0 && index < available.length,
          'Gateway returned an incomplete session list delta');
        return available[index];
      });
    }
  }
  if (typeof rowsRevision === 'string' && rowsRevision) {
    cache.set(request.membership, { revision: rowsRevision, rows });
  }
  return rows;
}

async function openSessions() {
  const context = await browser.newContext({
    viewport, colorScheme: 'light', reducedMotion: 'reduce',
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
  await recorder?.start(page);
  page.setDefaultTimeout(30_000);
  const writes = [];
  const filterProofs = [];
  let filterSequence = 0;
  let filterWireError;
  let sessionProof, listAdmissionTimeout;
  page.on('websocket', socket => {
    const pending = new Map();
    const filterCache = new Map();
    let handshake;
    const parse = payload => {
      try { return JSON.parse(payload.toString()); } catch { return null; }
    };
    socket.on('framesent', ({ payload }) => {
      const frame = parse(payload);
      if (frame?.type !== 'req') return;
      if (filtering && typeof frame.method === 'string' && frame.method.startsWith('sessions.') &&
          !sessionReadMethods.has(frame.method)) writes.push({ method: frame.method });
      if (frame.method === 'connect' ||
          (frame.method === 'sessions.list' && frame.params?.source === 'sessions-page'
           && (!deleting || frame.params.archived === 'all'))) {
        const request = { method: frame.method, archived: frame.params?.archived };
        if (filtering && request.method === 'sessions.list') {
          if (!handshake) return;
          const { ifRowsRevision, ...query } = frame.params;
          Object.assign(request, { ifRowsRevision, source: query.source, requestId: frame.id,
            sequence: ++filterSequence, membership: JSON.stringify(query, Object.keys(query).sort()) });
        }
        pending.set(frame.id, request);
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
        if (filtering) {
          try {
            const sessions = filterRows(request, frame.payload, filterCache);
            sessionProof = { socket, handshake, archived: request.archived, sessions, source: request.source,
              requestId: request.requestId, sequence: request.sequence,
              conditional: frame.payload.rowsUnchanged === true || frame.payload.rowsDelta !== undefined };
            filterProofs.push(sessionProof);
          } catch (error) {
            filterWireError ??= error;
          }
          return;
        }
        sessionProof = { socket, handshake, archived: request.archived, sessions: frame.payload.sessions };
      }
    });
  });
  const route = deleting ? '/sessions?status=all' : '/sessions';
  assert.equal((await page.goto(origin + route, { waitUntil: 'domcontentloaded', timeout: 60_000 }))?.status(), 200);
  const servedCommit = await page.evaluate(() => globalThis.OPENCLAW_CONTROL_UI_BUILD_INFO?.commit);
  assert.equal(servedCommit, expectedCommit, 'Served UI bundle does not identify the verified commit');
  await recorder?.checkpoint();
  // Cold Gateway startup can temporarily leave the UI in a pending connection state.
  const loginTimeout = 65_000;
  await page.getByLabel('Gateway secret', { exact: true }).fill(token, { timeout: loginTimeout });
  await recorder?.checkpoint();
  await page.getByRole('button', { name: 'Connect', exact: true }).click({ timeout: loginTimeout });
  const activeProof = () => {
    if (filterWireError) throw filterWireError;
    return sessionProof && !sessionProof.socket.isClosed() && sessionProof;
  };
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
  const latestFilterSequence = () => filterSequence;
  const filterProofAfter = (sequence, archived) => {
    if (filterWireError) throw filterWireError;
    return filterProofs.find(read => read.socket === proof.socket && read.sequence > sequence && read.archived === archived);
  };
  return { context, page, proof, servedCommit, writes, assertConnectedPage, latestFilterSequence, filterProofAfter };
}

async function capture(view, evidence = {}) {
  view.assertConnectedPage();
  await recorder?.stop({ finalFrame: true });
  await view.page.screenshot({ path: screenshot, fullPage: false, animations: 'disabled', caret: 'hide' });
  view.assertConnectedPage();
  const recordingSha256 = await recorder?.save();
  emit('ui', { route: '/sessions', gatewayPid: child.pid, servedCommit: view.servedCommit, handshake: view.proof.handshake,
    readback: { method: 'sessions.list', count: view.proof.sessions.length, archived: view.proof.archived },
    screenshot: true, ...(recorder ? { recording: true, recordingJourney: journey, recordingSha256 } : {}), ...evidence });
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
  await recorder?.stop({ finalFrame: true });
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
  await recorder?.checkpoint();
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await row.getByText(originalLabel, { exact: true }).waitFor();
  assert.equal(view.writes.length, 0, 'Cancel sent a session mutation');
  await recorder?.checkpoint();

  dialog = await renameDialog(view, row);
  await dialog.getByRole('textbox').fill(finalLabel);
  await recorder?.checkpoint();
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
  await recorder?.checkpoint();
  await canceled.getByRole('button', { name: 'Cancel', exact: true }).click();
  await canceled.waitFor({ state: 'hidden' });
  canceledView.assertConnectedPage();
  await recorder?.stop({ finalFrame: true });
  await canceledView.context.close();
  assert.equal(canceledView.writes.length, 0, 'Cancel sent a session deletion');

  const view = await openSessions();
  const afterCancelCount = view.proof.sessions.length;
  assert.equal(afterCancelCount, 2, 'Cancel changed the Gateway session list');
  await assertSession(view, target);
  await assertSession(view, survivor);
  view.assertConnectedPage();

  const dialog = await confirmation(view);
  await recorder?.checkpoint();
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

async function verifySessionStatusFilter(active, archived, view) {
  const fixtureIdentity = ({ key, sessionId }) => ({ key, sessionId });
  const fixtures = { active: fixtureIdentity(active), archived: fixtureIdentity(archived) };
  assert.notEqual(active.sessionId, archived.sessionId);
  const steps = [];
  const filters = view.page.getByRole('button', { name: 'Filters', exact: true });
  const states = view.page.getByRole('radiogroup', { name: 'Session state', exact: true });
  const refresh = view.page.getByRole('button', { name: 'Refresh', exact: true })
    .and(view.page.locator('.sessions-toolbar__refresh'));
  const prefix = 'Select session: ';
  const rowSelectors = view.page.locator(`.sessions-table tbody input[aria-label^="${prefix}"]:visible`);
  const expectRows = (state, expected) => observe(async () => {
    const keys = await rowSelectors.evaluateAll((nodes, length) =>
      nodes.map(node => node.getAttribute('aria-label').slice(length)).sort(), prefix.length);
    return JSON.stringify(keys) === JSON.stringify(expected) ? keys : null;
  }, `The ${state} Sessions filter did not render exactly the expected fixtures`);

  async function record(state, scope, trigger, proof, expected) {
    view.assertConnectedPage();
    assert.equal(new URL(view.page.url()).searchParams.get('status'), state === 'active' ? null : state);
    assert.equal(proof.socket, view.proof.socket);
    assert.equal(proof.source, 'sessions-page');
    assert.equal(proof.archived, scope);
    assert.ok(typeof proof.requestId === 'string' && proof.requestId &&
      !steps.some(step => step.requestId === proof.requestId), 'Expected a distinct authenticated list request');
    if (trigger !== 'filter-refresh') assert.equal(proof.conditional, false, 'Expected a fresh roster response');
    const gateway = proof.sessions.map(fixtureIdentity).sort((left, right) => left.key.localeCompare(right.key));
    const expectedRows = expected.map(fixtureIdentity).sort((left, right) => left.key.localeCompare(right.key));
    assert.deepEqual(gateway, expectedRows, 'Gateway listed different session identities for the selected scope');
    const visibleKeys = await expectRows(state, expectedRows.map(row => row.key));
    assert.equal(view.writes.length, 0,
      `The Sessions filters sent unexpected session RPCs: ${view.writes.map(({ method }) => method).join(', ')}`);
    steps.push({ state, trigger, requestId: proof.requestId, source: proof.source, archived: proof.archived,
      gateway, visibleKeys });
    await recorder?.checkpoint();
  }

  await record('active', undefined, 'load', view.proof, [active]);
  let finalProof;
  for (const [state, label, scope, expected] of [
    ['archived', 'Archived', true, [archived]],
    ['all', 'All', 'all', [active, archived]],
    ['active', 'Active', undefined, [active]],
  ]) {
    if (!(await states.isVisible())) await filters.click();
    let sequence = view.latestFilterSequence();
    await states.getByRole('radio', { name: label, exact: true }).click();
    if (state === 'active') {
      // The first Active visit populated the UI cache. Prove the click switched
      // the view and use the real Refresh button to obtain a new Gateway read.
      await expectRows(state, [active.key]);
      assert.equal(new URL(view.page.url()).searchParams.get('status'), null);
      if (await states.isVisible()) await filters.click();
      sequence = view.latestFilterSequence();
      await refresh.click();
    }
    finalProof = await observe(() => view.filterProofAfter(sequence, scope),
      `The ${state} Sessions filter did not receive a fresh authenticated list reply`, 90_000);
    await record(state, scope, state === 'active' ? 'filter-refresh' : 'filter', finalProof, expected);
  }
  await capture({ ...view, proof: finalProof }, { filter: { fixtures, steps, sessionMutations: view.writes.length } });
  assert.equal(view.writes.length, 0, 'The Sessions filters sent a late session mutation');
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
    : filtering ? [[fixtureKey, originalLabel], [archivedKey, archivedLabel]]
      : rename ? [[fixtureKey, originalLabel]] : [];
  const fixtures = seeds.length ? await prepareSessions(seeds, filtering ? archivedKey : null) : [];
  await launchBrowser();
  const view = await openSessions();
  if (rename) {
    await verifyRename(fixtures[0], view);
  } else if (deleting) {
    await verifyDelete(...fixtures, view);
  } else if (filtering) {
    await verifySessionStatusFilter(...fixtures, view);
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
    try { await recorder?.stop(); } finally { await browser?.close(); }
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

console.log(`VERIFY_OPENCLAW_CONTROL_UI_${journey.replaceAll('-', '_').toUpperCase()}_PASS`);
