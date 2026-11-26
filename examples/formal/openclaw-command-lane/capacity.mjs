import { spawnSync } from 'node:child_process';
import { setImmediate as tick } from 'node:timers/promises';

function* schedules(enqueued = 0, settled = 0, events = []) {
  if (settled === 3) {
    yield events;
    return;
  }
  if (enqueued < 3) {
    yield* schedules(enqueued + 1, settled, [...events, `Enqueue command ${enqueued + 1}`]);
  }
  if (settled < enqueued) {
    for (const action of ['Resolve', 'Reject']) {
      yield* schedules(enqueued, settled + 1, [...events, `${action} command ${settled + 1}`]);
    }
  }
}

async function check() {
  const queue = await import('/app/src/process/command-queue.ts');
  const lane = 'session:probe-formal';
  const symptom = 'Observe more than one active callback before releasing the running command';
  let explored = 0;
  for (const events of schedules()) {
    explored++;
    queue.resetAllLanes();
    queue.setCommandLaneConcurrency(lane, 1);
    const active = new Set();
    const releases = new Map();
    const finished = [];
    const trace = [];
    for (const event of events) {
      trace.push(event);
      const [action, , id] = event.split(' ');
      if (action === 'Enqueue') {
        const deferred = Promise.withResolvers();
        releases.set(id, deferred);
        finished.push(queue.enqueueCommandInLane(lane, async () => {
          active.add(id);
          try {
            await deferred.promise;
          } finally {
            active.delete(id);
          }
        }).catch(() => {}));
      } else {
        if (!active.has(id)) {
          throw new Error(`Harness could not settle a command whose callback never started: ${event}`);
        }
        const deferred = releases.get(id);
        if (action === 'Reject') {
          deferred.reject(new Error('controlled command rejection'));
        } else {
          deferred.resolve();
        }
      }
      await tick();
      if (active.size > 1) {
        return { status: 'violated', explored, counterexample: [...trace, symptom] };
      }
    }
    await Promise.all(finished);
    if (active.size !== 0 || queue.getQueueSize(lane) !== 0) {
      throw new Error('Harness did not return the lane to idle');
    }
  }
  return { status: 'holds', explored, counterexample: [] };
}

if (process.env.IBOARD_QUEUE_LOADER === '1') {
  const result = await check();
  console.log('IBOARD_PROPERTY ' + JSON.stringify({ ...result, tool_version: `node ${process.version}` }));
  process.exitCode = Number(result.status === 'violated');
} else {
  const child = spawnSync(process.execPath, [
    '--import', '/app/node_modules/tsx/dist/loader.mjs', process.argv[1],
  ], { cwd: '/app', env: { ...process.env, IBOARD_QUEUE_LOADER: '1' }, stdio: 'inherit', timeout: 90_000 });
  if (child.error) throw child.error;
  process.exitCode = child.status ?? 2;
}
