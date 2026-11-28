import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { setTimeout as sleep } from 'node:timers/promises';

const maxSourceBytes = 32 * 1024;
const objectGroup = 'iboard-browser-probe';
const methods = ['setup', 'observe', 'intervene', 'restore', 'cleanup'];
const stages = [
  ['baseline', null],
  ['intervention-1', 'intervene'],
  ['restored-1', 'restore'],
  ['intervention-2', 'intervene'],
  ['restored-2', 'restore'],
];

function validate(spec) {
  if (!spec || typeof spec !== 'object' || Array.isArray(spec)
    || typeof spec.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(spec.sha256)
    || !Number.isInteger(spec.samples) || spec.samples < 2 || spec.samples > 8
    || !Number.isInteger(spec.settle_ms) || spec.settle_ms < 0 || spec.settle_ms > 2_000
    || !Array.isArray(spec.metrics) || !spec.metrics.length || spec.metrics.length > 16
    || new Set(spec.metrics).size !== spec.metrics.length
    || spec.metrics.some(key => typeof key !== 'string' || !/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(key))) {
    throw new Error('Invalid browser probe request');
  }
}

async function readSource(expectedHash) {
  const input = await open('/workspace/browser-probe.js', constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const meta = await input.stat();
    if (!meta.isFile() || !meta.size || meta.size > maxSourceBytes) {
      throw new Error('Browser probe source file exceeds its limit or is empty');
    }
    const bytes = await input.readFile();
    if (bytes.length !== meta.size || createHash('sha256').update(bytes).digest('hex') !== expectedHash) {
      throw new Error('Browser probe source does not match the selected digest');
    }
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } finally {
    await input.close();
  }
}

// Run in the selected page. Return only the agreed scalar metrics to the driver.
async function invokeProbe(method, metrics) {
  const value = await this[method]();
  if (method !== 'observe') return true;
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Reflect.ownKeys(value).length !== metrics.length) {
    throw new Error('Invalid browser probe observation');
  }
  const result = Object.create(null);
  for (const key of metrics) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    const measurement = descriptor?.value;
    if (typeof measurement !== 'number' || !Number.isFinite(measurement) || Math.abs(measurement) > 1e12) {
      throw new Error('Invalid browser probe metric');
    }
    result[key] = measurement;
  }
  return result;
}

function isProbeObject(methodNames) {
  return methodNames.every(name => typeof this[name] === 'function');
}

async function runProbe({ page, session, send, checkOrigin, origin, spec, source }) {
  const mainFrame = page.mainFrame();
  const initialUrl = page.url();
  const contexts = new Map();
  let navigated = false;
  const onNavigate = frame => { if (frame === mainFrame) navigated = true; };
  const onCreated = ({ context }) => { contexts.set(context.uniqueId, context); };
  const onDestroyed = ({ executionContextUniqueId, executionContextId }) => {
    if (executionContextUniqueId) contexts.delete(executionContextUniqueId);
    else for (const context of contexts.values()) {
      if (context.id === executionContextId) contexts.delete(context.uniqueId);
    }
  };
  const onCleared = () => contexts.clear();
  page.on('framenavigated', onNavigate);
  session.on('Runtime.executionContextCreated', onCreated);
  session.on('Runtime.executionContextDestroyed', onDestroyed);
  session.on('Runtime.executionContextsCleared', onCleared);

  let pinned, objectId, interventionActive = false, completed = false;
  const assertPage = () => {
    if (navigated || page.isClosed() || page.mainFrame() !== mainFrame || page.url() !== initialUrl
      || (pinned && contexts.get(pinned.uniqueId) !== pinned)) {
      throw new Error('Browser probe left its original page context');
    }
    checkOrigin(page);
  };
  const evaluate = (expression, returnByValue) => send(session, 'Runtime.evaluate', {
    expression, uniqueContextId: pinned.uniqueId, objectGroup, returnByValue, silent: true, timeout: 4_000,
  });
  const call = (fn, args) => send(session, 'Runtime.callFunctionOn', {
    functionDeclaration: fn.toString(), objectId, arguments: args.map(value => ({ value })),
    awaitPromise: true, returnByValue: true, silent: true,
  });
  async function invoke(method) {
    let response;
    try {
      response = await call(invokeProbe, [method, spec.metrics]);
    } catch {
      throw new Error(`Browser probe ${method} did not return`);
    }
    if (response.exceptionDetails || !response.result || !Object.hasOwn(response.result, 'value')) {
      throw new Error(`Browser probe ${method} failed or returned invalid metrics`);
    }
    return response.result.value;
  }
  async function mark(name) {
    assertPage();
    const response = await evaluate(`void performance.mark(${JSON.stringify('iboard:probe:' + name)})`, true);
    if (response.exceptionDetails) throw new Error('Browser probe could not record a trace marker');
    assertPage();
  }

  try {
    assertPage();
    const { frameTree } = await send(session, 'Page.getFrameTree');
    await send(session, 'Runtime.enable');
    const candidates = [...contexts.values()].filter(context => context.auxData?.isDefault === true
      && context.auxData.frameId === frameTree?.frame?.id && context.origin === origin);
    if (candidates.length !== 1 || typeof candidates[0].uniqueId !== 'string') {
      throw new Error('Browser probe could not identify the selected page context');
    }
    pinned = candidates[0];
    assertPage();
    const evaluated = await evaluate(`(\n${source}\n)`, false);
    if (evaluated.exceptionDetails || evaluated.result?.type !== 'object' || !evaluated.result.objectId) {
      throw new Error('Browser probe source did not evaluate to an object');
    }
    objectId = evaluated.result.objectId;
    assertPage();
    const valid = await call(isProbeObject, [methods]);
    if (valid.exceptionDetails || valid.result?.value !== true) {
      throw new Error('Browser probe object lacks its required methods');
    }
    assertPage();
    await invoke('setup');
    await mark('setup-complete');

    const phases = [];
    for (const [name, transition] of stages) {
      assertPage();
      if (transition === 'intervene') interventionActive = true;
      if (transition) await invoke(transition);
      if (transition === 'restore') interventionActive = false;
      await mark(name + ':start');
      const samples = [];
      for (let index = 0; index < spec.samples; index += 1) {
        if (spec.settle_ms) await sleep(spec.settle_ms);
        assertPage();
        samples.push(await invoke('observe'));
        assertPage();
      }
      phases.push({ name, samples });
      await mark(name + ':end');
    }
    await invoke('cleanup');
    await mark('cleanup-complete');
    completed = true;
    return { sha256: spec.sha256, cleanup_confirmed: true, phases };
  } finally {
    try {
      if (objectId && !completed) {
        if (interventionActive) await invoke('restore').catch(() => {});
        await invoke('cleanup').catch(() => {});
      }
      if (pinned) await send(session, 'Runtime.releaseObjectGroup', { objectGroup }).catch(() => {});
    } finally {
      page.off('framenavigated', onNavigate);
      session.off('Runtime.executionContextCreated', onCreated);
      session.off('Runtime.executionContextDestroyed', onDestroyed);
      session.off('Runtime.executionContextsCleared', onCleared);
    }
  }
}

export async function prepareBrowserProbe(spec) {
  validate(spec);
  const source = await readSource(spec.sha256);
  return (runtime) => runProbe({ ...runtime, spec, source });
}
