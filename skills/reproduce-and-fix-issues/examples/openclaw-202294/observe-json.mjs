import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { promisify } from 'node:util';

const root = await mkdtemp('/tmp/iboard-legacy-json-');
const settings = join(root, 'openclaw.json');
const legacy = 'iboard-legacy-json';
const current = 'iboard-current-json';
const command = ['skills', 'check', '--agent', 'main', '--json'];
const config = JSON.stringify({ agents: { defaults: { workspace: root } },
  plugins: { enabled: false }, skills: { allowBundled: [] } });
const fixtures = { [settings]: config };
let absent;

try {
  for (const [name, key] of [[legacy, 'clawdbot'], [current, 'openclaw']]) {
    const directory = join(root, 'skills', name);
    await mkdir(directory, { recursive: true });
    fixtures[join(directory, 'SKILL.md')] = `---\nname: ${name}\ndescription: Local metadata fixture.\nmetadata: ${JSON.stringify({ [key]: { emoji: 'L' } })}\n---\n`;
  }
  for (const [file, contents] of Object.entries(fixtures)) await writeFile(file, contents);
  const { stdout } = await promisify(execFile)(process.execPath,
    ['--import', '/app/node_modules/tsx/dist/loader.mjs', '/app/src/entry.ts', ...command], {
      cwd: '/app', env: { PATH: process.env.PATH, HOME: root, OPENCLAW_HOME: root,
        OPENCLAW_STATE_DIR: root, OPENCLAW_CONFIG_PATH: settings, TMPDIR: root },
      timeout: 75_000, maxBuffer: 256 * 1024,
    });
  const result = JSON.parse(stdout);
  assert.equal(result.workspaceDir, root);
  assert.ok(result.eligible.includes(legacy) && result.eligible.includes(current));
  absent = !Object.hasOwn(result, 'legacyMetadata');
  if (!absent) assert.deepEqual(result.legacyMetadata, [legacy]);
  for (const [file, contents] of Object.entries(fixtures)) assert.equal(await readFile(file, 'utf8'), contents);
  console.log(JSON.stringify({ command, eligibleFixtures: [legacy, current],
    legacyMetadata: absent ? null : result.legacyMetadata, inputsUnchanged: true }));
} finally {
  await rm(root, { recursive: true, force: true });
}
console.log(absent ? 'OPENCLAW_LEGACY_JSON_FAIL' : 'OPENCLAW_LEGACY_JSON_PASS');
process.exitCode = absent ? 1 : 0;
