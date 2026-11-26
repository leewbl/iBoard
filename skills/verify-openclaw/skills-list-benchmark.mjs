import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { performance } from 'node:perf_hooks';
import { promisify } from 'node:util';

const execute = promisify(execFile);
const command = [
  '--import', '/app/node_modules/tsx/dist/loader.mjs', '/app/src/entry.ts',
  'skills', 'list', '--agent', 'main', '--json',
];
const expected = Array.from({ length: 32 }, (_, index) => {
  const number = String(index + 1).padStart(2, '0');
  return {
    name: `iboard-benchmark-${number}`,
    description: `Local skill listing benchmark fixture ${number}.`,
    eligible: true,
    disabled: false,
    blockedByAllowlist: false,
    blockedByAgentFilter: false,
    modelVisible: true,
    userInvocable: true,
    commandVisible: true,
    source: 'openclaw-workspace',
    bundled: false,
    missing: { bins: [], anyBins: [], env: [], config: [], os: [] },
  };
});

function resultDigest(rows) {
  const metadata = rows.map(({ name, description, eligible, source }) => [name, description, eligible, source]);
  return createHash('sha256').update(JSON.stringify(metadata)).digest('hex');
}

const root = await mkdtemp('/tmp/iboard-skills-list-benchmark-');
const home = join(root, 'home');
const state = join(root, 'state');
const workspace = join(root, 'workspace');
const skillDirectory = join(workspace, 'skills');
const emptyBundledSkills = join(root, 'bundled-skills');
const config = join(state, 'openclaw.json');
const env = {
  PATH: process.env.PATH, HOME: home, OPENCLAW_HOME: home,
  OPENCLAW_STATE_DIR: state, OPENCLAW_CONFIG_PATH: config, TMPDIR: root,
  OPENCLAW_BUNDLED_SKILLS_DIR: emptyBundledSkills,
};
const configContents = JSON.stringify({
  agents: { defaults: { workspace } }, plugins: { enabled: false }, skills: { allowBundled: [] },
});
const files = expected.map(({ name, description }) => ({
  directory: join(skillDirectory, name),
  contents: `---\nname: ${name}\ndescription: ${description}\n---\n\nOpenClaw benchmark fixture.\n`,
}));

let elapsedMs;
let digest;
try {
  await Promise.all([mkdir(home), mkdir(state), mkdir(emptyBundledSkills),
    mkdir(skillDirectory, { recursive: true })]);
  await writeFile(config, configContents, { mode: 0o600 });
  await Promise.all(files.map(async ({ directory, contents }) => {
    await mkdir(directory);
    await writeFile(join(directory, 'SKILL.md'), contents);
  }));

  const start = performance.now();
  const { stdout } = await execute(process.execPath, command, {
    cwd: '/app', env, timeout: 30_000, killSignal: 'SIGKILL', maxBuffer: 512 * 1024,
  });
  elapsedMs = performance.now() - start;

  const result = JSON.parse(stdout);
  assert.equal(result.workspaceDir, workspace, 'list resolved a different workspace');
  assert.ok(Array.isArray(result.skills), 'list did not return a skills array');
  const rows = [...result.skills].sort((left, right) => left.name.localeCompare(right.name, 'en'));
  assert.deepEqual(rows, expected, 'list did not return exactly the expected skill records');
  digest = resultDigest(rows);
  assert.equal(digest, resultDigest(expected));
  assert.equal(await readFile(config, 'utf8'), configContents, 'list changed the configuration');
  assert.deepEqual((await readdir(skillDirectory)).sort(), expected.map(({ name }) => name));
  await Promise.all(files.map(async ({ directory, contents }) => {
    assert.deepEqual(await readdir(directory), ['SKILL.md']);
    assert.equal(await readFile(join(directory, 'SKILL.md'), 'utf8'), contents);
  }));
} finally {
  await rm(root, { recursive: true, force: true });
  await assert.rejects(stat(root), { code: 'ENOENT' });
}

console.log(JSON.stringify({
  kind: 'iboard-benchmark', unit: 'ms', elapsed_ms: elapsedMs, completed: 1, errors: 0, digest,
}));
