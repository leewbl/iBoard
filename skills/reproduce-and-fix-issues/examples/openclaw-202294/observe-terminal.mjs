import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const root = mkdtempSync('/tmp/iboard-legacy-terminal-');
const settings = join(root, 'openclaw.json');
const name = 'iboard-legacy-terminal';
const folder = join(root, 'skills', name);
const skill = join(folder, 'SKILL.md');
const fixture = `---\nname: ${name}\ndescription: Legacy terminal fixture.\nmetadata: {"clawdbot":{"emoji":"T"}}\n---\n`;
const command = ['skills', 'check', '--agent', 'main'];
let absent;

try {
  mkdirSync(folder, { recursive: true });
  writeFileSync(settings, JSON.stringify({ agents: { defaults: { workspace: root } },
    plugins: { enabled: false }, skills: { allowBundled: [] } }));
  writeFileSync(skill, fixture);
  const result = spawnSync(process.execPath,
    ['--import', '/app/node_modules/tsx/dist/loader.mjs', '/app/src/entry.ts', ...command], {
      cwd: '/app', encoding: 'utf8', timeout: 75_000, maxBuffer: 256 * 1024,
      env: { PATH: process.env.PATH, HOME: root, OPENCLAW_HOME: root, OPENCLAW_STATE_DIR: root,
        OPENCLAW_CONFIG_PATH: settings, TMPDIR: root, NO_COLOR: '1' },
    });
  assert.equal(result.status, 0, 'skills check must exit successfully');
  const lines = result.stdout.split(/\r?\n/).map(line => line.trim());
  assert.ok(lines.some(line => line.includes(name)), 'skills check did not discover the fixture');
  const warning = lines.findIndex(line => line.includes('Legacy metadata.clawdbot is ignored:'));
  absent = warning === -1;
  if (!absent) {
    assert.equal(lines[warning + 1], name);
    assert.ok(lines[warning + 2]?.includes('rename the metadata.clawdbot block to metadata.openclaw'));
  }
  assert.equal(readFileSync(skill, 'utf8'), fixture);
  console.log(JSON.stringify({ command, eligibleFixture: name,
    warningLines: absent ? [] : lines.slice(warning, warning + 3), inputUnchanged: true }));
} finally {
  rmSync(root, { recursive: true, force: true });
}
console.log(absent ? 'OPENCLAW_LEGACY_TERMINAL_FAIL' : 'OPENCLAW_LEGACY_TERMINAL_PASS');
process.exitCode = absent ? 1 : 0;
