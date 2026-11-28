import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtemp, open, readFile, rm } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

const maxTraceBytes = 32 * 1024 * 1024;
const maxScriptBytes = 2 * 1024 * 1024;
const maxScripts = 100;
const maxMapBytes = 8 * 1024 * 1024;
const maxMapEvidenceBytes = 16 * 1024 * 1024;
const maxMapAttempts = 16;
const requireFromImage = createRequire('/app/package.json');
const trackedFile = /^(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+\.(?:js|mjs|cjs|ts|mts|cts)$/;

function isTrackedFilename(value) {
  return typeof value === 'string' && value.length <= 400 && trackedFile.test(value)
    && !value.split('/').some(part => part === '.' || part === '..');
}

function selector(value) {
  return value == null || (typeof value === 'string' && value.length > 0 && value.length <= 400
    && !/[\x00-\x1f\x7f]/u.test(value));
}

function request(value) {
  if (!value || !isTrackedFilename(value.entry)
    || (value.loader && !isTrackedFilename(value.loader))
    || !Array.isArray(value.args) || value.args.length > 32
    || value.args.some(arg => typeof arg !== 'string' || Buffer.byteLength(arg) > 2048 || arg.includes('\0'))
    || !Number.isInteger(value.port) || value.port < 1024 || value.port > 65535
    || !Number.isInteger(value.window_ms) || value.window_ms < 0 || value.window_ms > 60_000
    || (value.source_maps !== undefined && typeof value.source_maps !== 'boolean')
    || typeof value.paused !== 'boolean' || !selector(value.click) || !selector(value.observe)
    || typeof value.route !== 'string' || value.route.length > 1024 || !value.route.startsWith('/')
    || /[\x00-\x20\x7f\\]/u.test(value.route) || value.route.startsWith('//')) {
    throw new Error('Invalid browser trace request');
  }
  const origin = `http://127.0.0.1:${value.port}`;
  const url = new URL(value.route, origin);
  if (url.origin !== origin || url.username || url.password) {
    throw new Error('The page must be on the trace container loopback origin');
  }
  return { ...value, origin, url: url.href };
}

async function bounded(promise, ms, description) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${description} timed out`)), ms);
    })]);
  } finally {
    clearTimeout(timer);
  }
}

function send(session, method, params) {
  return bounded(session.send(method, params), 5_000, method);
}

function localEnvironment(home, port) {
  return {
    PATH: process.env.PATH,
    LANG: 'C.UTF-8',
    HOME: home,
    XDG_CONFIG_HOME: join(home, 'config'),
    XDG_CACHE_HOME: join(home, 'cache'),
    OPENCLAW_HOME: home,
    OPENCLAW_STATE_DIR: join(home, 'state'),
    HOST: '127.0.0.1',
    PORT: String(port),
  };
}

function startServer(spec, env) {
  const args = [...(spec.loader ? ['--import', '/app/' + spec.loader] : []),
    '/app/' + spec.entry, ...spec.args];
  const child = spawn(process.execPath, args, { cwd: '/app', env, stdio: ['ignore', 2, 2] });
  const server = { child, stopped: false };
  server.closed = new Promise(resolve => child.once('close', (code, signal) => {
    server.stopped = true;
    resolve({ code, signal });
  }));
  child.once('error', error => { server.error = error; });
  return server;
}

async function waitForServer(server, url) {
  const deadline = performance.now() + 75_000;
  while (performance.now() < deadline) {
    if (server.error || server.stopped) throw new Error('The selected application exited before it became ready');
    try {
      const response = await fetch(url, { method: 'HEAD', redirect: 'manual', signal: AbortSignal.timeout(750) });
      await response.body?.cancel();
      if (response.status < 500) return;
    } catch {
      // The listener may not exist yet.
    }
    await sleep(150);
  }
  throw new Error('The selected application did not answer on loopback');
}

async function stopServer(server) {
  if (server.stopped) return;
  server.child.kill('SIGTERM');
  try {
    await bounded(server.closed, 2_000, 'application shutdown');
  } catch {
    server.child.kill('SIGKILL');
    await bounded(server.closed, 2_000, 'application termination');
  }
}

function trackScripts(session, origin, sourceMaps) {
  const scripts = new Map();
  let complete = true;
  let referenceChars = 0;
  session.on('Debugger.scriptParsed', event => {
    let url;
    try {
      url = new URL(event.url);
    } catch {
      return;
    }
    if (url.origin !== origin || url.username || url.password) return;
    const originalUrl = event.url;
    // Bind the actual browser URL without exposing its query or fragment.
    url.search = '';
    url.hash = '';
    if (event.scriptLanguage === 'WebAssembly') {
      complete = false;
      return;
    }
    if (typeof event.scriptId !== 'string' || scripts.size >= maxScripts
      || Buffer.byteLength(event.url) > 4096
      || (Number.isInteger(event.length) && event.length > maxScriptBytes)) {
      complete = false;
      return;
    }
    const identity = { url: url.href, url_sha256: sha256(originalUrl) };
    const declared = sourceMaps && typeof event.sourceMapURL === 'string' && event.sourceMapURL.length > 0;
    const standalone = event.startLine === 0 && event.startColumn === 0 && !event.hasSourceURL;
    const reference = declared && standalone && event.sourceMapURL.length <= 4 * maxMapBytes
      && referenceChars + event.sourceMapURL.length <= 2 * maxMapEvidenceBytes ? event.sourceMapURL : null;
    if (reference) referenceChars += reference.length;
    scripts.set(event.scriptId, { identity, originalUrl, declared, reference });
  });
  return async externalScripts => {
    const records = new Map();
    const candidates = new Map();
    const declaredUrls = new Set();
    const deadline = performance.now() + 10_000;
    const pending = Array.from(scripts);
    if (sourceMaps) {
      for (const [, script] of pending) {
        if (script.declared) declaredUrls.add(script.identity.url_sha256);
      }
    }
    for (const [scriptId, { identity, originalUrl, reference }] of pending) {
      if (performance.now() >= deadline) {
        complete = false;
        break;
      }
      try {
        const { scriptSource } = await send(session, 'Debugger.getScriptSource', { scriptId });
        if (typeof scriptSource !== 'string' || Buffer.byteLength(scriptSource) > maxScriptBytes) {
          complete = false;
          continue;
        }
        const digest = sha256(scriptSource);
        records.set(identity.url_sha256 + digest, { ...identity, sha256: digest });
        if (sourceMaps) {
          const urlHash = identity.url_sha256;
          const previous = candidates.get(urlHash);
          const locator = externalScripts.has(withoutFragment(originalUrl)) ? reference : null;
          if (previous) {
            previous.ambiguous ||= previous.script_sha256 !== digest || previous.reference !== locator;
          } else {
            candidates.set(urlHash, { url_sha256: urlHash, script_sha256: digest,
              originalUrl, reference: locator, ambiguous: false });
          }
        }
      } catch {
        complete = false;
      }
    }
    const mapDigest = sourceMaps ? await writeSourceMaps(declaredUrls, candidates, origin, complete) : null;
    return { scripts: [...records.values()].sort((a, b) => a.url.localeCompare(b.url)
      || a.url_sha256.localeCompare(b.url_sha256) || a.sha256.localeCompare(b.sha256)),
    scripts_complete: complete, ...(mapDigest ? { source_maps_sha256: mapDigest } : {}) };
  };
}

function sha256(data) {
  return createHash('sha256').update(data).digest('hex');
}

function withoutFragment(value) {
  const url = new URL(value);
  url.hash = '';
  return url.href;
}

function inlineSourceMap(reference) {
  const comma = reference.indexOf(',');
  const header = reference.slice(0, comma).toLowerCase();
  if (comma < 0 || !/^data:application\/json(?:;charset=utf-8)?(?:;base64)?$/.test(header)) return null;
  const payload = decodeURIComponent(reference.slice(comma + 1));
  if (!header.endsWith(';base64')) return Buffer.from(payload, 'utf8');
  const raw = Buffer.from(payload, 'base64');
  const encoded = raw.toString('base64');
  return encoded === payload || encoded.replace(/=+$/, '') === payload ? raw : null;
}

async function fetchSourceMap(url, timeout) {
  const response = await fetch(url, {
    redirect: 'manual', credentials: 'omit', signal: AbortSignal.timeout(timeout),
  });
  if (response.status !== 200 || response.url !== url.href || !response.body
    || Number(response.headers.get('content-length')) > maxMapBytes) {
    await response.body?.cancel();
    return null;
  }
  const chunks = [];
  let size = 0;
  const reader = response.body.getReader();
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return Buffer.concat(chunks, size);
      size += value.length;
      if (size > maxMapBytes) return null;
      chunks.push(value);
    }
  } finally {
    await reader.cancel();
  }
}

async function sourceMap(candidate, origin, timeout) {
  const { reference } = candidate;
  let raw, mapUrl;
  if (/^data:/i.test(reference)) {
    raw = inlineSourceMap(reference);
    mapUrl = 'inline';
  } else {
    if (reference.length > 4096 || /[\x00-\x20\x7f\\]/u.test(reference)) return null;
    const url = new URL(reference, candidate.originalUrl);
    if (url.origin !== origin || url.href !== origin + url.pathname) return null;
    raw = await fetchSourceMap(url, timeout);
    mapUrl = url.href;
  }
  if (!raw?.length || raw.length > maxMapBytes) return null;
  const parsed = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw));
  if (parsed?.version !== 3 || !(Array.isArray(parsed.sections)
    || (Array.isArray(parsed.sources) && typeof parsed.mappings === 'string'))) return null;
  return { url_sha256: candidate.url_sha256, script_sha256: candidate.script_sha256,
    map_url: mapUrl, map_sha256: sha256(raw), content_base64: raw.toString('base64') };
}

async function writeSourceMaps(declaredUrls, candidates, origin, complete) {
  const maps = [];
  let omitted = 0, attempts = 0;
  // Leave room for the manifest wrapper and the omission count.
  let evidenceBytes = 128;
  const deadline = performance.now() + 10_000;
  for (const url of declaredUrls) {
    const candidate = candidates.get(url);
    const remaining = Math.floor(deadline - performance.now());
    if (complete && candidate?.reference && !candidate.ambiguous && remaining > 0 && attempts < maxMapAttempts) {
      attempts += 1;
      try {
        const record = await sourceMap(candidate, origin, Math.min(remaining, 3_000));
        if (record) {
          const size = Buffer.byteLength(JSON.stringify(record)) + 1;
          if (evidenceBytes + size <= maxMapEvidenceBytes) {
            evidenceBytes += size;
            maps.push(record);
            continue;
          }
        }
      } catch {
        // An inaccessible or malformed map leaves its script unbound.
      }
    }
    omitted += 1;
  }
  const evidence = `${JSON.stringify({ schema: 1, maps, omitted })}\n`;
  const output = await open('/artifacts/browser-source-maps.json', 'wx', 0o600);
  try {
    await output.writeFile(evidence);
  } finally {
    await output.close();
  }
  return sha256(evidence);
}

async function sample(locator) {
  return locator.evaluate(element => {
    const rect = element.getBoundingClientRect();
    const animations = element.getAnimations({ subtree: true });
    if (animations.length > 64) throw new Error('The observed element has more than 64 animations');
    return {
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      animations: animations.map(animation => ({
        id: (animation.id || animation.animationName || animation.transitionProperty || '').slice(0, 160),
        type: animation.constructor.name.slice(0, 80),
        time: typeof animation.currentTime === 'number' && Number.isFinite(animation.currentTime)
          ? animation.currentTime : null,
      })),
    };
  }, undefined, { timeout: 6_000 });
}

async function writeTrace(session) {
  const completed = new Promise(resolve => session.once('Tracing.tracingComplete', resolve));
  await send(session, 'Tracing.end');
  const trace = await bounded(completed, 10_000, 'trace completion');
  if (trace.dataLossOccurred !== false || typeof trace.stream !== 'string'
    || (trace.traceFormat && trace.traceFormat !== 'json')
    || (trace.streamCompression && trace.streamCompression !== 'none')) {
    throw new Error('Chromium did not return a complete JSON trace stream');
  }
  const filename = '/artifacts/trace.cdptrace';
  const output = await open(filename, 'wx', 0o600);
  let bytes = 0;
  try {
    for (;;) {
      const part = await send(session, 'IO.read', { handle: trace.stream, size: 128 * 1024 });
      if (typeof part.data !== 'string' || typeof part.eof !== 'boolean') {
        throw new Error('Chromium returned an invalid trace chunk');
      }
      const data = Buffer.from(part.data, part.base64Encoded ? 'base64' : 'utf8');
      bytes += data.length;
      if ((!part.eof && data.length === 0) || bytes > maxTraceBytes) {
        throw new Error('The trace is incomplete or exceeds 32 MiB');
      }
      await output.writeFile(data);
      if (part.eof) break;
    }
    if (!bytes) throw new Error('Chromium returned an empty trace');
  } catch (error) {
    // A partial trace is never presented as complete.
    await rm(filename, { force: true });
    throw error;
  } finally {
    try {
      await output.close();
    } finally {
      await send(session, 'IO.close', { handle: trace.stream });
    }
  }
}

async function capture(browser, spec, browserProbe) {
  const context = await browser.newContext({
    viewport: { width: 1200, height: 800 }, serviceWorkers: 'block', acceptDownloads: false,
  });
  let redirectedOutside = false;
  const local = value => {
    const url = new URL(value);
    return url.origin === spec.origin && !url.username && !url.password;
  };
  // Playwright routes the first request of a redirect chain. A later request
  // outside the allowed origin invalidates the capture in the offline sandbox.
  context.on('request', request => {
    if (request.redirectedFrom() && !local(request.url())) redirectedOutside = true;
  });
  const checkOrigin = page => {
    if (redirectedOutside || !local(page.url())) {
      throw new Error('The browser request left the selected loopback origin');
    }
  };
  await context.route('**/*', route => {
    return local(route.request().url()) ? route.continue() : route.abort('blockedbyclient');
  });
  if (typeof context.routeWebSocket === 'function') {
    const allowedSocketOrigin = spec.origin.replace(/^http:/, 'ws:');
    await context.routeWebSocket('**/*', route => {
      const url = new URL(route.url());
      if (url.origin === allowedSocketOrigin && !url.username && !url.password) route.connectToServer();
      else return route.close();
    });
  }
  const page = await context.newPage();
  page.setDefaultTimeout(6_000);
  const externalScripts = new Set();
  if (spec.source_maps) {
    page.on('response', response => {
      const url = response.url();
      const request = response.request();
      if (externalScripts.size < maxScripts && url.length <= 4096 && response.ok() && local(url)
        && request.resourceType() === 'script' && request.frame() === page.mainFrame()) {
        externalScripts.add(withoutFragment(url));
      }
    });
  }
  const pageSession = await context.newCDPSession(page);
  const traceSession = await browser.newBrowserCDPSession();
  const digests = trackScripts(pageSession, spec.origin, spec.source_maps);
  await send(pageSession, 'Debugger.enable');
  await send(pageSession, 'Animation.enable');
  const expectedRate = spec.paused ? 0 : 1;
  await send(pageSession, 'Animation.setPlaybackRate', { playbackRate: expectedRate });
  await send(traceSession, 'Tracing.start', {
    transferMode: 'ReturnAsStream', streamFormat: 'json', streamCompression: 'none',
    traceConfig: {
      recordMode: 'recordUntilFull',
      includedCategories: ['devtools.timeline', 'blink.user_timing', 'v8', 'disabled-by-default-v8.cpu_profiler'],
    },
  });
  const response = await page.goto(spec.url, { waitUntil: 'load', timeout: 12_000 });
  checkOrigin(page);
  if (!response?.ok()) {
    throw new Error('The browser failed to load the selected loopback page');
  }
  const { playbackRate } = await send(pageSession, 'Animation.getPlaybackRate');
  if (playbackRate !== expectedRate) throw new Error('Chromium did not apply the selected animation playback rate');
  await page.evaluate(() => performance.mark('iboard:before-action'));
  if (spec.click) await page.locator(spec.click).click();
  checkOrigin(page);
  await page.evaluate(() => performance.mark('iboard:window-start'));
  const locator = spec.observe ? page.locator(spec.observe) : null;
  const before = locator ? await sample(locator) : null;
  await sleep(spec.window_ms);
  const after = locator ? await sample(locator) : null;
  const probe = browserProbe ? await browserProbe({ page, session: pageSession, send, checkOrigin, origin: spec.origin }) : null;
  await page.evaluate(() => performance.mark('iboard:window-end'));
  const finalRate = await send(pageSession, 'Animation.getPlaybackRate');
  checkOrigin(page);
  if (finalRate.playbackRate !== expectedRate) {
    throw new Error('The animation playback rate changed during the capture');
  }
  await writeTrace(traceSession);
  const scriptEvidence = await digests(externalScripts);
  checkOrigin(page);
  return {
    kind: 'iboard-browser-trace', port: spec.port, route: spec.route, window_ms: spec.window_ms,
    paused: spec.paused, playback_rate: playbackRate, clicked: Boolean(spec.click),
    observed: locator ? { before, after } : null, ...scriptEvidence, ...(probe ? { probe } : {}),
  };
}

async function main() {
  const spec = request(JSON.parse(await readFile('/workspace/browser-request.json', 'utf8')));
  let browserProbe = null;
  if (spec.probe !== undefined) {
    if (spec.paused) throw new Error('Browser probe cannot be combined with the animation pause');
    const { prepareBrowserProbe } = await import('./browser-probe.mjs');
    browserProbe = await prepareBrowserProbe(spec.probe);
  }
  const home = await mkdtemp('/tmp/iboard-browser-');
  const env = localEnvironment(home, spec.port);
  let browser, server, report;
  try {
    server = startServer(spec, env);
    await waitForServer(server, spec.url);
    const { chromium } = requireFromImage('/app/node_modules/playwright');
    browser = await chromium.launch({
      env, headless: true, timeout: 12_000,
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || chromium.executablePath(),
    });
    report = await capture(browser, spec, browserProbe);
  } finally {
    try {
      if (browser) await bounded(browser.close(), 8_000, 'Chromium shutdown');
    } finally {
      if (server) await stopServer(server);
      await rm(home, { recursive: true, force: true });
    }
  }
  process.stdout.write(`${JSON.stringify({ ...report,
    browser_closed: !browser.isConnected(), server_stopped: server.stopped })}\n`);
}

main().catch(error => {
  process.stderr.write(`Browser trace stopped: ${String(error.message).slice(0, 600)}\n`);
  process.exitCode = 1;
});
