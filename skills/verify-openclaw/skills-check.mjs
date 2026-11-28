import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { promisify } from 'node:util';

const root = await mkdtemp('/tmp/iboard-skills-check-');
const home = join(root, 'home');
const state = join(root, 'state');
const workspace = join(root, 'workspace');
const config = join(state, 'openclaw.json');
const available = 'iboard-fixture-ready';
const needsEnv = 'iboard-fixture-needs-env';
const envKey = 'IBOARD_TEST_SKILLS_READY';
const command = ['skills', 'check', '--agent', 'main', '--json'];
const env = {
  PATH: process.env.PATH, HOME: home, OPENCLAW_HOME: home,
  OPENCLAW_STATE_DIR: state, OPENCLAW_CONFIG_PATH: config, TMPDIR: root,
};

async function writeSkill(name, metadata) {
  const directory = join(workspace, 'skills', name);
  await mkdir(directory, { recursive: true });
  const path = join(directory, 'SKILL.md');
  await writeFile(path, [
    '---', `name: ${name}`, 'description: Local iBoard verification fixture.',
    ...(metadata ? [`metadata: ${JSON.stringify(metadata)}`] : []),
    '---', '', 'This fixture exercises local skill discovery.', '',
  ].join('\n'));
  return path;
}

async function check(extraEnv) {
  const { stdout } = await promisify(execFile)(process.execPath, [
    '--import', '/app/node_modules/tsx/dist/loader.mjs', '/app/src/entry.ts', ...command,
  ], {
    cwd: '/app', env: { ...env, ...extraEnv }, timeout: 75_000,
    killSignal: 'SIGKILL', maxBuffer: 512 * 1024,
  });
  const report = JSON.parse(stdout);
  assert.equal(report.workspaceDir, workspace, 'CLI resolved a different workspace');
  assert.ok(report.eligible.includes(available), 'ready skill was not discovered');
  return report;
}

try {
  await mkdir(home);
  await mkdir(state);
  await writeFile(config, JSON.stringify({
    agents: { defaults: { workspace } },
    plugins: { enabled: false },
    skills: { allowBundled: [] },
  }), { mode: 0o600 });
  const inputs = [config, await writeSkill(available),
    await writeSkill(needsEnv, { openclaw: { requires: { env: [envKey] } } })];
  const snapshot = await Promise.all(inputs.map(path => readFile(path, 'utf8')));

  const missing = await check({});
  assert.ok(!missing.eligible.includes(needsEnv), 'skill was eligible with its prerequisite absent');
  assert.deepEqual(missing.missingRequirements.find(item => item.name === needsEnv)?.missing?.env,
    [envKey], 'CLI did not report the missing prerequisite');
  const ready = await check({ [envKey]: 'local-fixture' });
  assert.ok(ready.eligible.includes(needsEnv), 'skill remained ineligible with its prerequisite present');
  assert.ok(!ready.missingRequirements.some(item => item.name === needsEnv),
    'CLI still reported the prerequisite as missing');
  assert.deepEqual(await Promise.all(inputs.map(path => readFile(path, 'utf8'))), snapshot,
    'skills check changed the config or fixture files');
  console.log(JSON.stringify({
    command, prerequisite: envKey, missing: missing.missingRequirements.find(item => item.name === needsEnv),
    ready: ready.eligible.filter(name => [available, needsEnv].includes(name)), inputsUnchanged: true,
  }));
} finally {
  await rm(root, { recursive: true, force: true });
  console.log(JSON.stringify({ phase: 'cleanup', scratchRemoved: true }));
}
console.log('VERIFY_OPENCLAW_SKILLS_CHECK_PASS');
