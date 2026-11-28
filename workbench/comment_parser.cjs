const { readFileSync } = require('node:fs');
const { createHash } = require('node:crypto');
const { extname } = require('node:path');

const { packagePath, files } = JSON.parse(readFileSync('/workspace/input.json', 'utf8'));
const { parse } = require(packagePath);
const ignored = new Set(['start', 'end', 'loc', 'range', 'extra', 'leadingComments',
  'innerComments', 'trailingComments', 'comments', 'tokens', 'errors']);
const result = {};
for (const [name, source] of Object.entries(files)) {
  const extension = extname(name);
  const plugins = [['decorators', { decoratorsBeforeExport: true }]];
  if (['.ts', '.tsx', '.mts', '.cts'].includes(extension)) {
    plugins.push(['typescript', { dts: /\.d\.[cm]?ts$/.test(name) }]);
  }
  if (['.tsx', '.jsx', '.js'].includes(extension)) {
    plugins.push('jsx');
  }
  const tree = parse(source, { sourceType: 'unambiguous', plugins });
  const structural = JSON.stringify(tree.program, (key, value) => ignored.has(key) ? undefined : value);
  result[name] = {
    fingerprint: createHash('sha256').update(structural).digest('hex'),
    comments: tree.comments.map(comment => ({
      start: Array.from(source.slice(0, comment.start)).length,
      end: Array.from(source.slice(0, comment.end)).length,
      line: comment.loc.start.line,
      end_line: comment.loc.end.line,
    })),
  };
}
process.stdout.write(JSON.stringify(result));
