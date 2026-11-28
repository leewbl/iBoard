import { readFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { setImmediate as tick } from 'node:timers/promises';

async function replay(path) {
  const steps = JSON.parse(await readFile(path, 'utf8'));
  const symptom = 'Observe more than one active callback before releasing the running command';
  if (!Array.isArray(steps) || steps.at(-1) !== symptom) {
    throw new Error('Unsupported command-lane counterexample');
  }
  const { enqueueCommandInLane, getCommandLaneSnapshot, setCommandLaneConcurrency } =
    await import('/app/src/process/command-queue.ts');
  const lane = 'session:probe-independent-formal';
  setCommandLaneConcurrency(lane, 1);
  const waiters = new Map();
  const running = new Set();
  let peak = 0;
  let overlap = false;
  for (const step of steps.slice(0, -1)) {
    const match = /^(Enqueue|Resolve|Reject) command ([1-3])$/.exec(step);
    if (!match) throw new Error(`Unknown witness action: ${step}`);
    const [, action, id] = match;
    if (action === 'Enqueue') {
      const pending = Promise.withResolvers();
      waiters.set(id, pending);
      void enqueueCommandInLane(lane, async () => {
        running.add(id);
        peak = Math.max(peak, running.size);
        try {
          await pending.promise;
        } finally {
          running.delete(id);
        }
      }).catch(() => {});
    } else if (action === 'Resolve') {
      waiters.get(id).resolve();
    } else {
      waiters.get(id).reject(new Error('independent controlled rejection'));
    }
    await tick();
    overlap ||= getCommandLaneSnapshot(lane).activeCount > 1;
  }
  for (const pending of waiters.values()) pending.resolve();
  await tick();
  if ((peak > 1) !== overlap) {
    throw new Error('The live callbacks and the reported active count disagreed');
  }
  console.log(`Peak overlapping callbacks: ${peak}`);
  return { status: overlap ? 'violated' : 'holds', explored: 1, counterexample: overlap ? steps : [] };
}

if (process.env.IBOARD_QUEUE_REPLAY === '1') {
  const result = await replay(process.argv[2]);
  console.log('IBOARD_PROPERTY ' + JSON.stringify({ ...result, tool_version: `node ${process.version}` }));
  process.exitCode = Number(result.status === 'violated');
} else {
  const child = spawnSync(process.execPath, [
    '--import', '/app/node_modules/tsx/dist/loader.mjs', process.argv[1], process.argv[2],
  ], { cwd: '/app', env: { ...process.env, IBOARD_QUEUE_REPLAY: '1' }, stdio: 'inherit', timeout: 90_000 });
  if (child.error) throw child.error;
  process.exitCode = child.status ?? 2;
}
