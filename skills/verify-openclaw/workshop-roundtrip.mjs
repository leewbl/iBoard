import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { promisify } from 'node:util';

const execute = promisify(execFile);
const root = await mkdtemp('/tmp/iboard-workshop-');
const home = join(root, 'home');
const state = join(root, 'state');
const agent = join(state, 'agents', 'main', 'agent');
const workspace = join(root, 'workspace');
const config = join(state, 'openclaw.json');
const workshop = join(agent, 'workshop-skills');
const name = 'iboard-workshop-fixture';
const file = 'SKILL.md';
const fixtureDir = join(workshop, name);
const fixturePath = join(fixtureDir, file);
const content = [
  '---',
  `name: ${name}`,
  'description: A disposable skill used to verify Workshop archive and restore.',
  '---',
  '',
  '# Workshop archive check',
  '',
  'Keep this example intact when restoring its saved version.',
  '',
].join('\n');
const env = {
  PATH: process.env.PATH, HOME: home, OPENCLAW_HOME: home,
  OPENCLAW_STATE_DIR: state, OPENCLAW_CONFIG_PATH: config, TMPDIR: root,
};

async function run(phase, ...args) {
  const command = ['skills', 'workshop', ...args, '--agent', 'main', '--json'];
  const { stdout } = await execute(process.execPath, [
    '--import', '/app/node_modules/tsx/dist/loader.mjs', '/app/src/entry.ts', ...command,
  ], {
    cwd: '/app', env, timeout: 75_000, killSignal: 'SIGKILL', maxBuffer: 512 * 1024,
  });
  const result = JSON.parse(stdout);
  console.log(JSON.stringify({ phase, command, result }));
  return result;
}

async function list(phase, live) {
  const result = await run(phase, 'list');
  assert.equal(result.agentId, 'main');
  assert.equal(result.root, workshop, 'Workshop selected a different agent directory');
  assert.deepEqual(result.skills.map(skill => skill.name), live ? [name] : []);
  if (live) assert.deepEqual(result.skills[0].files, [file]);
  return result.archived;
}

function checkContent(result) {
  assert.equal(result.name, name);
  assert.equal(result.filePath, file);
  assert.deepEqual(result.files, [file]);
  assert.equal(result.content, content, 'Workshop show returned different skill contents');
}

try {
  await mkdir(home);
  await mkdir(workspace);
  await mkdir(fixtureDir, { recursive: true });
  const configText = JSON.stringify({
    agents: { defaults: { workspace } },
    gateway: { mode: 'local' },
    plugins: { enabled: false },
  });
  await writeFile(config, configText, { mode: 0o600 });
  await writeFile(fixturePath, content, { mode: 0o600 });

  assert.deepEqual(await list('initial-list', true), []);
  const { change: archived } = await run('archive', 'archive', name, '--reason', 'iBoard fixture');
  assert.equal(archived.action, 'archive');
  assert.equal(archived.agentId, 'main');
  assert.equal(archived.skillName, name);
  assert.equal(archived.actor, 'user');
  assert.match(archived.summary, /iBoard fixture/);
  assert.ok(archived.versionId, 'Archive did not return a restorable version');
  await assert.rejects(stat(fixturePath), { code: 'ENOENT' });

  const archiveEntries = await list('archived-list', false);
  assert.equal(archiveEntries.length, 1);
  assert.equal(archiveEntries[0].name, name);
  assert.equal(archiveEntries[0].live, false);
  assert.deepEqual(archiveEntries[0].versions.map(({ id, action }) => [id, action]), [
    [archived.versionId, 'archive'],
  ]);
  checkContent(await run('archived-show', 'show', name, '--version', archived.versionId));

  const { change: restored } = await run('restore', 'restore', name, '--version', archived.versionId);
  assert.equal(restored.action, 'restore');
  assert.equal(restored.agentId, 'main');
  assert.equal(restored.skillName, name);
  assert.equal(restored.actor, 'user');
  const restoredEntries = await list('restored-list', true);
  assert.equal(restoredEntries.length, 1);
  assert.equal(restoredEntries[0].name, name);
  assert.equal(restoredEntries[0].live, true);
  assert.ok(restoredEntries[0].versions.some(({ id }) => id === archived.versionId));
  checkContent(await run('restored-show', 'show', name));
  const bytes = await readFile(fixturePath);
  assert.deepEqual(bytes, Buffer.from(content), 'Restored file differs from seeded bytes');
  assert.equal(await readFile(config, 'utf8'), configText);
  console.log(JSON.stringify({
    phase: 'content', restoredSha256: createHash('sha256').update(bytes).digest('hex'),
    fixtureBytes: bytes.length, configUnchanged: true,
  }));
} finally {
  await rm(root, { recursive: true, force: true });
  await assert.rejects(stat(root), { code: 'ENOENT' });
  console.log(JSON.stringify({ phase: 'cleanup', scratchRemoved: true }));
}
console.log('VERIFY_OPENCLAW_WORKSHOP_ROUNDTRIP_PASS');
