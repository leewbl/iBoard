import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, open, readFile, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

const origin = 'http://127.0.0.1:18789';
const socketOrigin = 'ws://127.0.0.1:18789';
const token = 'iboard-disposable-local-ui-token';
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
      '--import', '/app/node_modules/tsx/dist/loader.mjs', '/app/src/entry.ts',
      'gateway', '--port', '18789', '--bind', 'loopback',
    ], {
      cwd: '/app',
      env: {
        PATH: process.env.PATH, HOME: home, TMPDIR: root, XDG_CACHE_HOME: join(root, 'cache'),
        OPENCLAW_HOME: home, OPENCLAW_STATE_DIR: state, OPENCLAW_CONFIG_PATH: config,
        OPENCLAW_DISABLE_BONJOUR: '1',
        OPENCLAW_SKIP_CHANNELS: '1', OPENCLAW_SKIP_PROVIDERS: '1', OPENCLAW_SKIP_GMAIL_WATCHER: '1',
        OPENCLAW_SKIP_CRON: '1', OPENCLAW_SKIP_BROWSER_CONTROL_SERVER: '1', OPENCLAW_SKIP_CANVAS_HOST: '1',
      },
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

async function drive() {
  const requireFromImage = createRequire('/app/package.json');
  const { chromium } = requireFromImage('/app/node_modules/playwright');
  browser = await chromium.launch({
    headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || chromium.executablePath(),
  });
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
  let sessionProof;
  page.on('websocket', socket => {
    const pending = new Map();
    let handshake;
    const parse = payload => {
      try { return JSON.parse(payload.toString()); } catch { return null; }
    };
    socket.on('framesent', ({ payload }) => {
      const frame = parse(payload);
      if (frame?.type === 'req' && (frame.method === 'connect'
          || (frame.method === 'sessions.list' && frame.params?.source === 'sessions-page'))) {
        pending.set(frame.id, frame.method);
      }
    });
    socket.on('framereceived', ({ payload }) => {
      const frame = parse(payload);
      if (frame?.type !== 'res') return;
      const method = pending.get(frame.id);
      pending.delete(frame.id);
      if (frame.ok !== true) return;
      if (method === 'connect' && frame.payload?.type === 'hello-ok') {
        const identity = { protocol: frame.payload.protocol, role: frame.payload.auth?.role,
          method: frame.payload.auth?.method };
        if (identity.method === 'token' && identity.role === 'operator') handshake = identity;
      }
      if (handshake && method === 'sessions.list' && Array.isArray(frame.payload?.sessions)) {
        sessionProof = { socket, handshake, readback: { method, count: frame.payload.sessions.length } };
      }
    });
  });
  assert.equal((await page.goto(origin + '/sessions', { waitUntil: 'load' }))?.status(), 200);
  const servedCommit = await page.evaluate(() => globalThis.OPENCLAW_CONTROL_UI_BUILD_INFO?.commit);
  assert.equal(servedCommit, expectedCommit, 'Served UI bundle does not identify the verified commit');
  await page.getByLabel('Gateway secret', { exact: true }).fill(token);
  await page.getByRole('button', { name: 'Connect', exact: true }).click();
  const proof = await observe(() => sessionProof && !sessionProof.socket.isClosed() && sessionProof,
    'The connected browser did not receive the Gateway session list');
  assert.ok(Number.isInteger(proof.handshake.protocol) && proof.handshake.protocol > 0);
  await page.locator('openclaw-sessions-page .sessions-table').waitFor({ state: 'visible' });
  assert.equal(proof.readback.count, 0, 'Fresh Gateway state unexpectedly contains sessions');
  await page.getByText('No active sessions.', { exact: true }).waitFor({ state: 'visible' });
  const assertConnectedPage = () => {
    const url = new URL(page.url());
    assert.equal(url.origin, origin);
    assert.equal(url.pathname, '/sessions');
    assert.ok(!outside && !exit && !proof.socket.isClosed(), 'The UI left the isolated Gateway');
  };
  assertConnectedPage();
  await page.screenshot({ path: screenshot, fullPage: false, animations: 'disabled', caret: 'hide' });
  assertConnectedPage();
  emit('ui', { route: '/sessions', servedCommit, handshake: proof.handshake, readback: proof.readback, screenshot: true });
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

console.log('VERIFY_OPENCLAW_CONTROL_UI_CONNECTED_PASS');
