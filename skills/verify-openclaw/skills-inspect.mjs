import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { promisify } from 'node:util';

const execute = promisify(execFile);
const entry = ['--import', '/app/node_modules/tsx/dist/loader.mjs', '/app/src/entry.ts'];
const fixture = 'iboard-fixture-inspect';
const absent = 'iboard-fixture-absent';
const description = 'Local iBoard skill metadata inspection fixture.';
const root = await mkdtemp('/tmp/iboard-skills-inspect-');
const home = join(root, 'home');
const state = join(root, 'state');
const workspace = join(root, 'workspace');
const config = join(state, 'openclaw.json');
const skillFile = join(workspace, 'skills', fixture, 'SKILL.md');
const env = {
  PATH: process.env.PATH, HOME: home, OPENCLAW_HOME: home,
  OPENCLAW_STATE_DIR: state, OPENCLAW_CONFIG_PATH: config, TMPDIR: root,
};
let missingFromListing = false;

async function run(args, expectedExit) {
  const command = ['skills', ...args, '--agent', 'main', '--json'];
  let exitCode = 0;
  let stdout;
  try {
    ({ stdout } = await execute(process.execPath, [...entry, ...command], {
      cwd: '/app', env, timeout: 75_000, killSignal: 'SIGKILL', maxBuffer: 512 * 1024,
    }));
  } catch (error) {
    if (!Number.isInteger(error.code) || error.killed || error.signal) throw error;
    exitCode = error.code;
    stdout = error.stdout;
  }
  assert.equal(exitCode, expectedExit, 'skills command returned an unexpected exit status');
  return { command, exitCode, result: JSON.parse(stdout) };
}

try {
  await mkdir(home);
  await mkdir(state);
  await mkdir(dirname(skillFile), { recursive: true });
  const configContents = JSON.stringify({
    agents: { defaults: { workspace } }, plugins: { enabled: false }, skills: { allowBundled: [] },
  });
  const skillContents = `---\nname: ${fixture}\ndescription: ${description}\n---\n\nLocal inspection fixture.\n`;
  await writeFile(config, configContents, { mode: 0o600 });
  await writeFile(skillFile, skillContents);

  const listed = await run(['list'], 0);
  assert.equal(listed.result.workspaceDir, workspace, 'list resolved a different workspace');
  assert.ok(Array.isArray(listed.result.skills), 'list did not return a skills array');
  const row = listed.result.skills.find(skill => skill.name === fixture);
  if (!row) {
    assert.equal(await readFile(config, 'utf8'), configContents);
    assert.equal(await readFile(skillFile, 'utf8'), skillContents);
    missingFromListing = true;
    throw new Error('observed fixture absent from list');
  }
  assert.ok(!listed.result.skills.some(skill => skill.name === absent), 'list reported the absent skill');
  assert.equal(row.description, description);
  assert.equal(row.source, 'openclaw-workspace');
  assert.equal(row.eligible, true);

  const details = await run(['info', fixture], 0);
  assert.equal(details.result.name, row.name);
  assert.equal(details.result.description, row.description);
  assert.equal(details.result.source, row.source);
  assert.equal(details.result.eligible, row.eligible);
  assert.equal(details.result.filePath, skillFile, 'info resolved a different file');

  const missing = await run(['info', absent], 1);
  assert.equal(missing.result.ok, false);
  assert.equal(missing.result.skill, absent);
  assert.equal(missing.result.error?.type, 'cli_error');
  assert.equal(missing.result.error?.message, `Skill "${absent}" not found.`);

  assert.equal(await readFile(config, 'utf8'), configContents, 'skills commands changed the configuration');
  assert.equal(await readFile(skillFile, 'utf8'), skillContents, 'skills commands changed the fixture skill');
  console.log(JSON.stringify({
    listed: { ...listed, result: { workspaceDir: listed.result.workspaceDir, skill: row } },
    details, missing, inputsUnchanged: true,
  }));
} catch (error) {
  if (!missingFromListing) throw error;
  console.log(JSON.stringify({ observation: 'fixture absent from successful skills list', fixture }));
} finally {
  await rm(root, { recursive: true, force: true });
  await assert.rejects(stat(root), { code: 'ENOENT' });
  console.log(JSON.stringify({ phase: 'cleanup', scratchRemoved: true }));
}
if (missingFromListing) {
  console.log('VERIFY_OPENCLAW_SKILLS_INSPECT_FAIL');
  process.exitCode = 1;
} else {
  console.log('VERIFY_OPENCLAW_SKILLS_INSPECT_PASS');
}
