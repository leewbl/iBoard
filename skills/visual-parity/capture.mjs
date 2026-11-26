import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { mkdtemp, readFile, realpath, rm, stat, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { isAbsolute, join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';

const appRoot = '/app/';
const actionNames = new Set(['click', 'hover', 'fill', 'press', 'visible', 'text']);
const actionsWithValue = new Set(['fill', 'press', 'text']);

function string(value, maximum) {
  return typeof value === 'string' && value.length <= maximum && !value.includes('\0');
}

async function appFile(relative) {
  if (!string(relative, 1024) || !relative || isAbsolute(relative) ||
      relative.includes('\\') || relative.split('/').some(part => !part || part === '.' || part === '..')) {
    throw new Error('Expected an application-relative file path');
  }
  const path = await realpath(join(appRoot, relative));
  if (!path.startsWith(appRoot) || !(await stat(path)).isFile()) {
    throw new Error('Selected file is outside the application or is not a regular file');
  }
  return path;
}

function loopbackPath(path, origin) {
  if (!string(path, 1024) || !path.startsWith('/') || path.startsWith('//') || path.includes('\\')) {
    throw new Error('Expected an origin-relative route');
  }
  const url = new URL(path, origin);
  if (url.origin !== origin || url.username || url.password) {
    throw new Error('Route escaped the test server');
  }
  return url;
}

async function startServer(server, home) {
  if (!Number.isInteger(server.port) || server.port < 1024 || server.port > 65535 ||
      !Array.isArray(server.args) || server.args.length > 32 ||
      !server.args.every(arg => string(arg, 2048))) {
    throw new Error('Invalid test server settings');
  }
  const entry = await appFile(server.entry);
  const origin = `http://127.0.0.1:${server.port}`;
  const ready = loopbackPath(server.ready, origin);
  const child = spawn(process.execPath, [entry, ...server.args], {
    cwd: appRoot,
    env: {
      PATH: process.env.PATH ?? '/usr/local/bin:/usr/bin:/bin',
      HOME: home,
      TMPDIR: home,
      NODE_ENV: 'test',
      HOST: '127.0.0.1',
      PORT: String(server.port),
      TZ: 'UTC',
      LANG: 'C.UTF-8',
    },
    stdio: ['ignore', 'ignore', 'inherit'],
  });
  let finished = false;
  let launchError;
  child.once('error', error => { launchError = error; });
  const closed = new Promise(resolve => child.once('close', () => {
    finished = true;
    resolve();
  }));

  const stop = async () => {
    if (!finished) {
      child.kill('SIGTERM');
      await Promise.race([closed, sleep(1500, undefined, { ref: false })]);
    }
    if (!finished) {
      child.kill('SIGKILL');
      await Promise.race([closed, sleep(1500, undefined, { ref: false })]);
    }
    if (!finished) throw new Error('Test server did not close');
  };
  const assertRunning = () => {
    if (launchError) throw launchError;
    if (finished || child.exitCode !== null || child.signalCode !== null) {
      throw new Error('Test server exited before capture');
    }
  };

  try {
    const deadline = performance.now() + 10_000;
    while (performance.now() < deadline) {
      assertRunning();
      const response = await fetch(ready, {
        redirect: 'manual',
        signal: AbortSignal.timeout(700),
      }).catch(() => undefined);
      if (response) {
        await response.body?.cancel();
        if (response.ok) return { origin, stop, assertRunning };
      }
      await sleep(100);
    }
    throw new Error('Test server readiness timed out');
  } catch (error) {
    await stop();
    throw error;
  }
}

async function allowLocalFile(url) {
  try {
    return url.protocol === 'file:' && !url.host &&
      (await realpath(fileURLToPath(url))).startsWith(appRoot);
  } catch {
    return false;
  }
}

async function executeStep(page, step) {
  const { action, selector, value } = step;
  if (!actionNames.has(action) || !string(selector, 500) || !selector.trim() ||
      (actionsWithValue.has(action) ? !string(value, 2048) : value !== undefined)) {
    throw new Error('Invalid capture action');
  }
  const locator = page.locator(selector);
  await locator.waitFor({ state: 'visible' });
  switch (action) {
    case 'click': await locator.click(); break;
    case 'hover': await locator.hover(); break;
    case 'fill': await locator.fill(value); break;
    case 'press': await locator.press(value); break;
    case 'visible': break;
    case 'text': {
      const deadline = performance.now() + 6_000;
      while (await locator.innerText() !== value) {
        if (performance.now() >= deadline) throw new Error('Expected state text was not observed');
        await sleep(50);
      }
      break;
    }
  }
}

async function digestFile(path) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest('hex');
}

async function capture(manifest, state) {
  const { viewport } = manifest;
  const requireFromImage = createRequire('/app/package.json');
  const playwrightPath = '/app/node_modules/playwright';
  const { chromium } = requireFromImage(playwrightPath);
  const { version: playwright } = requireFromImage(playwrightPath + '/package.json');
  const executablePath = await realpath(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || chromium.executablePath());
  const chromiumSha256 = await digestFile(executablePath);
  const home = await mkdtemp('/tmp/iboard-visual-');
  let browser;
  let server;
  try {
    browser = await chromium.launch({ executablePath, headless: true, timeout: 12_000 });
    server = manifest.server ? await startServer(manifest.server, home) : undefined;
    const target = server ? loopbackPath(state.path, server.origin) : pathToFileURL(await appFile(state.path));
    const context = await browser.newContext({
      viewport,
      deviceScaleFactor: 1,
      locale: 'en-US',
      timezoneId: 'UTC',
      colorScheme: 'light',
      reducedMotion: 'reduce',
      serviceWorkers: 'block',
      acceptDownloads: false,
    });
    let denied;
    const allowDocument = url => server ? url.origin === server.origin : allowLocalFile(url);
    context.on('request', request => {
      if (server && request.redirectedFrom() && new URL(request.url()).origin !== server.origin) {
        denied ??= 'A browser redirect left the capture surface';
      }
    });
    await context.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      const inline = !request.isNavigationRequest() && (url.protocol === 'data:' || url.protocol === 'blob:');
      if (inline || await allowDocument(url)) {
        await route.continue();
      } else {
        denied ??= 'Browser requested a resource outside the capture surface';
        await route.abort('blockedbyclient');
      }
    });
    await context.routeWebSocket('**/*', async route => {
      const url = new URL(route.url());
      if (server && url.protocol === 'ws:' && `http://${url.host}` === server.origin) {
        route.connectToServer();
      } else {
        denied ??= 'Browser requested a WebSocket outside the capture surface';
        await route.close({ code: 1008, reason: 'Outside capture surface' });
      }
    });
    const page = await context.newPage();
    context.on('page', other => {
      if (other !== page) denied ??= 'Capture state opened an unexpected page';
    });
    let scriptError;
    page.on('pageerror', error => { scriptError ??= error; });
    const checkPage = () => {
      server?.assertRunning();
      if (denied) throw new Error(denied);
      if (scriptError) throw scriptError;
    };
    page.setDefaultTimeout(7_000);
    const response = await page.goto(target.href, { waitUntil: 'load', timeout: 12_000 });
    if (server && !response?.ok()) throw new Error('Test server did not return a successful document');
    checkPage();
    for (const step of state.steps) {
      await executeStep(page, step);
      checkPage();
    }
    if (!(await allowDocument(new URL(page.url())))) throw new Error('Capture left its application surface');
    await page.evaluate(async () => {
      await document.fonts.ready;
      await Promise.all([...document.images]
        .filter(image => image.currentSrc && image.loading !== 'lazy')
        .map(image => image.decode()));
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    });
    const png = await page.screenshot({ fullPage: false, animations: 'disabled', caret: 'hide', scale: 'css' });
    checkPage();
    return {
      png,
      receipt: {
        kind: 'visual-parity-capture',
        state: state.id,
        actions: state.steps,
        viewport,
        browser: browser.version(),
        playwright,
        chromium_sha256: chromiumSha256,
        screenshot_sha256: createHash('sha256').update(png).digest('hex'),
      },
    };
  } finally {
    try {
      await browser?.close();
    } finally {
      try {
        await server?.stop();
      } finally {
        await rm(home, { recursive: true, force: true });
      }
    }
  }
}

async function main() {
  const [id, ...extra] = process.argv.slice(2);
  const screenshot = process.env.WORKBENCH_SCREENSHOT_PATH;
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(id ?? '') || extra.length || !screenshot) {
    throw new Error('Expected one capture state ID and a screenshot destination');
  }
  const manifest = JSON.parse(await readFile('/workspace/manifest.json', 'utf8'));
  const viewport = manifest.viewport;
  if (manifest.schema !== 1 || !viewport || !Array.isArray(manifest.states) ||
      !['width', 'height'].every(key => Number.isInteger(viewport[key]) && viewport[key] > 0 && viewport[key] <= 10000) ||
      viewport.width * viewport.height > 30_000_000) {
    throw new Error('Invalid capture manifest');
  }
  const matches = manifest.states.filter(state => state.id === id);
  if (matches.length !== 1 || !Array.isArray(matches[0].steps) || matches[0].steps.length > 32) {
    throw new Error('Invalid capture state or step count');
  }
  const { png, receipt } = await capture(manifest, matches[0]);
  await writeFile(screenshot, png, { flag: 'wx', mode: 0o600 });
  process.stdout.write(`${JSON.stringify(receipt)}\n`);
}

main().catch(error => {
  process.stderr.write(`Visual parity capture stopped. ${String(error.message).slice(0, 450)}\n`);
  process.exitCode = 1;
});
