import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import { join } from 'node:path';
import { promisify } from 'node:util';

const execute = promisify(execFile);
const command = ['config', 'validate', '--json'];
const root = await mkdtemp('/tmp/iboard-config-validate-');
const home = join(root, 'home');
const state = join(root, 'state');
const config = join(state, 'openclaw.json');
const env = {
  PATH: process.env.PATH, HOME: home, OPENCLAW_HOME: home,
  OPENCLAW_STATE_DIR: state, OPENCLAW_CONFIG_PATH: config, TMPDIR: root,
};

async function freePort(port = 0) {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', resolve);
  });
  const bound = server.address().port;
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  return bound;
}

async function validate(port) {
  const contents = JSON.stringify({ gateway: { mode: 'local', port }, plugins: { enabled: false } });
  await writeFile(config, contents, { mode: 0o600 });
  let exitCode = 0;
  let stdout;
  try {
    ({ stdout } = await execute(process.execPath, [
      '--import', '/app/node_modules/tsx/dist/loader.mjs', '/app/src/entry.ts', ...command,
    ], {
      cwd: '/app', env, timeout: 75_000, killSignal: 'SIGKILL', maxBuffer: 512 * 1024,
    }));
  } catch (error) {
    if (!Number.isInteger(error.code) || error.killed || error.signal) throw error;
    exitCode = error.code;
    stdout = error.stdout;
  }
  assert.equal(await readFile(config, 'utf8'), contents, 'validation changed the configuration');
  const result = JSON.parse(stdout);
  assert.equal(result.path, config, 'CLI resolved a different configuration file');
  return { exitCode, result };
}

try {
  await mkdir(home);
  await mkdir(state);
  const port = await freePort();
  const accepted = await validate(port);
  assert.equal(accepted.exitCode, 0, 'valid configuration returned a nonzero exit');
  assert.equal(accepted.result.valid, true, 'CLI did not accept the valid configuration');
  await freePort(port);

  const rejected = await validate('not-a-port');
  assert.equal(rejected.exitCode, 1, 'invalid configuration returned an unexpected exit status');
  assert.equal(rejected.result.valid, false, 'CLI did not reject the invalid configuration');
  const issue = rejected.result.issues?.find(item => item.path === 'gateway.port');
  assert.ok(issue?.message, 'CLI did not explain the invalid gateway.port value');
  await freePort(port);

  console.log(JSON.stringify({
    command, accepted, rejected: { exitCode: rejected.exitCode, valid: rejected.result.valid, issue },
    inputsUnchanged: true, loopbackPortFreeAfterEachCheck: true,
  }));
} finally {
  await rm(root, { recursive: true, force: true });
  await assert.rejects(stat(root), { code: 'ENOENT' });
  console.log(JSON.stringify({ phase: 'cleanup', scratchRemoved: true }));
}
console.log('VERIFY_OPENCLAW_CONFIG_VALIDATE_PASS');
