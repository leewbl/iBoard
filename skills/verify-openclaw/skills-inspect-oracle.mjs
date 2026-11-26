import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const name = 'iboard-fixture-inspect';
const missingName = 'iboard-fixture-absent';
const description = 'Local iBoard skill metadata inspection fixture.';
const root = mkdtempSync('/tmp/iboard-skills-oracle-');
let observation;

function openclaw(args, expectedExit, env) {
  const invocation = ['skills', ...args, '--agent', 'main', '--json'];
  const child = spawnSync(process.execPath, [
    '--import', '/app/node_modules/tsx/dist/loader.mjs', '/app/src/entry.ts', ...invocation,
  ], {
    cwd: '/app',
    env,
    encoding: 'utf8',
    timeout: 75_000,
    killSignal: 'SIGKILL',
    maxBuffer: 512 * 1024,
  });
  if (child.error) throw child.error;
  assert.equal(child.signal, null, 'OpenClaw was interrupted');
  assert.equal(child.status, expectedExit, `${invocation.join(' ')} returned an unexpected exit status`);
  return { command: invocation, exitCode: child.status, result: JSON.parse(child.stdout) };
}

try {
  const home = join(root, 'home');
  const state = join(root, 'state');
  const workspace = join(root, 'workspace');
  const skillDirectory = join(workspace, 'skills', name);
  const skillFile = join(skillDirectory, 'SKILL.md');
  const configFile = join(state, 'openclaw.json');
  const config = Buffer.from(JSON.stringify({
    agents: { defaults: { workspace } }, plugins: { enabled: false }, skills: { allowBundled: [] },
  }));
  const skill = Buffer.from(`---\nname: ${name}\ndescription: ${description}\n---\n\nLocal inspection fixture.\n`);
  const env = {
    PATH: process.env.PATH, HOME: home, OPENCLAW_HOME: home,
    OPENCLAW_STATE_DIR: state, OPENCLAW_CONFIG_PATH: configFile, TMPDIR: root,
  };

  mkdirSync(home);
  mkdirSync(state);
  mkdirSync(skillDirectory, { recursive: true });
  writeFileSync(configFile, config, { mode: 0o600 });
  writeFileSync(skillFile, skill);

  const listed = openclaw(['list'], 0, env);
  assert.equal(listed.result.workspaceDir, workspace, 'list resolved a different workspace');
  const rows = listed.result.skills;
  assert.ok(Array.isArray(rows), 'list did not return a skills array');
  assert.ok(rows.every(row => typeof row?.name === 'string'), 'list contained malformed skill rows');
  const row = rows.find(skill => skill.name === name);
  if (row === undefined) {
    observation = {
      issue: 'configured-skill-missing', expected: name,
      listed: { ...listed, result: { workspaceDir: workspace, names: rows.map(skill => skill.name) } },
    };
  } else {
    assert.ok(!rows.some(skill => skill.name === missingName), 'list reported the absent skill');
    assert.equal(row.description, description);
    assert.equal(row.source, 'openclaw-workspace');
    assert.equal(row.eligible, true);

    const details = openclaw(['info', name], 0, env);
    assert.equal(details.result.name, row.name);
    assert.equal(details.result.description, row.description);
    assert.equal(details.result.source, row.source);
    assert.equal(details.result.eligible, row.eligible);
    assert.equal(details.result.filePath, skillFile, 'info resolved a different file');

    const absent = openclaw(['info', missingName], 1, env);
    assert.equal(absent.result.ok, false);
    assert.equal(absent.result.skill, missingName);
    assert.equal(absent.result.error?.type, 'cli_error');
    assert.equal(absent.result.error?.message, `Skill "${missingName}" not found.`);

    observation = { listed: { ...listed, result: { workspaceDir: workspace, skill: row } }, details, absent };
  }

  assert.deepEqual(readFileSync(configFile), config, 'OpenClaw changed the configuration');
  assert.deepEqual(readFileSync(skillFile), skill, 'OpenClaw changed the fixture skill file');
} finally {
  rmSync(root, { recursive: true, force: true });
  assert.throws(() => statSync(root), { code: 'ENOENT' });
}

console.log(JSON.stringify({ ...observation, inputsUnchanged: true, scratchRemoved: true }));
if (observation.issue === 'configured-skill-missing') {
  console.log('VERIFY_OPENCLAW_SKILLS_ORACLE_FAIL');
  process.exitCode = 1;
} else {
  console.log('VERIFY_OPENCLAW_SKILLS_ORACLE_PASS');
}
