import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { mkdir, mkdtemp, open, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { promisify } from 'node:util';

const origin = 'http://127.0.0.1:18789';
const socketOrigin = 'ws://127.0.0.1:18789';
const token = 'iboard-disposable-local-ui-token';
const [journey = 'connected', ...extra] = process.argv.slice(2);
assert.ok(['connected', 'session-rename', 'session-delete', 'session-status-filter', 'session-archive-restore',
  'session-archive-subtree-undo', 'session-sidebar-archive-automations', 'session-archive-running'].includes(journey),
  'Unknown Control UI journey');
const rename = journey === 'session-rename';
const deleting = journey === 'session-delete';
const filtering = journey === 'session-status-filter';
const archiving = journey === 'session-archive-restore';
const archiveTree = journey === 'session-archive-subtree-undo';
const archiveSidebar = journey === 'session-sidebar-archive-automations';
const archiveRunning = journey === 'session-archive-running';
const archiveBatch = archiveTree || archiveSidebar;
const tracksArchiveWrites = archiving || archiveBatch || archiveRunning;
const tracksListReads = filtering || archiveBatch || archiveRunning;
const defaultStatus = deleting || archiveBatch || archiveRunning ? 'all' : undefined;
const visualState = extra[1];
assert.ok(!extra.length || (
  extra.length === 2 && extra[0] === '--visual-state' &&
  ((journey === 'connected' && visualState === 'empty') ||
    (filtering && ['active', 'archived', 'all'].includes(visualState)))
), 'Unknown Control UI visual state for this journey');
const fixtureKey = 'agent:main:iboard-browser-verification';
const originalLabel = 'iBoard verification fixture';
const finalLabel = 'iBoard verification persisted';
const survivorKey = 'agent:main:iboard-browser-survivor';
const survivorLabel = 'iBoard unselected fixture';
const archivedKey = 'agent:main:iboard-browser-archived';
const archivedLabel = 'iBoard archived fixture';
const nestedKey = 'agent:main:iboard-browser-nested';
const nestedLabel = 'iBoard nested fixture';
const secondKey = 'agent:main:iboard-browser-second';
const secondLabel = 'iBoard second selected fixture';
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
// Cold Gateway stalls have delayed both page startup and Chromium input delivery.
const browserActionTimeout = 65_000;
const browserNavigationTimeout = 90_000;
const recordingWidth = 960;
const recordingHeight = 600;
const root = await mkdtemp('/tmp/iboard-control-ui-');
const home = join(root, 'home');
const state = join(root, 'state');
const config = join(state, 'openclaw.json');
const log = join(root, 'gateway.log');
const runMarker = 'iboard-archive-running-fixture';
const finalAnswerMarker = 'IBOARD_OFFLINE_FIXTURE_COMPLETED';
let browser, child, closed, exit, launchError, passed = false;
let visualBrowser;
const deleteTrace = deleting ? { step: 'start-gateway' } : null;

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
  // Multiple compiler and Node workers share the sandbox's PID limit with Chromium.
  ...(archiveRunning || archiveSidebar
    ? { GOMAXPROCS: '2', UV_THREADPOOL_SIZE: '2', NODE_OPTIONS: '--v8-pool-size=2' } : {}),
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

async function localRunConfig() {
  const plugin = join(home, 'archive-fixture-plugin');
  const workspace = join(home, 'fixture-workspace');
  const backend = join(plugin, 'backend.mjs');
  const id = 'iboard-archive-cli';
  await mkdir(plugin);
  await mkdir(workspace);
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
        api.registerCliBackend({ id: ${JSON.stringify(id)}, config: ${JSON.stringify({
          command: process.execPath, args: [backend, root], output: 'text', input: 'stdin',
          sessionMode: 'none', serialize: false,
          reliability: { watchdog: { fresh: { minMs: 210_000, maxMs: 210_000 } } },
        })} });
      },
    };`),
    writeFile(backend, `
      import { existsSync, writeFileSync } from 'node:fs';
      import { join } from 'node:path';
      import { setTimeout as delay } from 'node:timers/promises';
      const state = process.argv[2];
      let prompt = '';
      for await (const chunk of process.stdin) prompt += chunk;
      if (!prompt.includes(${JSON.stringify(runMarker)})) throw new Error('Unexpected fixture input');
      for (const signal of ['SIGTERM', 'SIGINT']) {
        process.once(signal, () => {
          writeFileSync(join(state, 'run-stopped.json'), JSON.stringify({ pid: process.pid, signal }));
          process.exit(signal === 'SIGTERM' ? 143 : 130);
        });
      }
      writeFileSync(join(state, 'run-started.json'), JSON.stringify({ pid: process.pid }));
      const deadline = Date.now() + 200000;
      while (!existsSync(join(state, 'run-release'))) {
        if (Date.now() >= deadline) throw new Error('Offline fixture exceeded its hold time');
        await delay(50);
      }
      writeFileSync(join(state, 'run-completed'), '');
      process.stdout.write(${JSON.stringify(finalAnswerMarker + '\n')});
    `),
  ]);
  return {
    agents: { defaults: { workspace, model: id + '/proof', timeoutSeconds: 240 }, entries: { main: {} } },
    plugins: { allow: [id], load: { paths: [plugin] }, entries: { [id]: { enabled: true } }, slots: { memory: 'none' } },
  };
}

async function prepareFixture() {
  await mkdir(home);
  await mkdir(state);
  await writeFile(config, JSON.stringify({
    ...(archiveRunning ? await localRunConfig() : { plugins: { enabled: false } }),
    cron: { enabled: false },
    ...(deleting || tracksArchiveWrites ? { update: { checkOnStart: false } } : {}),
    gateway: {
      mode: 'local',
      auth: { mode: 'token', token },
      controlUi: { enabled: true, root: '/app/dist/control-ui', ...(visualState ? { communityInvite: false } : {}) },
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
      // The archived fixture loads multiple Gateway workers before Chromium.
      // Smaller young generations leave room for both in the same container.
      env: filtering || tracksArchiveWrites ? {
        ...fixtureEnv,
        NODE_OPTIONS: ['--max-semi-space-size=4', fixtureEnv.NODE_OPTIONS].filter(Boolean).join(' '),
      } : fixtureEnv,
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

async function localGateway(body, values, scopes = ['operator.admin', 'operator.read', 'operator.write']) {
  const program = `
    import { GatewayClient } from '/app/src/gateway/client.ts';
    import { writeFile } from 'node:fs/promises';
    const [url, input] = process.argv.slice(1);
    const { values, scopes } = JSON.parse(input);
    let connect, fail;
    const connected = new Promise((resolve, reject) => { connect = resolve; fail = reject; });
    const terminal = Promise.withResolvers();
    const client = new GatewayClient({ url, token: process.env.OPENCLAW_GATEWAY_TOKEN,
      clientName: 'cli', mode: 'cli', role: 'operator', caps: ['ultrafast'],
      scopes,
      onHelloOk: connect, onConnectError: fail,
      onEvent: ({ event, payload }) => {
        if (values.watchRun && event === 'chat' && payload?.runId === values.watchRun
            && payload.sessionKey === values.key && ['aborted', 'final', 'error'].includes(payload.state)) {
          const { runId, sessionKey, state, stopReason } = payload;
          terminal.resolve({ runId, sessionKey, state, stopReason });
        }
      },
      onClose: code => fail(new Error('Fixture connection closed: ' + code)) });
    const timer = setTimeout(() => fail(new Error('Fixture gateway handshake timed out')), 45_000);
    try {
      client.start();
      await connected;
      ${body}
    } finally {
      clearTimeout(timer);
      await client.stopAndWait();
    }`;
  const proc = await promisify(execFile)(process.execPath, [
    '--max-old-space-size=256', ...nodeSource, '--input-type=module', '-e', program,
    socketOrigin, JSON.stringify({ values, scopes }),
  ], { cwd: '/app', env: { ...fixtureEnv, OPENCLAW_GATEWAY_TOKEN: token }, timeout: 140_000 });
  return JSON.parse(proc.stdout.trim().split('\n').at(-1));
}

async function prepareSessions(fixtures = [], archiveKey = null, status = defaultStatus,
  { jobKeys = [], inspectJobs = [] } = {}) {
  // Start the Gateway's session-list workers before Chromium's initial parallel reads.
  // Only Chromium performs the mutation being checked.
  const program = `
      const { seeds, query, archiveKey, warmUpdateStatus, jobKeys, inspectJobs } = values;
      const fixtures = [];
      for (const [key, label, parentSessionKey] of seeds) {
        const result = await client.request('sessions.create', { agentId: 'main', key, label,
          ...(parentSessionKey ? { parentSessionKey } : {}) }, { timeoutMs: 45_000 });
        fixtures.push({ key: result.key, sessionId: result.sessionId, label });
      }
      for (const key of jobKeys) {
        const fixture = fixtures.find(row => row.key === key);
        const name = fixture.label + ' automation';
        const job = await client.request('cron.add', {
          agentId: 'main', name, enabled: true, sessionTarget: 'main', sessionKey: key,
          schedule: { kind: 'at', at: new Date(Date.now() + 7 * 86400_000).toISOString() },
          wakeMode: 'next-heartbeat',
          payload: { kind: 'systemEvent', text: 'Disposable automation fixture. This job must never run.' },
        }, { timeoutMs: 45_000 });
        fixture.job = { id: job.id, name };
      }
      if (archiveKey !== null) {
        const fixture = fixtures.find(({ key }) => key === archiveKey);
        await client.request('sessions.patch', {
          key: fixture.key, expectedSessionId: fixture.sessionId, archived: true,
        }, { timeoutMs: 45_000 });
      }
      if (!inspectJobs.length) await client.request('sessions.list', query, { timeoutMs: 75_000 });
      if (!inspectJobs.length && warmUpdateStatus) {
        // Admin Control UI connections also request update.status. Load its
        // reconciliation dependencies before introducing the browser process.
        await client.request('update.status', {}, { timeoutMs: 75_000 });
      }
      const jobs = [];
      for (const id of inspectJobs) {
        const job = await client.request('cron.get', { id }, { timeoutMs: 45_000 });
        jobs.push({ id: job.id, name: job.name, sessionKey: job.sessionKey, sessionTarget: job.sessionTarget,
          enabled: job.enabled, neverRun: job.state?.lastRunAtMs === undefined });
      }
      console.log(JSON.stringify(inspectJobs.length ? jobs : fixtures));
    `;
  const query = { source: 'sessions-page', rowMode: 'compact', agentId: 'main', configuredAgentsOnly: true,
    includeGlobal: true, includeUnknown: true, excludeDock: true, limit: 50,
    ...(status ? { archived: status === 'archived' ? true : status } : {}) };
  const seeded = await localGateway(program, { seeds: fixtures, query, archiveKey, jobKeys, inspectJobs,
    warmUpdateStatus: deleting || tracksArchiveWrites }, inspectJobs.length ? ['operator.read'] : undefined);
  if (inspectJobs.length) return seeded;
  assert.deepEqual(seeded.map(({ key, label }) => [key, label]), fixtures.map(([key, label]) => [key, label]));
  for (const { sessionId } of seeded) assert.ok(typeof sessionId === 'string' && sessionId, 'Fixture has no session ID');
  return seeded;
}

function fixtureRequest(method, params, scopes, timeoutMs = 45_000) {
  return localGateway(`
    const { method, params, timeoutMs } = values;
    console.log(JSON.stringify(await client.request(method, params, { timeoutMs })));
  `, { method, params, timeoutMs }, scopes);
}

function watchArchiveEvent(key, runId) {
  return localGateway(`
    const subscription = await client.request('sessions.messages.subscribe',
      { key: values.key, agentId: 'main' }, { timeoutMs: 35_000 });
    if (subscription?.subscribed !== true || subscription.key !== values.key) {
      throw new Error('Unable to observe the expected session');
    }
    await writeFile(values.readyPath, JSON.stringify({ runId: values.watchRun }), { flag: 'wx' });
    const timeout = new Promise((_, reject) => {
      setTimeout(() => reject(new Error('No terminal chat event for the offline worker')), 85_000).unref();
    });
    console.log(JSON.stringify(await Promise.race([terminal.promise, timeout])));
  `, { key, watchRun: runId, readyPath: join(root, 'run-listener.json') }, ['operator.read']);
}

function expandSessionRows(sessions, sharedFacts) {
  return sessions.map(row => {
    const { sharedFacts: indexes, ...fields } = row;
    if (indexes === undefined) return row;
    assert.ok(Array.isArray(indexes) && Array.isArray(sharedFacts), 'Invalid Gateway session row facts');
    const expanded = {};
    for (const index of indexes) {
      const fact = sharedFacts[index];
      assert.ok(Number.isInteger(index) && index >= 0 && index < sharedFacts.length &&
        fact && typeof fact === 'object' && !Array.isArray(fact), 'Unknown Gateway session row fact');
      Object.assign(expanded, fact);
    }
    return { ...expanded, ...fields };
  });
}

function filterRows(request, payload, cache) {
  const { sessions, sharedFacts, rowsRevision, rowsUnchanged, rowsDelta } = payload;
  const previous = cache.get(request.membership);
  // Resolve shared fields before caching rows or applying the delta order.
  let rows = expandSessionRows(sessions, sharedFacts);
  if (rowsUnchanged || rowsDelta) {
    assert.ok(previous && request.ifRowsRevision === `delta:${previous.revision}`,
      'Gateway returned a conditional session list for an unknown query revision');
    if (rowsUnchanged) {
      assert.ok(!rowsDelta && sessions.length === 0, 'Unchanged session list unexpectedly includes rows');
      assert.equal(rowsRevision, previous.revision);
      rows = previous.rows;
    } else {
      assert.equal(rowsDelta.baseRevision, previous.revision);
      const available = [...previous.rows, ...rows];
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

async function openSessions(status = defaultStatus) {
  const scope = status === 'archived' ? true : status;
  const context = await browser.newContext({
    viewport, colorScheme: 'light', reducedMotion: 'reduce',
    locale: 'en-US', timezoneId: 'UTC', serviceWorkers: 'block', acceptDownloads: false,
    ...(visualState ? { deviceScaleFactor: 1 } : {}),
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
  page.setDefaultTimeout(browserActionTimeout);
  page.setDefaultNavigationTimeout(browserNavigationTimeout);
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
           && (!(deleting || tracksArchiveWrites) || frame.params.archived === scope))) {
        const request = { method: frame.method, archived: frame.params?.archived, requestId: frame.id };
        if (tracksListReads && request.method === 'sessions.list') {
          if (!handshake) return;
          const { ifRowsRevision, ...query } = frame.params;
          Object.assign(request, { ifRowsRevision, source: query.source, requestId: frame.id,
            sequence: ++filterSequence, membership: JSON.stringify(query, Object.keys(query).sort()) });
        }
        pending.set(frame.id, request);
      } else if (handshake && (
        (rename && frame.method === 'sessions.patch' && frame.params?.key === fixtureKey)
        || (deleting && ['sessions.delete', 'sessions.deleteMany'].includes(frame.method))
        || (tracksArchiveWrites && typeof frame.method === 'string' && frame.method.startsWith('sessions.') &&
          !sessionReadMethods.has(frame.method))
        || (archiveSidebar && ['cron.add', 'cron.update', 'cron.remove', 'cron.run', 'cron.bind'].includes(frame.method))
      )) {
        const write = { method: frame.method, params: frame.params, socket, accepted: false,
          requestId: frame.id, startedAt: performance.now() };
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
        request.received = true;
        request.accepted = frame.ok === true;
        request.deleted = frame.payload?.deleted === true;
        request.responseKey = frame.payload?.key;
        request.responseSessionId = frame.payload?.entry?.sessionId;
        request.responseArchived = typeof frame.payload?.entry?.archivedAt === 'number';
        if (archiveBatch && Array.isArray(frame.payload?.outcomes)) {
          request.outcomes = frame.payload.outcomes.map(({ key, agentId, ok }) => ({ key, agentId, ok }));
        }
        if (typeof frame.error?.code === 'string' && /^[A-Z_]{1,64}$/u.test(frame.error.code)) {
          request.errorCode = frame.error.code;
        }
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
        if (tracksListReads) {
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
        sessionProof = { socket, handshake, archived: request.archived, sessions: frame.payload.sessions,
          requestId: request.requestId };
      }
    });
  });
  const route = status ? '/sessions?status=' + status : '/sessions';
  assert.equal((await page.goto(origin + route, { waitUntil: 'domcontentloaded' }))?.status(), 200);
  const servedCommit = await page.evaluate(() => globalThis.OPENCLAW_CONTROL_UI_BUILD_INFO?.commit);
  assert.equal(servedCommit, expectedCommit, 'Served UI bundle does not identify the verified commit');
  await recorder?.checkpoint();
  await page.getByLabel('Gateway secret', { exact: true }).fill(token);
  await recorder?.checkpoint();
  await page.getByRole('button', { name: 'Connect', exact: true }).click();
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
    await refresh.click();
    process.stderr.write('Retried the Sessions list after a Gateway admission timeout\n');
  }
  const proof = await observe(activeProof, missingList, listResponseTimeout);
  assert.ok(Number.isInteger(proof.handshake.protocol) && proof.handshake.protocol > 0);
  if (visualState) {
    const presentation = await page.request.get(origin + '/control-ui-config.json', {
      headers: { Authorization: `Bearer ${token}` }, maxRedirects: 0,
    });
    assert.ok(presentation.ok(), 'The visual fixture could not read authenticated Gateway presentation settings');
    assert.equal((await presentation.json()).communityInvite, false, 'The visual fixture requires the pinned invitation setting');
  }
  await page.locator('openclaw-sessions-page .sessions-table').waitFor({ state: 'visible' });
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
  return { page, proof, servedCommit, writes, assertConnectedPage, latestFilterSequence, filterProofAfter };
}

async function captureVisual(view, requestId, assertState) {
  view.assertConnectedPage();
  await view.page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all([...document.images].filter(image => image.currentSrc && image.loading !== 'lazy')
      .map(image => image.decode()));
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  await assertState();
  const png = await view.page.screenshot({ fullPage: false, animations: 'disabled', caret: 'hide', scale: 'css' });
  await assertState();
  view.assertConnectedPage();
  return { png, receipt: { state: visualState, viewport, ...visualBrowser,
    screenshot_sha256: createHash('sha256').update(png).digest('hex'), request_id: requestId } };
}

async function capture(view, evidence = {}, selectedVisual) {
  view.assertConnectedPage();
  await recorder?.stop({ finalFrame: true });
  const visual = visualState === 'empty' ? await captureVisual(view, null, async () => {
    assert.equal(new URL(view.page.url()).searchParams.get('status'), null);
    assert.ok(await view.page.getByText('No active sessions.', { exact: true }).isVisible());
  }) : selectedVisual;
  if (visualState) {
    assert.ok(visual, 'The selected authenticated Sessions state was not captured');
    await writeFile(screenshot, visual.png, { flag: 'wx', mode: 0o600 });
  } else {
    await view.page.screenshot({ path: screenshot, fullPage: false, animations: 'disabled', caret: 'hide' });
  }
  view.assertConnectedPage();
  const recordingSha256 = await recorder?.save();
  emit('ui', { route: '/sessions', gatewayPid: child.pid, servedCommit: view.servedCommit, handshake: view.proof.handshake,
    readback: { method: 'sessions.list', count: view.proof.sessions.length, archived: view.proof.archived },
    screenshot: true, ...(recorder ? { recording: true, recordingJourney: journey, recordingSha256 } : {}),
    ...(visual ? { visual: visual.receipt } : {}), ...evidence });
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

async function restartSessions(status = defaultStatus) {
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
  await prepareSessions([], null, status);
  await launchBrowser();
  return openSessions(status);
}

async function awaitArchivePatch(view, target, archived) {
  const write = await observe(() => view.writes.find(item => item.received),
    'Gateway did not answer the archive patch', 90_000);
  assert.equal(view.writes.length, 1, 'Expected one browser mutation for the selected row');
  assert.equal(write.socket, view.proof.socket);
  assert.equal(write.method, 'sessions.patch');
  assert.equal(write.params.key, target.key);
  assert.equal(write.params.expectedSessionId, target.sessionId);
  assert.equal(write.params.archived, archived);
  assert.equal(write.accepted, true, 'Gateway rejected archive or restore');
  assert.equal(write.responseKey, target.key);
  assert.equal(write.responseSessionId, target.sessionId);
  assert.equal(write.responseArchived, archived);
  return { method: write.method, requestId: write.requestId, key: write.params.key,
    expectedSessionId: write.params.expectedSessionId, archived, accepted: write.accepted,
    responseSessionId: write.responseSessionId, responseKey: write.responseKey,
    responseArchived: write.responseArchived };
}

async function verifyArchiveRestore(target, survivor, active) {
  const steps = [];
  async function roster(view, state, expected) {
    view.assertConnectedPage();
    const archived = state === 'archived';
    assert.equal(new URL(view.page.url()).searchParams.get('status'), archived ? 'archived' : null);
    assert.equal(view.proof.archived, archived ? true : undefined);
    assert.ok(typeof view.proof.requestId === 'string' && view.proof.requestId, 'Missing authenticated list request ID');
    assert.equal(view.writes.length, 0, 'Opening a fresh Sessions page sent a write');
    const row = view.proof.sessions.find(entry => entry.key === target.key);
    assert.equal(row?.archived === true, archived);
    assert.deepEqual(view.proof.sessions.map(entry => entry.key).sort(), expected.map(entry => entry.key).sort());
    for (const fixture of expected) await assertSession(view, fixture);
    if (archived) assert.equal(await rowFor(view, survivor.key).count(), 0, 'Unselected session was archived');
    steps.push({ state, gatewayPid: child.pid, requestId: view.proof.requestId,
      rows: expected.map(({ key, sessionId }) => ({ key, sessionId })) });
    await recorder?.checkpoint();
  }

  async function change(view, archived) {
    const row = rowFor(view, target.key);
    await row.getByRole('button', { name: `Open session menu: ${target.key}`, exact: true }).click();
    const action = archived ? 'Archive session' : 'Restore session';
    await view.page.getByRole('menuitem', { name: action, exact: true }).click();
    const write = await awaitArchivePatch(view, target, archived);
    await row.waitFor({ state: 'hidden' });
    await recorder?.checkpoint();
    return write;
  }

  await roster(active, 'active', [target, survivor]);
  const archive = await change(active, true);
  const archived = await restartSessions('archived');
  await roster(archived, 'archived', [target]);
  const restore = await change(archived, false);
  const restored = await restartSessions();
  await roster(restored, 'active', [target, survivor]);
  await capture(restored, { mutation: { key: target.key, sessionId: target.sessionId,
    writes: [archive, restore], steps, survivor: { key: survivor.key, sessionId: survivor.sessionId } } });
}

async function freshSessionsRead(view) {
  const sequence = view.latestFilterSequence();
  // A keyboard click preserves the pointer position when an Undo toast is hovered.
  await view.page.getByRole('button', { name: 'Refresh', exact: true })
    .and(view.page.locator('.sessions-toolbar__refresh')).press('Enter');
  return observe(() => view.filterProofAfter(sequence, 'all'),
    'Sessions did not return a fresh authenticated roster', 90_000);
}

async function verifyArchiveRunning(target, survivor, view) {
  const fixtures = [target, survivor];
  const steps = [];
  const readIds = new Set();
  const readonly = ['operator.read'];

  async function marker(name) {
    const raw = await readFile(join(root, name), 'utf8').catch(error => {
      if (error.code !== 'ENOENT') throw error;
      return null;
    });
    return raw && JSON.parse(raw);
  }

  async function roster(browserView, proof, stage, targetArchived) {
    browserView.assertConnectedPage();
    assert.equal(proof.socket, browserView.proof.socket);
    assert.equal(proof.source, 'sessions-page');
    assert.equal(proof.archived, 'all');
    assert.ok(typeof proof.requestId === 'string' && proof.requestId && !readIds.has(proof.requestId));
    readIds.add(proof.requestId);
    assert.deepEqual(proof.sessions.map(row => row.key).sort(), fixtures.map(row => row.key).sort());
    const rows = [];
    for (const fixture of fixtures) {
      const expected = targetArchived && fixture === target;
      const row = proof.sessions.find(entry => entry.key === fixture.key);
      assert.equal(row?.sessionId, fixture.sessionId);
      assert.equal(row.archived === true, expected);
      const visible = rowFor(browserView, fixture.key);
      await visible.getByText(fixture.label, { exact: true }).waitFor({ state: 'visible' });
      await observe(() => visible.evaluate(element => element.classList.contains('session-data-row--archived'))
        .then(value => value === expected), 'UI and Gateway differ on archive state');
      rows.push({ key: row.key, sessionId: row.sessionId, archived: expected });
    }
    steps.push({ stage, gatewayPid: child.pid, requestId: proof.requestId, rows });
    await recorder?.checkpoint();
  }

  async function describe() {
    const { session } = await fixtureRequest('sessions.describe', { key: target.key, agentId: 'main' }, readonly);
    assert.equal(session?.sessionId, target.sessionId);
    return session;
  }

  async function dialog() {
    await rowFor(view, target.key).getByRole('button', { name: `Open session menu: ${target.key}`, exact: true }).click();
    await view.page.getByRole('menuitem', { name: 'Archive session', exact: true }).click();
    const modal = view.page.locator('openclaw-modal-dialog dialog[open]')
      .filter({ hasText: 'Active work in this session will be stopped.' });
    await modal.getByRole('button', { name: 'Archive session', exact: true }).waitFor({ state: 'visible' });
    return modal;
  }

  const idempotencyKey = randomUUID();
  const started = await fixtureRequest('chat.send', {
    sessionKey: target.key, message: runMarker, idempotencyKey, deliver: false,
  }, ['operator.write']);
  assert.equal(started.status, 'started');
  assert.equal(started.runId, idempotencyKey);
  const { pid } = await observe(() => marker('run-started.json'), 'Local worker never started', 70_000)
    .catch(async error => {
      const outcome = await fixtureRequest('agent.wait', { runId: idempotencyKey, timeoutMs: 0 },
        ['operator.write'], 10_000).catch(() => null);
      if (outcome) process.stderr.write(JSON.stringify({ phase: 'worker-start-diagnostic', outcome }) + '\n');
      throw error;
    });
  assert.ok(Number.isInteger(pid) && pid > 1 && pid !== child.pid && pid !== process.pid);
  assert.equal(process.kill(pid, 0), true);
  assert.equal((await describe()).hasActiveRun, true);
  await roster(view, await freshSessionsRead(view), 'initial', false);
  const cancel = await dialog();
  await recorder?.checkpoint();
  await cancel.getByRole('button', { name: 'Cancel', exact: true }).click();
  await cancel.waitFor({ state: 'hidden' });
  await roster(view, await freshSessionsRead(view), 'after-cancel', false);
  assert.equal(view.writes.length, 0, 'Cancel sent a session write');
  assert.equal(process.kill(pid, 0), true, 'Cancel stopped the worker');
  assert.equal(await marker('run-stopped.json'), null);
  assert.equal((await describe()).hasActiveRun, true);

  const confirm = await dialog();
  const archiveResult = watchArchiveEvent(target.key, idempotencyKey);
  void archiveResult.catch(() => {});
  await observe(async () => (await marker('run-listener.json'))?.runId === idempotencyKey,
    'The archive event listener did not subscribe to the running session', 50_000);
  await confirm.getByRole('button', { name: 'Archive session', exact: true }).click();
  const write = await awaitArchivePatch(view, target, true);
  const stop = await observe(() => marker('run-stopped.json'), 'Archive did not signal the local worker', 30_000);
  assert.equal(stop.pid, pid);
  assert.ok(['SIGTERM', 'SIGINT'].includes(stop.signal));
  await observe(() => {
    try { process.kill(pid, 0); } catch (error) {
      if (error.code === 'ESRCH') return true;
      throw error;
    }
    return false;
  }, 'Archived worker did not exit');
  const archiveEvent = await archiveResult;
  assert.deepEqual(archiveEvent, { runId: idempotencyKey, sessionKey: target.key,
    state: 'aborted', stopReason: 'archive' });
  const outcome = await fixtureRequest('agent.wait', { runId: idempotencyKey, timeoutMs: 5_000 }, ['operator.write']);
  assert.equal(outcome.runId, idempotencyKey);
  assert.equal(outcome.status, 'error');
  assert.ok(['archive', 'aborted'].includes(outcome.stopReason));
  const wait = { runId: outcome.runId, status: outcome.status, stopReason: outcome.stopReason };
  assert.notEqual((await describe()).hasActiveRun, true, 'Archived session still has active work');
  await recorder?.checkpoint();
  const fresh = await restartSessions();
  await roster(fresh, fresh.proof, 'after-archive-restart', true);
  const history = await fixtureRequest('chat.history', { sessionKey: target.key, limit: 20 }, readonly);
  assert.ok(Array.isArray(history.messages));
  assert.ok(JSON.stringify(history.messages).includes(runMarker), 'The restored transcript is missing the submitted work');
  assert.ok(!JSON.stringify(history.messages).includes(finalAnswerMarker), 'An archived worker emitted a final answer');
  await assert.rejects(readFile(join(root, 'run-completed')), { code: 'ENOENT' });
  assert.equal(view.writes.length, 1);
  assert.equal(fresh.writes.length, 0);
  await capture(fresh, { mutation: { key: target.key, sessionId: target.sessionId,
    survivor: { key: survivor.key, sessionId: survivor.sessionId }, canceledWithoutWrite: true, write, steps,
    run: { id: idempotencyKey, childPid: pid, activeBefore: true, activeAfterCancel: true,
      archiveEvent, wait, childExitSignal: stop.signal, childExited: true, noCompletion: true },
  } });
}

async function verifyArchiveBatchUndo(first, second, survivor, view) {
  const fixtures = [first, second, survivor];
  const targetIdentity = ({ key, sessionId }) => ({ key, expectedSessionId: sessionId, agentId: 'main' });
  const targets = [first, second].map(targetIdentity);
  const steps = [];
  const selections = [];
  const automationReads = [];
  const readIds = new Set();
  const sidebarRow = (browserView, key) =>
    browserView.page.locator(`.sidebar-recent-session[data-session-key="${key}"]`);

  async function roster(browserView, proof, stage, archived) {
    browserView.assertConnectedPage();
    assert.equal(proof.socket, browserView.proof.socket);
    assert.equal(proof.archived, 'all');
    assert.equal(proof.source, 'sessions-page');
    assert.ok(typeof proof.requestId === 'string' && proof.requestId && !readIds.has(proof.requestId));
    readIds.add(proof.requestId);
    assert.deepEqual(proof.sessions.map(row => row.key).sort(), fixtures.map(row => row.key).sort());
    const rows = [];
    for (const fixture of fixtures) {
      const expected = archived && fixture !== survivor;
      const row = proof.sessions.find(entry => entry.key === fixture.key);
      assert.equal(row?.sessionId, fixture.sessionId);
      assert.equal(row.archived === true, expected, `${stage}: unexpected Gateway archive state for ${fixture.key}`);
      const visible = rowFor(browserView, fixture.key);
      await visible.getByText(fixture.label, { exact: true }).waitFor({ state: 'visible' });
      await observe(() => visible.evaluate(element => element.classList.contains('session-data-row--archived'))
        .then(value => value === expected), `Visible archive state did not match Gateway for ${fixture.key}`);
      if (archiveSidebar) await sidebarRow(browserView, fixture.key).waitFor({ state: expected ? 'hidden' : 'visible' });
      rows.push({ key: row.key, sessionId: row.sessionId, archived: expected });
    }
    steps.push({ stage, gatewayPid: child.pid, requestId: proof.requestId, rows });
    await recorder?.checkpoint();
  }

  async function checkAutomations(stage, targetEnabled) {
    if (!archiveSidebar) return;
    const jobs = await prepareSessions([], null, 'all', { inspectJobs: [first.job.id, survivor.job.id] });
    assert.deepEqual(jobs, [first, survivor].map((fixture, index) => ({
      ...fixture.job, sessionKey: fixture.key, sessionTarget: 'main',
      enabled: index === 0 ? targetEnabled : true, neverRun: true,
    })), `${stage}: automation state or binding changed unexpectedly`);
    automationReads.push({ stage, gatewayPid: child.pid, jobs });
  }

  async function freshRead(stage, flags) {
    await roster(view, await freshSessionsRead(view), stage, flags);
  }

  async function dialog() {
    if (archiveSidebar) {
      const selected = view.page.locator('.sidebar-recent-session--selected');
      for (const { key } of targets) {
        const row = sidebarRow(view, key);
        await row.waitFor({ state: 'visible' });
        if (!await row.evaluate(element => element.classList.contains('sidebar-recent-session--selected'))) {
          await row.locator('.sidebar-recent-session__link').click({ modifiers: ['Alt'] });
        }
      }
      const keys = await selected.evaluateAll(rows => rows.map(row => row.getAttribute('data-session-key')).sort());
      assert.deepEqual(keys, targets.map(({ key }) => key).sort(), 'Sidebar did not select exactly the two intended sessions');
      selections.push(keys);
      await sidebarRow(view, first.key).locator('.sidebar-recent-session__link').click({ button: 'right' });
      await view.page.getByRole('menuitem', { name: 'Archive 2', exact: true }).click();
      const modal = view.page.locator('openclaw-modal-dialog dialog[open]').filter({ hasText: 'Pause automations?' });
      await modal.getByText(`${first.job.name} (${first.job.id})`, { exact: true }).waitFor({ state: 'visible' });
      assert.equal(await modal.getByText(survivor.job.name, { exact: false }).count(), 0,
        'The warning listed an unselected automation');
      await modal.getByRole('button', { name: 'Archive anyway', exact: true }).waitFor({ state: 'visible' });
      return modal;
    }
    await rowFor(view, first.key).getByRole('button', { name: `Open session menu: ${first.key}`, exact: true }).click();
    await view.page.getByRole('menuitem', { name: 'Archive session', exact: true }).click();
    const modal = view.page.locator('openclaw-modal-dialog dialog[open]').filter({ hasText: 'Also archive nested sessions?' });
    await modal.waitFor({ state: 'visible' });
    await modal.getByRole('button', { name: 'Archive all (2)', exact: true }).waitFor({ state: 'visible' });
    return modal;
  }

  async function batch(index, archived) {
    const write = await observe(() => view.writes[index]?.received && view.writes[index],
      `Gateway did not acknowledge the ${archived ? 'archive' : 'Undo'} batch`, 90_000);
    assert.equal(view.writes.length, index + 1, 'Unexpected session mutation count');
    assert.equal(write.socket, view.proof.socket);
    assert.equal(write.method, 'sessions.patchMany');
    assert.equal(write.accepted, true);
    assert.equal(write.params.patch?.archived, archived);
    const actual = write.params.targets.map(({ key, expectedSessionId, agentId }) => ({ key, expectedSessionId, agentId }));
    const byKey = (a, b) => a.key.localeCompare(b.key);
    assert.deepEqual(actual.toSorted(byKey), targets.toSorted(byKey));
    const outcomes = targets.map(({ key, agentId }) => ({ key, agentId, ok: true }));
    assert.deepEqual(write.outcomes?.toSorted(byKey), outcomes.toSorted(byKey));
    return { method: write.method, requestId: write.requestId, archived, accepted: true, targets: actual, outcomes: write.outcomes };
  }

  await roster(view, view.proof, 'initial', false);
  await checkAutomations('initial', true);
  const canceled = await dialog();
  await recorder?.checkpoint();
  await canceled.getByRole('button', { name: 'Cancel', exact: true }).click();
  await canceled.waitFor({ state: 'hidden' });
  await freshRead('after-cancel', false);
  assert.equal(view.writes.length, 0, 'Cancel submitted a session mutation');
  await checkAutomations('after-cancel', true);
  const confirmed = await dialog();
  await confirmed.getByRole('button', { name: archiveSidebar ? 'Archive anyway' : 'Archive all (2)', exact: true }).click();
  const archive = await batch(0, true);
  const undo = view.page.getByRole('status').filter({ hasText: 'Archived 2 sessions' })
    .getByRole('button', { name: 'Undo', exact: true });
  await undo.hover();
  await freshRead('after-archive', true);
  await checkAutomations('after-archive', false);
  await undo.click();
  const restore = await batch(1, false);
  const fresh = await restartSessions();
  await roster(fresh, fresh.proof, 'after-undo-restart', false);
  await checkAutomations('after-undo-restart', false);
  assert.equal(view.writes.length, 2, 'A late mutation followed Undo');
  assert.equal(fresh.writes.length, 0, 'Fresh browser mutated the verified roster');
  await capture(fresh, { mutation: { key: first.key, sessionId: first.sessionId,
    ...(archiveSidebar ? {
      second: { key: second.key, sessionId: second.sessionId }, selections, automationReads,
      warningJobId: first.job.id, sidebarVisibilityChecked: true,
    } : { nested: { key: second.key, sessionId: second.sessionId } }),
    survivor: { key: survivor.key, sessionId: survivor.sessionId },
    writes: [archive, restore], steps, canceledWithoutWrite: true } });
}

async function verifyRename(fixture, view) {
  const row = rowFor(view, fixture.key);
  const menuEntries = [];
  async function renameDialog(entry, openMenu) {
    await openMenu();
    await view.page.getByRole('menuitem', { name: 'Rename…', exact: true }).click();
    const dialog = view.page.getByRole('dialog', { name: 'Rename session', exact: true });
    assert.equal(await dialog.getByRole('textbox').inputValue(), originalLabel);
    menuEntries.push(entry);
    return dialog;
  }
  await assertSession(view, fixture);
  let dialog = await renameDialog('context-menu', () => row.click({ button: 'right' }));
  await dialog.getByRole('textbox').fill('iBoard canceled fixture edit');
  await recorder?.checkpoint();
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await row.getByText(originalLabel, { exact: true }).waitFor();
  assert.equal(view.writes.length, 0, 'Cancel sent a session mutation');
  await recorder?.checkpoint();

  const menuButton = row.getByRole('button', { name: `Open session menu: ${fixture.key}`, exact: true });
  dialog = await renameDialog('row-button', () => menuButton.click());
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
    expectedSessionId: write.params.expectedSessionId, requestCount: view.writes.length,
    originalLabel, finalLabel, menuEntries, canceledWithoutWrite: true, accepted: true, freshContextReadback: true } });
}

async function verifyDelete(target, survivor, canceledView) {
  Object.assign(deleteTrace, { step: 'cancel-confirmation', view: canceledView });
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
  // Release Chromium's renderer and caches before opening the confirmation view.
  // Keep the same Gateway so the cancel readback cannot rely on a restart.
  const cancelBrowser = browser;
  const cancelGatewayPid = child.pid;
  await cancelBrowser.close();
  browser = undefined;
  assert.equal(cancelBrowser.isConnected(), false, 'The cancellation browser still holds a connection');
  assert.equal(canceledView.writes.length, 0, 'Cancel sent a session deletion');

  deleteTrace.step = 'read-after-cancel';
  await launchBrowser();
  assert.notEqual(browser, cancelBrowser);
  const view = await openSessions();
  deleteTrace.view = view;
  assert.equal(child.pid, cancelGatewayPid);
  assert.notEqual(view.proof.socket, canceledView.proof.socket);
  const afterCancelCount = view.proof.sessions.length;
  assert.equal(afterCancelCount, 2, 'Cancel changed the Gateway session list');
  await assertSession(view, target);
  await assertSession(view, survivor);
  view.assertConnectedPage();

  deleteTrace.step = 'confirm-deletion';
  const dialog = await confirmation(view);
  await recorder?.checkpoint();
  await dialog.getByRole('button', { name: 'Delete', exact: true }).click();
  deleteTrace.step = 'await-delete-response';
  const write = await observe(() => view.writes.find(entry => entry.accepted && entry.deleted),
    'Gateway did not accept the selected session deletion', 90_000);
  assert.equal(view.writes.length, 1, 'Deleting one selection issued more than one request');
  assert.equal(write.method, 'sessions.delete');
  assert.equal(write.params.key, target.key);
  assert.equal(write.responseKey, target.key);
  assert.equal(write.params.expectedSessionId, target.sessionId);
  assert.equal(write.socket, view.proof.socket);
  deleteTrace.step = 'read-after-deletion';
  await dialog.waitFor({ state: 'hidden' });
  await rowFor(view, target.key).waitFor({ state: 'hidden' });
  await rowFor(view, survivor.key).getByText(survivor.label, { exact: true }).waitFor({ state: 'visible' });
  view.assertConnectedPage();

  deleteTrace.step = 'restart-readback';
  const fresh = await restartSessions();
  assert.notEqual(fresh.proof.socket, write.socket);
  assert.equal(fresh.proof.sessions.length, 1, 'Deletion did not persist for exactly one session');
  assert.ok(!fresh.proof.sessions.some(entry => entry.key === target.key || entry.sessionId === target.sessionId),
    'Deleted session reappeared after the Gateway restarted');
  await rowFor(fresh, target.key).waitFor({ state: 'hidden' });
  await assertSession(fresh, survivor);
  await capture(fresh, { mutation: { method: write.method, key: target.key, sessionId: target.sessionId,
    expectedSessionId: write.params.expectedSessionId, responseKey: write.responseKey, requestCount: view.writes.length,
    canceledWithoutWrite: true, afterCancelCount, cancelBrowserReplaced: true, cancelGatewayPid, deleted: write.deleted,
    freshContextReadback: true, targetAbsent: true, survivor: { key: survivor.key, sessionId: survivor.sessionId } } });
}

function logDeleteFailure(trace) {
  const writes = (trace.view?.writes ?? []).map(write => ({
    method: write.method,
    id: typeof write.requestId === 'string' && /^[\w:-]{1,120}$/u.test(write.requestId) ? write.requestId : undefined,
    state: !write.received ? 'no-response' : !write.accepted ? 'rejected' : write.deleted ? 'deleted' : 'not-deleted',
    errorCode: write.errorCode, socketClosed: write.socket.isClosed(),
    elapsedMs: Math.round(performance.now() - write.startedAt),
  }));
  process.stderr.write(JSON.stringify({
    phase: 'delete-diagnostic', step: trace.step, gatewayExited: Boolean(exit),
    authenticatedSocketClosed: trace.view?.proof.socket.isClosed(), writes,
  }) + '\n');
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
  const readVisibleKeys = () => rowSelectors.evaluateAll((nodes, length) =>
    nodes.map(node => node.getAttribute('aria-label').slice(length)).sort(), prefix.length);
  const expectRows = (state, expected) => observe(async () => {
    const keys = await readVisibleKeys();
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
  let finalProof, selectedVisual;
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
    if (state === visualState) {
      selectedVisual = await captureVisual(view, finalProof.requestId, async () => {
        assert.equal(new URL(view.page.url()).searchParams.get('status'), state === 'active' ? null : state);
        assert.deepEqual(await readVisibleKeys(), steps.at(-1).visibleKeys, 'Sessions changed during visual capture');
      });
    }
  }
  const evidence = { filter: { fixtures, steps, sessionMutations: view.writes.length } };
  await capture({ ...view, proof: finalProof }, evidence, selectedVisual);
  assert.equal(view.writes.length, 0, 'The Sessions filters sent a late session mutation');
}

async function launchBrowser() {
  const requireFromImage = createRequire('/app/package.json');
  const { chromium } = requireFromImage('/app/node_modules/playwright');
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || chromium.executablePath();
  const binary = visualState ? await realpath(executablePath) : executablePath;
  browser = await chromium.launch({ headless: true, executablePath: binary });
  if (visualState) {
    const digest = createHash('sha256');
    for await (const chunk of createReadStream(binary)) digest.update(chunk);
    const { version: playwright } = requireFromImage('/app/node_modules/playwright/package.json');
    visualBrowser = { browser: browser.version(), playwright, chromium_sha256: digest.digest('hex') };
  }
}

async function drive() {
  if (deleteTrace) deleteTrace.step = 'prepare-fixtures';
  const seeds = archiveSidebar ? [[fixtureKey, originalLabel], [secondKey, secondLabel], [survivorKey, survivorLabel]]
    : archiveTree ? [[fixtureKey, originalLabel], [nestedKey, nestedLabel, fixtureKey], [survivorKey, survivorLabel]]
    : deleting || archiving || archiveRunning ? [[fixtureKey, originalLabel], [survivorKey, survivorLabel]]
    : filtering ? [[fixtureKey, originalLabel], [archivedKey, archivedLabel]]
      : rename ? [[fixtureKey, originalLabel]] : [];
  const fixtures = seeds.length ? await prepareSessions(seeds, filtering ? archivedKey : null, defaultStatus,
    archiveSidebar ? { jobKeys: [fixtureKey, survivorKey] } : {}) : [];
  if (deleteTrace) deleteTrace.step = 'authenticate-browser';
  await launchBrowser();
  const view = await openSessions();
  if (rename) {
    await verifyRename(fixtures[0], view);
  } else if (deleting) {
    await verifyDelete(...fixtures, view);
  } else if (filtering) {
    await verifySessionStatusFilter(...fixtures, view);
  } else if (archiving) {
    await verifyArchiveRestore(...fixtures, view);
  } else if (archiveRunning) {
    await verifyArchiveRunning(...fixtures, view);
  } else if (archiveBatch) {
    await verifyArchiveBatchUndo(...fixtures, view);
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
} catch (error) {
  if (deleteTrace) logDeleteFailure(deleteTrace);
  throw error;
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
