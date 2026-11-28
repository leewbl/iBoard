import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { copyFileSync, existsSync, lstatSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const tools = '/opt/iboard-statechart';
const jar = join(tools, 'tla2tools.jar');
const java = join(tools, 'jre/bin/java');
const tlcSha256 = '936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88';
const [renderMode, ...args] = process.argv.slice(2);

const palette = {
  blue: ['#c3e0ff', '#1b3857'], orange: ['#f0d3bf', '#573117'], purple: ['#e3d5ff', '#3c2e57'],
  yellow: ['#f0dcbf', '#534018'], teal: ['#bfe3df', '#174641'], tan: ['#e5d9ce', '#3d3127'],
  pink: ['#ffcbe6', '#57233e'],
};

function chartStyle() {
  const marks = (error, warning, path, loop) => `
    .pele .breaks rect { fill: ${error[0]} !important; stroke: ${error[1]} !important; stroke-width: 2.5px; }
    .pele .breaks text { fill: ${error[1]} !important; }
    .pele .mayBreak rect { fill: ${warning[0]} !important; stroke: ${warning[1]} !important; stroke-width: 2.5px; }
    .pele .mayBreak text { fill: ${warning[1]} !important; }
    .pele .bugPath rect { stroke: ${path} !important; stroke-width: 2.5px; }
    .pele .repeats rect { stroke: ${loop} !important; stroke-width: 2.5px; stroke-dasharray: 6 3; }`;
  const groups = (index) => Object.entries(palette)
    .map(([key, values]) => `.pele .${key} rect { fill: ${values[index]}; }`).join('\n');
  return `<style>
    ${groups(0)}
    ${marks(['#ffecee', '#cd2834'], ['#fff3e6', '#a34c00'], '#1680f3', '#862bb5')}
    @media (prefers-color-scheme: dark) {
      .pele { --pele-bg: #1f1f1f; --pele-fg: #f5f5f5; --pele-muted: #acacac;
        --pele-line: #939393; --pele-surface: #121212; --pele-surface-alt: #2c2c2c; --pele-border: #4e4e4e; }
      ${groups(1)}
      ${marks(['#2e2021', '#ff6463'], ['#2e2619', '#ffa257'], '#66b0ff', '#c994ef')}
    }
  </style>`;
}

async function renderChart(file) {
  const pele = join(tools, 'pele');
  const meta = JSON.parse(readFileSync(join(pele, 'package.json'), 'utf8'));
  if (meta.version !== '1.9.0') throw new Error('requires Pele 1.9.0');
  const { render } = await import(join(pele, 'dist/pele.js'));
  const { svg } = render(readFileSync(file, 'utf8'), {
    limit: 1_000_000, outputLimit: 8_000_000, maxEdges: 10_000,
  });
  writeFileSync('/artifacts/chart.svg', svg.replace(/<svg[^>]*>/, (tag) => tag + chartStyle()), { flag: 'wx' });
  return meta.version;
}

const temporary = mkdtempSync(join(tmpdir(), 'iboard-statechart-'));
try {
  const hash = createHash('sha256').update(readFileSync(jar)).digest('hex');
  if (hash !== tlcSha256) throw new Error('TLC JAR must match the pinned 1.7.4 checksum');
  const version = spawnSync(java, ['-version'], { encoding: 'utf8', timeout: 10_000 });
  if (version.status !== 0 || !version.stderr) throw new Error('cannot run the prepared Java runtime');
  const output = join(temporary, 'output');
  const run = spawnSync(process.execPath, [
    '--disable-warning=ExperimentalWarning', '/workspace/engine/statechart.ts',
    ...args, '--out', output,
  ], { env: { ...process.env, TLA2TOOLS: jar, JAVA: java }, stdio: ['ignore', 'ignore', 'inherit'] });
  if (run.status !== 0 && run.status !== 1) {
    throw new Error(`statechart did not complete (exit ${run.status ?? run.signal ?? 'unknown'})`);
  }
  for (const name of readdirSync(output)) {
    const file = join(output, name);
    if (!lstatSync(file).isFile()) throw new Error(`statechart produced a nonregular file: ${name}`);
    copyFileSync(file, join('/artifacts', name));
  }
  const chart = join(output, 'chart.mmd');
  let renderer = existsSync(chart) ? 'skipped' : 'no-chart';
  if (renderMode === 'svg' && existsSync(chart)) {
    try {
      renderer = `Pele ${await renderChart(chart)}`;
    } catch (error) {
      renderer = 'failed';
      writeFileSync('/artifacts/render-error.txt', String(error.message || error).slice(0, 4000) + '\n');
    }
  }
  writeFileSync('/artifacts/runtime.json', JSON.stringify({
    node: process.version, java: version.stderr.trim(), tlc: '1.7.4', tlc_sha256: hash,
    verdict: run.status, renderer,
  }, null, 2) + '\n', { flag: 'wx' });
  process.exitCode = run.status;
} catch (error) {
  console.error(String(error.message || error));
  process.exitCode = 2;
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
