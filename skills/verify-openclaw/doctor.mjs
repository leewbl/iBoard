import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { promisify } from 'node:util';

const root = await mkdtemp('/tmp/iboard-openclaw-doctor-');
try {
  const home = join(root, 'home');
  const state = join(root, 'state');
  await Promise.all([mkdir(home), mkdir(state)]);
  const expected = JSON.parse(await readFile('/app/package.json', 'utf8')).version;
  const { stdout } = await promisify(execFile)(process.execPath, [
    '--import', '/app/node_modules/tsx/dist/loader.mjs', '/app/src/entry.ts', '--version',
  ], {
    cwd: '/app',
    env: {
      PATH: process.env.PATH, HOME: home, OPENCLAW_HOME: home, OPENCLAW_STATE_DIR: state,
      OPENCLAW_CONFIG_PATH: join(state, 'openclaw.json'), TMPDIR: root,
    },
    timeout: 25_000,
    killSignal: 'SIGKILL',
    maxBuffer: 65_536,
  });
  assert.ok(stdout.includes(expected), 'OpenClaw CLI version did not match this image');
  console.log(JSON.stringify({ command: ['openclaw', '--version'], expected, cli: stdout.trim() }));
} finally {
  await rm(root, { recursive: true, force: true });
}
console.log('VERIFY_OPENCLAW_DOCTOR_PASS');
