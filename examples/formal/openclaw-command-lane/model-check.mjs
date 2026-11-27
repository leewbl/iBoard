import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { copyFileSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const jar = '/opt/iboard-statechart/tla2tools.jar';
const java = '/opt/iboard-statechart/jre/bin/java';
const source = readFileSync('/app/src/process/command-queue.ts', 'utf8');
const guards = source.match(/^\s*state\.activeTasks\.size (<|<=) state\.maxConcurrent &&\s*$/gm) ?? [];
if (guards.length !== 1) throw new Error('Admission guard moved or changed. Review the model synchronization.');
const relaxed = guards[0].includes('<=');
if (createHash('sha256').update(readFileSync(jar)).digest('hex') !==
    '936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88') {
  throw new Error('The verifier JAR does not match TLC 1.7.4');
}

const root = mkdtempSync(join(tmpdir(), 'iboard-queue-model-'));
try {
  copyFileSync('/workspace/CommandLane.tla', join(root, 'CommandLane.tla'));
  copyFileSync(relaxed ? '/workspace/Overlap.cfg' : '/workspace/CommandLane.cfg', join(root, 'CommandLane.cfg'));
  const result = spawnSync(java, ['-XX:+UseParallelGC', '-cp', jar, 'tlc2.TLC', '-workers', '1', '-fp', '0',
    '-config', 'CommandLane.cfg', 'CommandLane.tla'], { cwd: root, encoding: 'utf8', timeout: 90_000 });
  if (result.error) throw result.error;
  process.stdout.write(result.stdout);
  process.stderr.write(result.stderr);
  const explored = Number(/(\d+) distinct states found/.exec(result.stdout)?.[1]);
  const check = { tool_version: 'TLC 1.7.4', explored, status: 'holds', counterexample: [] };
  if (result.status === 0 && result.stdout.includes('No error has been found')) {
    if (relaxed) throw new Error('The model failed to detect an admission guard that permits overlap');
  } else if (relaxed && result.status !== 0 && /Invariant SingleActive is violated\./.test(result.stdout)) {
    const states = Array.from(result.stdout.matchAll(/^State \d+: <(\w+)/gm), (match) => match[1]);
    if (states.join(',') !== 'Initial,Enqueue,Enqueue' || !/active = 2\b/.test(result.stdout)) {
      throw new Error('The TLC trace does not match this example’s code-level replay');
    }
    check.status = 'violated';
    check.counterexample = ['Enqueue command 1', 'Enqueue command 2',
      'Observe more than one active callback before releasing the running command'];
    process.exitCode = 1;
  } else {
    throw new Error('TLC did not yield a supported model result');
  }
  console.log('IBOARD_PROPERTY ' + JSON.stringify(check));
} finally {
  rmSync(root, { recursive: true, force: true });
}
