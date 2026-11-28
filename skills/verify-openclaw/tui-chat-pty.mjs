import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, open, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { promisify } from 'node:util';

const loader = '/app/node_modules/tsx/dist/loader.mjs';
await import(loader);
const { startPty } = await import('/app/src/tui/tui-pty-test-support.ts');
const { waitForSynchronizedFrameRows } = await import('/app/src/tui/tui-pty-terminal-evidence-test-support.ts');
const execute = promisify(execFile);
const entry = ['--import', loader, '/app/src/entry.ts'];
const address = '127.0.0.1:18789';
const url = 'ws://' + address;
const token = 'iboard-disposable-tui-proof-token';
const sessionKey = 'agent:main:iboard-tui-proof';
const prompt = 'Check the terminal repair loop.';
const reply = 'iBoard verified this terminal turn.';
const readyText = 'gateway connected';
const dimensions = { cols: 120, rows: 40 };
const root = await mkdtemp('/tmp/iboard-tui-pty-');
const home = join(root, 'home');
const state = join(root, 'state');
const config = join(state, 'openclaw.json');
const log = join(root, 'gateway.log');
const inputLog = join(root, 'backend.jsonl');
const ptys = [];
const env = {
  PATH: process.env.PATH, HOME: home, OPENCLAW_HOME: home, OPENCLAW_STATE_DIR: state,
  OPENCLAW_CONFIG_PATH: config, TMPDIR: root, XDG_CACHE_HOME: join(root, 'cache'), NO_COLOR: '1',
  OPENCLAW_DISABLE_BONJOUR: '1', OPENCLAW_SKIP_CHANNELS: '1', OPENCLAW_SKIP_PROVIDERS: '1',
  OPENCLAW_SKIP_CRON: '1', OPENCLAW_SKIP_BROWSER_CONTROL_SERVER: '1', OPENCLAW_SKIP_CANVAS_HOST: '1',
  OPENCLAW_TUI_PTY_COLS: String(dimensions.cols), OPENCLAW_TUI_PTY_ROWS: String(dimensions.rows),
  GOMAXPROCS: '2', UV_THREADPOOL_SIZE: '2', NODE_OPTIONS: '--v8-pool-size=2 --max-semi-space-size=4',
};
let gateway, closed, exit, launchError, passed = false;
const health = path => fetch('http://' + address + path, { signal: AbortSignal.timeout(4000), redirect: 'error' });
const refused = error => error.cause?.code === 'ECONNREFUSED';
const emit = (phase, data) => console.log(JSON.stringify({ phase, ...data }));

async function prepare() {
  const id = 'iboard-tui-fixture-cli';
  const plugin = join(home, id);
  const workspace = join(home, 'workspace');
  const backend = join(plugin, 'backend.mjs');
  await mkdir(home);
  await Promise.all([mkdir(state), mkdir(plugin), mkdir(workspace)]);
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
          command: process.execPath, args: [backend, inputLog], output: 'text', input: 'stdin', sessionMode: 'none',
        })} });
      },
    };`),
    writeFile(backend, `
      import { appendFileSync } from 'node:fs';
      let input = '';
      for await (const chunk of process.stdin) input += chunk;
      const inputMatched = input.includes(${JSON.stringify(prompt)});
      appendFileSync(process.argv[2], JSON.stringify({ pid: process.pid, inputMatched }) + '\\n');
      if (!inputMatched) throw new Error('Unexpected input to offline terminal fixture');
      process.stdout.write(${JSON.stringify(reply + '\n')});
    `),
    writeFile(config, JSON.stringify({
      agents: { defaults: { workspace, model: id + '/proof', timeoutSeconds: 90 }, entries: { main: {} } },
      plugins: { allow: [id], load: { paths: [plugin] }, entries: { [id]: { enabled: true } }, slots: { memory: 'none' } },
      cron: { enabled: false }, update: { checkOnStart: false },
      gateway: { mode: 'local', auth: { mode: 'token', token }, controlUi: { enabled: false } },
    }), { mode: 0o600 }),
  ]);
}

async function startGateway() {
  await assert.rejects(health('/healthz'), refused);
  const logfile = await open(log, 'a', 0o600);
  try {
    gateway = spawn(process.execPath, [...entry, 'gateway', '--port', '18789', '--bind', 'loopback'],
      { cwd: '/app', env, stdio: ['ignore', logfile.fd, logfile.fd] });
    closed = new Promise(resolve => gateway.once('close', (code, signal) => resolve(exit = { code, signal })));
    gateway.once('error', error => { launchError = error; });
  } finally {
    await logfile.close();
  }
  const deadline = performance.now() + 130_000;
  while (performance.now() < deadline && !exit && !launchError) {
    const [ready, live] = await Promise.all([
      health('/readyz').then(async r => r.ok && (await r.json()).ready === true).catch(() => false),
      health('/healthz').then(async r => r.ok && (await r.json()).ok === true).catch(() => false),
    ]);
    if (ready && live) return { gatewayPid: gateway.pid, ready, live };
    await sleep(250);
  }
  assert.ifError(launchError);
  assert.fail('The disposable TUI Gateway failed its health check');
}

async function call(method, params) {
  const command = [...entry, 'gateway', 'call', method, '--params', JSON.stringify(params),
    '--url', url, '--token', token, '--timeout', '40000', '--json'];
  const result = await execute(process.execPath, command, { cwd: '/app', env, timeout: 55_000, maxBuffer: 128 * 1024 });
  return JSON.parse(result.stdout);
}

async function terminal(submit) {
  const run = startPty(process.execPath, [...entry, 'tui', '--url', url, '--token', token, '--session', sessionKey],
    { cwd: '/app', env, exitTimeoutMs: 45_000, outputTimeoutMs: 100_000 });
  ptys.push(run);
  assert.deepEqual({ cols: run.cols, rows: run.rows }, dimensions);
  const has = (rows, text) => rows.some(row => row.includes(text));
  const get = (rows, text) => rows.find(row => row.includes(text)).trim();
  const ready = await waitForSynchronizedFrameRows(run, rows => has(rows, readyText), 100_000);
  if (submit) {
    assert.equal(has(ready, reply), false);
    await run.write(prompt + '\r');
  }
  const frame = await waitForSynchronizedFrameRows(run, rows => has(rows, prompt) && has(rows, reply), 100_000);
  const captured = Buffer.from(run.output());
  assert.ok(captured.byteLength <= 96 * 1024 && !captured.includes(token));
  await run.write('/exit\r');
  const ending = await run.waitForExit();
  const signal = ending.signal ?? 0;
  assert.deepEqual({ exitCode: ending.exitCode, signal }, { exitCode: 0, signal: 0 });
  return {
    pid: run.pid, readyRow: get(ready, readyText), promptRow: get(frame, prompt), replyRow: get(frame, reply),
    exitCode: ending.exitCode, signal, transcriptBase64: captured.toString('base64'),
    transcriptSha: createHash('sha256').update(captured).digest('hex'),
  };
}

async function drive() {
  const initial = await call('sessions.create', { key: sessionKey, agentId: 'main' });
  assert.equal(initial.key, sessionKey);
  assert.ok(initial.sessionId);
  const before = await call('chat.history', { sessionKey, limit: 20 });
  assert.deepEqual(before.messages, []);
  const first = await terminal(true);
  const reopened = await terminal(false);
  const after = await call('chat.history', { sessionKey, limit: 20 });
  const { session } = await call('sessions.describe', { key: sessionKey, agentId: 'main' });
  assert.equal(session.sessionId, initial.sessionId);
  const messages = after.messages.filter(item => ['user', 'assistant'].includes(item.role)).map(item => ({
    role: item.role, text: typeof item.content === 'string' ? item.content : item.content
      .filter(block => block.type === 'text').map(block => block.text).join(''),
  }));
  assert.deepEqual(messages, [{ role: 'user', text: prompt }, { role: 'assistant', text: reply }]);
  const calls = (await readFile(inputLog, 'utf8')).trim().split('\n').map(line => JSON.parse(line));
  assert.equal(calls.length, 1);
  const [{ pid, inputMatched }] = calls;
  assert.equal(inputMatched, true);
  assert.equal(new Set([pid, first.pid, reopened.pid, gateway.pid]).size, 4);
  return { sessionKey, sessionId: initial.sessionId, priorMessageCount: before.messages.length, dimensions,
    first, reopened, backend: { pid, inputMatched, calls: calls.length }, history: { sessionId: session.sessionId, messages } };
}

try {
  await prepare();
  emit('doctor', await startGateway());
  emit('tui', await drive());
  passed = true;
} catch (error) {
  throw new Error(String(error).replaceAll(token, '[fixture token]'));
} finally {
  try {
    await Promise.all(ptys.map(run => run.dispose()));
    if (gateway && !exit && !launchError) {
      gateway.kill('SIGTERM');
      const timer = setTimeout(() => gateway.kill('SIGKILL'), 30_000);
      try { await closed; } finally { clearTimeout(timer); }
    }
    if (passed) {
      assert.deepEqual(exit, { code: 0, signal: null });
      await assert.rejects(health('/healthz'), refused);
    } else {
      process.stderr.write((await readFile(log, 'utf8').catch(() => '')).slice(-4000).replaceAll(token, '[fixture token]'));
    }
  } finally {
    await rm(root, { recursive: true, force: true });
    emit('cleanup', { exit: exit ?? null, scratchRemoved: true });
  }
}
console.log('VERIFY_OPENCLAW_TUI_CHAT_PTY_PASS');
