import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

// This abstraction applies to one lane generation with a fixed, positive
// capacity and distinct task IDs. It does not model configuration changes or resetAllLanes().
const stateSource = readFileSync('/app/src/process/command-queue.state.ts', 'utf8');
const initializers = stateSource.match(/export function getLaneState\(lane: string\): LaneState \{[\s\S]*?\n\}/g);
if (!initializers || initializers.length !== 1 ||
    (initializers[0].match(/activeTasks:\s*new Map\(\),/g) ?? []).length !== 1 ||
    (initializers[0].match(/maxConcurrent:\s*getDefaultLaneConcurrency\(lane\),/g) ?? []).length !== 1) {
  throw new Error('Unrecognized lane initialization');
}
const source = readFileSync('/app/src/process/command-queue.ts', 'utf8');
const drain = source.match(/function drainLane\([\s\S]*?\n}\n\nfunction drainReadyCommandLane\(/g);
if (!drain || drain.length !== 1) throw new Error('Unrecognized drainLane source');
const body = drain[0];
const guards = [...body.matchAll(/state\.activeTasks\.size\s*(<=|<)\s*state\.maxConcurrent\s*&&/g)];
const adds = [...body.matchAll(/state\.activeTasks\.set\(taskId,\s*entry\.sessionTarget\);/g)];
const calls = [...body.matchAll(/entry\.task\(\)/g)];
const deletes = [...body.matchAll(/state\.activeTasks\.delete\(taskId\);/g)];
if (guards.length !== 1 || adds.length !== 1 || calls.length !== 1 || deletes.length !== 1 ||
    !(guards[0].index < adds[0].index && adds[0].index < calls[0].index && calls[0].index < deletes[0].index) ||
    !/const taskId = getQueueState\(\)\.nextTaskId\+\+;/.test(body) ||
    !/const taskGeneration = state\.generation;/.test(body) ||
    !/finally\s*\{[\s\S]*?if\s*\(taskGeneration === state\.generation\)\s*\{\s*state\.activeTasks\.delete\(taskId\);/.test(body) ||
    (body.match(/state\.activeTasks\.(?:set|delete|clear)\s*\(/g) ?? []).length !== 2) {
  throw new Error('Unrecognized admission or settlement source; review model synchronization');
}
const relaxed = guards[0][1] === '<=';
const version = spawnSync('z3', ['-version'], { encoding: 'utf8', timeout: 10_000 });
if (version.error || version.status !== 0 || !/^Z3 version 4\.13\.3(?:\s+-\s+\d+\s+bit)?\s*$/.test(version.stdout)) {
  throw new Error('Expected Z3 4.13.3');
}
const prefix = `(set-logic QF_LIA)\n(declare-const capacity Int)\n(declare-const active Int)\n(assert (> capacity 0))\n`;
const invariant = '(and (<= 0 active) (<= active capacity))';
const obligations = [
  ['initial', '(assert (= active 0))\n(assert (not ' + invariant + '))'],
  ['admit', `(assert ${invariant})\n(assert (${relaxed ? '<=' : '<'} active capacity))\n(assert (not (and (<= 0 (+ active 1)) (<= (+ active 1) capacity))))`],
  ['settle', `(assert ${invariant})\n(assert (> active 0))\n(assert (not (and (<= 0 (- active 1)) (<= (- active 1) capacity))))`],
];
function solve(name, assertions) {
  const result = spawnSync('z3', ['-in', '-smt2'], {
    input: `${prefix}${assertions}\n(check-sat)\n`, encoding: 'utf8', timeout: 10_000,
  });
  if (result.error || result.status !== 0 || result.stderr.trim() || !/^(sat|unsat)\s*$/.test(result.stdout)) {
    throw new Error(`Z3 failed or returned an unknown result for ${name}`);
  }
  const outcome = result.stdout.trim();
  console.log(`${name}: ${outcome}`);
  return outcome;
}
const outcomes = obligations.map(([name, assertions]) => solve(name, assertions));
let status = 'holds';
let counterexample = [];
if (outcomes.join(',') === 'unsat,unsat,unsat' && !relaxed) {
  // The invariant is inductive for an unbounded sequence of modeled steps.
} else if (relaxed && outcomes.join(',') === 'unsat,sat,unsat') {
  const witness = solve('concrete-witness', `(assert (= capacity 1))\n(assert (= active 1))\n(assert ${invariant})\n(assert (<= active capacity))\n(assert (> (+ active 1) capacity))`);
  if (witness !== 'sat') throw new Error('Expected concrete two-command witness');
  status = 'violated';
  counterexample = ['Enqueue command 1', 'Enqueue command 2',
    'Observe more than one active callback before releasing the running command'];
  process.exitCode = 1;
} else {
  throw new Error(`Unexpected proof obligations: ${outcomes.join(',')}`);
}
console.log('IBOARD_PROPERTY ' + JSON.stringify({ status, tool_version: 'Z3 4.13.3', explored: obligations.length, counterexample }));
