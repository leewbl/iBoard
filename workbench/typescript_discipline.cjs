'use strict';

// Run by iBoard in a read-only, offline container whose /app matches a Git commit.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const root = '/app';
const input = JSON.parse(fs.readFileSync('/workspace/input.json', 'utf8'));
const implementation = require.resolve(input.compiler);
const ts = require(implementation);
const config = path.resolve(root, input.project);
const read = ts.readConfigFile(config, ts.sys.readFile);
const configResult = read.error ? null : ts.parseJsonConfigFileContent(
  read.config, ts.sys, path.dirname(config), {noEmit: true, noCheck: false}, config,
);
const program = configResult && configResult.fileNames.length > 0 ? ts.createProgram({
  rootNames: configResult.fileNames,
  options: configResult.options,
  projectReferences: configResult.projectReferences,
}) : null;

function relative(file) {
  if (!file) return null;
  return path.relative(root, file).split(path.sep).join('/');
}

function diagnostic(item) {
  const position = item.file && item.start !== undefined
    ? item.file.getLineAndCharacterOfPosition(item.start)
    : null;
  return {
    code: item.code,
    path: item.file ? relative(item.file.fileName) : null,
    line: position ? position.line + 1 : null,
    message: ts.flattenDiagnosticMessageText(item.messageText, ' ').slice(0, 400),
  };
}

function scope(node, source) {
  const parents = [];
  for (let parent = node.parent; parent; parent = parent.parent) {
    if (parent.name && (ts.isIdentifier(parent.name) || ts.isStringLiteral(parent.name))) {
      parents.push(parent.name.getText(source));
    }
  }
  return parents.reverse().join('.').slice(0, 160);
}

function inspect(source) {
  const findings = [];
  function add(rule, node) {
    const start = node.getStart(source);
    const {line, character} = source.getLineAndCharacterOfPosition(start);
    const excerpt = node.getText(source);
    findings.push({
      rule, line: line + 1, column: character + 1,
      scope: scope(node, source),
      signature: crypto.createHash('sha256').update(excerpt).digest('hex'),
      text: excerpt.split(/\s+/).join(' ').slice(0, 220),
    });
  }
  function literalConst(node) {
    return ts.isTypeReferenceNode(node.type) && node.type.typeName.getText(source) === 'const';
  }
  function predicateBody(node) {
    if (!node.type || !ts.isTypePredicateNode(node.type) || !node.body) return;
    if (node.name && !/^(?:is|has)[A-Z]/.test(node.name.getText(source))) {
      add('guard-name', node.type);
    }
    if (node.body.kind === ts.SyntaxKind.TrueKeyword ||
        (ts.isBlock(node.body) && node.body.statements.length === 1 &&
          ts.isReturnStatement(node.body.statements[0]) &&
          node.body.statements[0].expression?.kind === ts.SyntaxKind.TrueKeyword)) {
      add('unconditional-type-predicate', node.type);
    }
  }
  function walk(node) {
    if (node.kind === ts.SyntaxKind.AnyKeyword) add('explicit-any', node);
    if (ts.isNonNullExpression(node)) add('non-null-assertion', node);
    if ((ts.isAsExpression(node) || ts.isTypeAssertionExpression(node)) && !literalConst(node)) {
      add('type-assertion', node);
    }
    if (ts.isFunctionLike(node)) predicateBody(node);
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) &&
        ts.isIdentifier(node.expression.expression) && node.expression.expression.text === 'console' &&
        ['debug', 'info', 'log', 'warn', 'error'].includes(node.expression.name.text)) {
      add('console-telemetry', node.expression);
    }
    ts.forEachChild(node, walk);
  }
  walk(source);
  for (const directive of source.commentDirectives || []) {
    const {pos, end} = directive.range;
    const {line, character} = source.getLineAndCharacterOfPosition(pos);
    const excerpt = source.text.slice(pos, end);
    findings.push({
      rule: 'compiler-suppression', line: line + 1, column: character + 1,
      scope: '', signature: crypto.createHash('sha256').update(excerpt).digest('hex'),
      text: excerpt.split(/\s+/).join(' ').slice(0, 220),
    });
  }
  return findings;
}

const failures = read.error ? [read.error] : configResult.errors;
const diagnostics = [...failures, ...(program ? ts.getPreEmitDiagnostics(program) : [])]
  .filter(item => item.category === ts.DiagnosticCategory.Error);
const files = input.files.map(file => {
  const name = path.resolve(root, file);
  const body = fs.readFileSync(name);
  const compiled = program?.getSourceFile(name);
  const source = compiled || ts.createSourceFile(name, body.toString('utf8'), ts.ScriptTarget.Latest, true);
  return {
    path: file,
    sha256: crypto.createHash('sha256').update(body).digest('hex'),
    compiled: Boolean(compiled),
    findings: inspect(source),
  };
});
const findingCount = files.reduce((total, file) => total + file.findings.length, 0);
const options = configResult?.options || {};
const strictChecks = Object.fromEntries([
  'alwaysStrict', 'strictNullChecks', 'strictFunctionTypes', 'strictBindCallApply',
  'strictPropertyInitialization', 'useUnknownInCatchVariables', 'noImplicitAny', 'noImplicitThis',
].map(name => [name, typeof ts.getStrictOptionValue === 'function'
  ? Boolean(ts.getStrictOptionValue(options, name))
  : Boolean(options[name] ?? options.strict)]));
for (const name of ['exactOptionalPropertyTypes', 'noUncheckedIndexedAccess', 'noImplicitReturns', 'noFallthroughCasesInSwitch']) {
  strictChecks[name] = Boolean(options[name]);
}
const result = {
  compiler: ts.version,
  compilerSha256: crypto.createHash('sha256').update(fs.readFileSync(implementation)).digest('hex'),
  strictChecks,
  project: input.project,
  configOk: Boolean(program && failures.length === 0),
  rootCount: program ? program.getRootFileNames().length : 0,
  diagnosticCount: diagnostics.length,
  diagnostics: diagnostics.slice(0, 80).map(diagnostic),
  files: files.map(file => ({...file, findings: file.findings.slice(0, 80)})),
  truncated: diagnostics.length > 80 || findingCount > 160 ||
    files.some(file => file.findings.length > 80),
};
process.stdout.write(JSON.stringify(result) + '\n');
