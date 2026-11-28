import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, open, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

const { WebSocket, WebSocketServer } = createRequire('/app/package.json')('ws');
const root = await mkdtemp('/tmp/iboard-skills-team-');
const home = join(root, 'home');
const state = join(root, 'state');
const config = join(state, 'openclaw.json');
const log = join(root, 'gateway.log');
const port = 18789;
const proxyPort = 18790;
const fixtureOrigin = 'https://iboard-skills.example.test';
const roles = ['owner', 'member', 'admin'];
const emails = Object.fromEntries(roles.map(role => [role, `${role}@iboard-skills.example.test`]));
const credentials = Object.fromEntries(roles.map(role => [role,
  'Basic ' + Buffer.from(`${role}:${randomUUID()}`).toString('base64')]));
const grants = { owner: ['operator.write'], member: ['operator.write'], admin: ['operator.admin'] };
const slug = 'iboard-team-fixture';
const content = `---\nname: ${slug}\ndescription: Offline team library fixture\n---\n\nCheck the shared checklist.\n`;
const support = 'Team checklist attachment.\n';
const digest = value => createHash('sha256').update(value).digest('hex');
const emit = (phase, fields) => console.log(JSON.stringify({ phase, ...fields }));
const health = path => fetch(`http://127.0.0.1:${port}${path}`, { signal: AbortSignal.timeout(4000) });
const refused = error => error.cause?.code === 'ECONNREFUSED';
const clients = [];
let gateway, exited, exit, proxy, peers, passed = false;

async function startGateway() {
  await assert.rejects(health('/healthz'), refused);
  const output = await open(log, 'w', 0o600);
  try {
    gateway = spawn(process.execPath, ['--import', '/app/node_modules/tsx/dist/loader.mjs',
      '/app/src/entry.ts', 'gateway', '--port', `${port}`, '--bind', 'loopback'], {
      cwd: '/app', stdio: ['ignore', output.fd, output.fd], env: {
        PATH: process.env.PATH, HOME: home, OPENCLAW_HOME: home, OPENCLAW_STATE_DIR: state,
        OPENCLAW_CONFIG_PATH: config, TMPDIR: root, XDG_CACHE_HOME: join(root, 'cache'), NO_COLOR: '1',
        OPENCLAW_DISABLE_BONJOUR: '1', OPENCLAW_SKIP_CHANNELS: '1', OPENCLAW_SKIP_PROVIDERS: '1',
        OPENCLAW_SKIP_CRON: '1', OPENCLAW_SKIP_BROWSER_CONTROL_SERVER: '1', OPENCLAW_SKIP_CANVAS_HOST: '1',
        GOMAXPROCS: '2', UV_THREADPOOL_SIZE: '2', NODE_OPTIONS: '--v8-pool-size=2 --max-semi-space-size=4',
      },
    });
    exited = new Promise(resolve => gateway.once('close', (code, signal) => resolve(exit = { code, signal })));
    gateway.once('error', error => { exit = { error: error.message }; });
  } finally {
    await output.close();
  }
  const deadline = performance.now() + 130_000;
  while (!exit && performance.now() < deadline) {
    const [ready, live] = await Promise.all([
      health('/readyz').then(async r => r.ok && (await r.json()).ready === true).catch(() => false),
      health('/healthz').then(async r => r.ok && (await r.json()).ok === true).catch(() => false),
    ]);
    if (ready && live) return { gatewayPid: gateway.pid, ready, live };
    await sleep(250);
  }
  assert.fail(`The team Gateway did not become ready: ${JSON.stringify(exit)}`);
}

async function startProxy() {
  peers = new WebSocketServer({ noServer: true });
  proxy = createServer((_request, response) => response.writeHead(404).end());
  proxy.on('upgrade', (request, socket, head) => {
    const user = roles.find(role => request.headers.authorization === credentials[role]);
    if (!user || request.headers.origin !== fixtureOrigin) {
      socket.end('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n');
      return;
    }
    peers.handleUpgrade(request, socket, head, downstream => {
      const upstream = new WebSocket(`ws://127.0.0.1:${port}`, {
        origin: fixtureOrigin,
        headers: {
          'x-forwarded-user': emails[user], 'x-forwarded-proto': 'https', 'x-forwarded-for': '203.0.113.50',
        },
      });
      downstream.on('message', (data, binary) => upstream.send(data, { binary }));
      upstream.on('message', (data, binary) => downstream.send(data, { binary }));
      upstream.on('error', () => downstream.close());
      downstream.on('error', () => upstream.close());
      upstream.on('close', () => downstream.close());
      downstream.on('close', () => upstream.close());
    });
  });
  await new Promise((resolve, reject) => {
    proxy.once('error', reject);
    proxy.listen(proxyPort, '127.0.0.1', resolve);
  });
}

function proxySocket(role) {
  return new WebSocket(`ws://127.0.0.1:${proxyPort}`, { origin: fixtureOrigin,
    headers: role ? { authorization: credentials[role] } : {}, handshakeTimeout: 15_000 });
}

async function connect(role) {
  const socket = proxySocket(role);
  clients.push(socket);
  const challenge = Promise.withResolvers();
  const pending = new Map();
  socket.on('message', data => {
    const frame = JSON.parse(data);
    if (frame.type === 'event' && frame.event === 'connect.challenge') challenge.resolve();
    if (frame.type === 'res') pending.get(frame.id)?.resolve(frame);
  });
  const disconnected = error => {
    challenge.reject(error);
    for (const item of pending.values()) item.reject(error);
  };
  socket.on('error', disconnected);
  socket.on('close', () => disconnected(new Error(`Team socket closed for ${role}`)));
  const wait = async (promise, label) => {
    let timer;
    try {
      return await Promise.race([promise, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`Timed out waiting for ${role} ${label}`)), 30_000);
      })]);
    } finally { clearTimeout(timer); }
  };
  const rpc = async (method, params, expectedCode) => {
    const id = randomUUID();
    const current = Promise.withResolvers();
    pending.set(id, current);
    try {
      socket.send(JSON.stringify({ type: 'req', id, method, params }));
      const response = await wait(current.promise, method);
      if (expectedCode) {
        assert.equal(response.ok, false, `${role} unexpectedly completed ${method}`);
        assert.equal(response.error?.details?.code, expectedCode, JSON.stringify(response.error));
        return response.error.details.code;
      }
      assert.equal(response.ok, true, `${role} ${method}: ${JSON.stringify(response.error)}`);
      return response.payload;
    } finally { pending.delete(id); }
  };
  await wait(challenge.promise, 'connect challenge');
  const hello = await rpc('connect', { minProtocol: 4, maxProtocol: 4,
    client: { id: 'openclaw-control-ui', version: 'control-ui', platform: 'web', mode: 'webchat' },
    role: 'operator', scopes: grants[role] });
  assert.equal(hello.type, 'hello-ok');
  return rpc;
}

async function drive() {
  const unauthenticated = proxySocket();
  const unauthenticatedStatus = await new Promise((resolve, reject) => {
    unauthenticated.once('error', reject);
    unauthenticated.once('open', () => reject(new Error('Fixture proxy admitted an unauthenticated client')));
    unauthenticated.once('unexpected-response', (_request, response) => {
      response.resume();
      resolve(response.statusCode);
    });
  });
  assert.equal(unauthenticatedStatus, 401);
  const [owner, member, admin] = await Promise.all(roles.map(connect));
  const list = (rpc, scope, sessionKey) => rpc('skills.library.list', { scope, ...(sessionKey ? { sessionKey } : {}) });
  const profiles = Object.fromEntries(await Promise.all(roles.map(async (role, index) => {
    const id = (await list([owner, member, admin][index], 'mine')).profileId;
    assert.ok(typeof id === 'string' && id.length > 0);
    return [role, id];
  })));
  assert.equal(new Set(Object.values(profiles)).size, roles.length);

  const saved = await owner('skills.library.save', { slug, expectedRevision: null, content,
    files: [{ path: 'reference.txt', content: Buffer.from(support).toString('base64'), encoding: 'base64' }] });
  const { skillId, revision } = saved.entry;
  assert.equal(saved.entry.ownerProfileId, profiles.owner);
  assert.equal(saved.entry.shared, false);
  const mutate = (rpc, action, code) => rpc('skills.library.mutate', { skillId, action, expectedRevision: revision }, code);
  const read = (rpc, sessionKey, code) => rpc('skills.library.read', { skillId, revision, ...(sessionKey ? { sessionKey } : {}) }, code);
  const visible = async scope => (await list(member, scope)).entries.map(row => row.skillId);
  const gone = async () => {
    const entries = await visible('all');
    assert.deepEqual(entries, []);
    return { visibleCount: entries.length, directReadCode: await read(member, null, 'SKILL_LIBRARY_NOT_FOUND') };
  };
  const original = await gone();
  const session = async label => {
    const key = `agent:main:iboard-team-${label}`;
    const value = await member('sessions.create', { key, agentId: 'main' });
    assert.equal(value.key, key);
    assert.ok(value.sessionId);
    const { session } = await list(member, 'all', key);
    assert.equal(session.sessionKey, key);
    return { key, id: value.sessionId, selections: session.selections.map(({ skillId, revision }) => ({ skillId, revision })) };
  };
  const pin = { skillId, revision };
  const checkRead = async key => {
    const result = await read(member, key);
    assert.equal(result.entry.skillId, skillId);
    assert.equal(result.entry.revision, revision);
    assert.equal(result.content, content);
    assert.equal(result.files.length, 1);
    const file = result.files[0];
    assert.equal(file.path, 'reference.txt');
    const bytes = Buffer.from(file.content, file.encoding === 'base64' ? 'base64' : 'utf8');
    assert.equal(bytes.toString(), support);
    return { contentSha: digest(result.content), supportSha: digest(bytes) };
  };
  const shared = await mutate(owner, 'share');
  assert.equal(shared.entry.ownerProfileId, profiles.owner);
  assert.equal(shared.entry.revision, revision);
  assert.equal(shared.entry.shared, true);
  assert.deepEqual(await visible('team'), [skillId]);
  const sharedSession = await session('shared');
  assert.deepEqual(sharedSession.selections, [pin]);
  const sharedRead = await read(member);
  assert.equal(sharedRead.entry.canEdit, false);
  const memberMutationCode = await mutate(member, 'unshare', 'SKILL_LIBRARY_FORBIDDEN');
  const ownerTransferCode = await mutate(owner, 'transfer', 'SKILL_LIBRARY_FORBIDDEN');

  const unshared = await mutate(owner, 'unshare');
  assert.equal(unshared.entry.ownerProfileId, profiles.owner);
  assert.equal(unshared.entry.revision, revision);
  assert.equal(unshared.entry.shared, false);
  const hidden = await gone();
  const retained = await checkRead(sharedSession.key);
  const unsharedSession = await session('unshared');
  assert.deepEqual(unsharedSession.selections, []);

  const result = await mutate(admin, 'transfer');
  const { ownerProfileId, ownerLabel, authorProfileId, canEdit } = result.entry;
  assert.equal(ownerProfileId, null);
  assert.equal(ownerLabel, 'Team');
  assert.equal(authorProfileId, profiles.owner);
  assert.equal(canEdit, true);
  assert.equal(result.entry.shared, true);
  assert.equal(result.entry.revision, revision);
  const ownerMine = (await list(owner, 'mine')).entries;
  assert.deepEqual(ownerMine, []);
  assert.deepEqual(await visible('team'), [skillId]);
  const teamSession = await session('transferred');
  assert.deepEqual(teamSession.selections, [pin]);
  assert.equal(new Set([sharedSession.id, unsharedSession.id, teamSession.id]).size, 3);
  const transferredRead = await checkRead(teamSession.key);
  const formerOwnerMutationCode = await mutate(owner, 'disable', 'SKILL_LIBRARY_FORBIDDEN');
  const adminUnshareCode = await mutate(admin, 'unshare', 'SKILL_LIBRARY_FORBIDDEN');
  assert.equal((await read(member)).entry.enabled, true);
  return { profiles, skill: { ...pin, contentSha: digest(content), supportSha: digest(support) }, unauthenticatedStatus,
    sessions: { shared: sharedSession, unshared: unsharedSession, team: teamSession },
    steps: { private: original, shared: { ownerId: shared.entry.ownerProfileId,
      memberVisibleId: sharedRead.entry.skillId, memberCanEdit: sharedRead.entry.canEdit,
      memberMutationCode, ownerTransferCode },
    unshared: { ownerId: unshared.entry.ownerProfileId, ...hidden, ...retained },
    transferred: { ownerProfileId, ownerLabel, authorProfileId, ownerMineCount: ownerMine.length,
      adminCanEdit: canEdit, formerOwnerMutationCode, adminUnshareCode, ...transferredRead } } };
}

try {
  await Promise.all([mkdir(home), mkdir(state)]);
  await writeFile(config, JSON.stringify({
    plugins: { enabled: false }, cron: { enabled: false }, update: { checkOnStart: false },
    gateway: { mode: 'local', bind: 'loopback', publicOrigin: fixtureOrigin,
      trustedProxies: ['127.0.0.1'], controlUi: { enabled: false, allowedOrigins: [fixtureOrigin] },
      auth: { mode: 'trusted-proxy',
        identityScopes: Object.fromEntries(roles.map(role => [emails[role], grants[role]])),
        trustedProxy: { userHeader: 'x-forwarded-user', requiredHeaders: ['x-forwarded-proto'],
          allowLoopback: true, allowUsers: Object.values(emails) } } },
  }), { mode: 0o600 });
  emit('doctor', await startGateway());
  await startProxy();
  emit('team', await drive());
  passed = true;
} finally {
  try {
    for (const client of clients) client.terminate();
    for (const socket of peers?.clients ?? []) socket.terminate();
    if (proxy?.listening) await new Promise(resolve => proxy.close(resolve));
    if (gateway && !exit) {
      gateway.kill('SIGTERM');
      const timer = setTimeout(() => gateway.kill('SIGKILL'), 30_000);
      try { await exited; } finally { clearTimeout(timer); }
    }
    if (passed) {
      assert.deepEqual(exit, { code: 0, signal: null });
      await assert.rejects(health('/healthz'), refused);
    } else {
      process.stderr.write((await readFile(log, 'utf8').catch(() => '')).slice(-4000));
    }
  } finally {
    await rm(root, { recursive: true, force: true });
    emit('cleanup', { exit: exit ?? null, scratchRemoved: true });
  }
}
console.log('VERIFY_OPENCLAW_SKILLS_LIBRARY_TEAM_PASS');
