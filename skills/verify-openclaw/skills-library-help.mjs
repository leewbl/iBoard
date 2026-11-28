import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { promisify } from 'node:util';

const execute = promisify(execFile);
const entry = ['--import', '/app/node_modules/tsx/dist/loader.mjs', '/app/src/entry.ts'];
const expected = [
  'attach', 'create', 'detach', 'disable', 'enable', 'import', 'list', 'read',
  'refresh', 'remove', 'rollback', 'share', 'transfer', 'unshare', 'update',
];
const root = await mkdtemp('/tmp/iboard-skills-library-help-');
const home = join(root, 'home');
const state = join(root, 'state');
const config = join(state, 'openclaw.json');
const contents = JSON.stringify({ plugins: { enabled: false }, update: { checkOnStart: false } });
const env = {
  PATH: process.env.PATH, HOME: home, OPENCLAW_HOME: home, OPENCLAW_STATE_DIR: state,
  OPENCLAW_CONFIG_PATH: config, TMPDIR: root, NO_COLOR: '1', OPENCLAW_SUPPRESS_HELP_BANNER: '1',
  GOMAXPROCS: '2', UV_THREADPOOL_SIZE: '2',
};

async function cli(args) {
  const { stdout } = await execute(process.execPath, [...entry, 'skills', 'library', ...args], {
    cwd: '/app', env, timeout: 75_000, killSignal: 'SIGKILL', maxBuffer: 256 * 1024,
  });
  return stdout;
}

function names(stdout) {
  const lines = stdout.split(/\r?\n/u);
  const heading = lines.indexOf('Commands:');
  assert.ok(heading >= 0, 'CLI help did not contain a Commands section');
  const names = [];
  for (const line of lines.slice(heading + 1)) {
    if (!line.trim()) break;
    const name = /^ {2}([a-z][a-z0-9-]*)(?:\s|$)/u.exec(line)?.[1];
    if (name && name !== 'help') names.push(name);
  }
  return names.sort();
}

try {
  await mkdir(home);
  await mkdir(state);
  await writeFile(config, contents, { mode: 0o600 });
  const explicitHelp = await cli(['--help']);
  const bareHelp = await cli([]);
  const readHelp = await cli(['read', '--help']);
  assert.deepEqual(names(explicitHelp), expected, 'Library help must match the source inventory');
  assert.deepEqual(names(bareHelp), expected, 'Bare library command must expose the same help');
  assert.match(readHelp, /Usage: openclaw skills library read \[options\] <skill-id>/u);
  assert.match(readHelp, /--revision <hash>/u);
  assert.match(readHelp, /--session <key>/u);
  assert.equal(await readFile(config, 'utf8'), contents, 'Help changed the isolated configuration');
  console.log(JSON.stringify({ commands: expected, explicitHelp, bareHelp, readHelp, configUnchanged: true }));
} finally {
  await rm(root, { recursive: true, force: true });
}

console.log('VERIFY_OPENCLAW_SKILLS_LIBRARY_HELP_PASS');
