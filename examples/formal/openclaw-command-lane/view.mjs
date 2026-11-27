export const title = 'OpenClaw command lane';
export const colors = { ready: 'tan', waiting: 'yellow', active: 'blue', overlap: 'orange', done: 'teal' };

export function state({ active, waiting, settled }) {
  if (active > 1) return 'Multiple commands running';
  if (active === 1) return waiting ? 'Running with commands waiting' : 'One command running';
  return settled === 3 ? 'All commands settled' : 'Ready to receive a command';
}

export function group({ active, waiting, settled }) {
  if (active > 1) return 'overlap';
  if (waiting) return 'waiting';
  if (active) return 'active';
  return settled === 3 ? 'done' : 'ready';
}

export function step(action) {
  return {
    Enqueue: 'enqueue a command',
    Resolve: 'running command ends',
    Reject: 'running command ends',
    Settle: 'running command ends',
    Done: 'stay settled',
  }[action] ?? action;
}
