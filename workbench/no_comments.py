"""Review comment-only edits and report scoped workarounds without changing application code."""

import argparse
import ast
import difflib
import io
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
import tokenize

from model import codex_review
from runner.sandbox import SandboxError, run_sandbox
from workbench import autopilot, cli, interrogate_snapshot as snapshot, investigate, prototype
from workbench.automate_me import SHA, _digest, _file, _keep, _read, _require


SOURCE = 'skills/no-comments/SKILL.md'
JAVASCRIPT = {'.js', '.mjs', '.cjs', '.jsx', '.ts', '.mts', '.cts', '.tsx'}
SUPPORTED = JAVASCRIPT | {'.py'}
PARSER = Path(__file__).with_name('comment_parser.cjs')
ERRORS = (*prototype.ERRORS, snapshot.SnapshotError, SandboxError, ValueError, KeyError, TypeError, SyntaxError, RuntimeError,
          tokenize.TokenError, codex_review.ReviewError, investigate.InvestigationError)
HUNK = re.compile(r'(?m)^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')
SUPPRESS = re.compile(r'@ts-(?:ignore|expect-error|nocheck)|(?:eslint|biome|oxlint).*?(?:disable|ignore)|\bnoqa\b'
                      r'|(?:type|pyright|mypy):\s*ignore|\bpylint:\s*disable|\bnosec\b', re.I)
DIRECTIVE = re.compile(r'coding[=:]\s*[-\w.]+|^#!|\bprettier-ignore\b|^///|[@#]\s*__PURE__'
                       r'|[@#]\s*__NO_SIDE_EFFECTS__|sourceMappingURL=|sourceURL=|@(?:vite|webpack)\b'
                       r'|\bwebpack[A-Za-z]+:|@(?:jsx|flow|ts-check)\b|(?:istanbul|c8|v8)\s+ignore'
                       r'|\bpragma:\s*no cover|#\s*(?:line|fmt:|isort:|cython:)|\bSPDX-License-Identifier'
                       r'|copyright|license header|licensed under|@license|@preserve', re.I)
CONSTRAINT = re.compile(r'\b(?:important|do not remove|do not change|must not change|do not modify)\b', re.I)
CORRECTNESS = re.compile(r'@ts-(?:ignore|expect-error|nocheck)|\bno-undef\b|no-explicit-any|no-unsafe-'
                         r'|(?:type|pyright|mypy):\s*ignore|\bnosec\b', re.I)


def _git(repo, *args, **options):
    return snapshot._git(repo, *args, **options)


def _python(source):
    offsets = [0, *(match.end() for match in re.finditer('\n', source))]
    result = []
    for item in tokenize.generate_tokens(io.StringIO(source).readline):
        if item.type != tokenize.COMMENT:
            continue
        line, column = item.start
        end_line, end_column = item.end
        result.append({'line': line, 'end_line': end_line, 'start': offsets[line - 1] + column,
                       'end': offsets[end_line - 1] + end_column})
    tree = ast.dump(ast.parse(source, type_comments=True), include_attributes=False)
    return {'fingerprint': _digest(tree.encode()), 'comments': result}


def _babel(files, engine, sandbox):
    with TemporaryDirectory(prefix='iboard-comment-parser-') as directory:
        scratch = Path(directory)
        (scratch / PARSER.name).write_bytes(PARSER.read_bytes())
        (scratch / 'input.json').write_text(json.dumps({'packagePath': engine['module'], 'files': files}),
                                            encoding='utf-8')
        raw = sandbox(scratch, engine['image'], ['node', f'/workspace/{PARSER.name}'], 30,
                      source_root='/workspace', workspace_read_only=True, memory_mb=512)
    _require(raw.get('exit_code') == 0 and raw.get('container_created') and raw.get('cleanup_confirmed')
             and raw.get('workspace_read_only') is True and raw.get('stdout_truncated') is False
             and not raw.get('error') and not raw.get('timed_out'), 'offline JavaScript or TypeScript parsing failed')
    result = json.loads(raw['stdout'])
    _require(set(result) == set(files), 'parser result does not cover the selected files')
    return result


def _parse(files, engine, sandbox):
    javascript = {path: value for path, value in files.items() if Path(path).suffix in JAVASCRIPT}
    if javascript:
        _require(engine and cli.digest(PARSER) == engine['parser_sha256'], 'use the frozen Babel parser and image')
    result = _babel(javascript, engine, sandbox) if javascript else {}
    result.update({path: _python(value) for path, value in files.items() if Path(path).suffix == '.py'})
    return result


def _classified(text):
    if SUPPRESS.search(text):
        return 'suppression'
    if DIRECTIVE.search(text):
        return 'protected'
    if text.startswith('/**'):
        return 'doc-contract'
    if CONSTRAINT.search(text):
        return 'constraint'
    return 'ordinary'


def _selection(repo, base, files):
    if files:
        return snapshot._paths(list(files)), [], set()
    names = _git(repo, 'diff', '--name-only', '--diff-filter=ACMRT', '--no-ext-diff', '--no-renames', '-z',
                 base, '--').decode().rstrip('\0').split('\0')
    untracked = set(_git(repo, 'ls-files', '--others', '--exclude-standard', '-z').decode().rstrip('\0').split('\0'))
    names = sorted((set(names) | untracked) - {''})
    chosen = [path for path in names if Path(path).suffix in SUPPORTED]
    skipped = [path for path in names if path and path not in chosen]
    return snapshot._paths(chosen), skipped, untracked


def _lines(repo, base, path):
    diff = _git(repo, 'diff', '--unified=0', '--no-color', '--no-ext-diff', '--no-textconv', base, '--', path)
    return [(int(line), int(line) + int(count or 1) - 1) for line, count in HUNK.findall(diff.decode('utf-8'))]


def collect(*, repo, out, base='main', files=(), context=(), image=None, babel=None,
            sandbox=run_sandbox, resolve_image=prototype._image_id):
    repo = Path(repo).expanduser().resolve(strict=True)
    _require(Path(_git(repo, 'rev-parse', '--show-toplevel').decode().strip()).resolve() == repo,
             'choose a worktree root')
    _require(not Path(out).expanduser().resolve().is_relative_to(repo), 'keep private evidence outside the target checkout')
    snapshot._assert_index_ready(repo)
    head = snapshot._revision(repo, 'HEAD')
    base = snapshot._revision(repo, base)
    scope, unsupported, untracked = _selection(repo, base, files)
    _require(all(Path(path).suffix in SUPPORTED for path in scope), 'select Python, JavaScript or TypeScript files')
    index = snapshot._index_state(repo)
    engine = None
    if any(Path(path).suffix in JAVASCRIPT for path in scope):
        _require(isinstance(babel, str) and re.fullmatch(r'/app/[A-Za-z0-9_./+@-]+', babel)
                 and '..' not in babel.split('/') and image, 'select a local image and Babel parser package below /app')
        engine = {'image': resolve_image(image, lambda: 20), 'module': babel, 'parser_sha256': cli.digest(PARSER)}
        _require(re.fullmatch(r'sha256:[0-9a-f]{64}', engine['image']), 'the parser image must be immutable')
    observed = {path: snapshot._read(repo, path) for path in scope}
    _require(all(row is not None for row in observed.values()), 'a selected file is absent from the worktree')
    total = sum(len(body) for _, body in observed.values())
    _require(total <= 160_000, 'select a smaller code scope')
    for path, (_, body) in observed.items():
        autopilot._reject_credential_artifact(path, body)
    text = {path: row[1].decode('utf-8') for path, row in observed.items()}
    syntax = _parse(text, engine, sandbox)
    evidence, comments = {}, {}
    for path in scope:
        lines = _lines(repo, base, path) if not files and path not in untracked else None
        selected = text[path]
        hashes = {'source_sha256': _digest(selected.encode()), 'syntax': syntax[path]['fingerprint']}
        evidence[path] = {'mode': observed[path][0], 'text': selected, **hashes}
        for entry in syntax[path]['comments']:
            if lines is not None and not any(entry['line'] <= end and begin <= entry['end_line'] for begin, end in lines):
                continue
            part = selected[entry['start']:entry['end']]
            comments[f'C{len(comments) + 1}'] = {'path': path, 'text': part, 'kind': _classified(part), **entry}
    _, anchors = investigate._collect(repo, head, context, history=True, remaining=lambda: 12) if context else (head, {})
    _require(len(comments) <= 120 and all(snapshot._read(repo, path) == value for path, value in observed.items())
             and snapshot._revision(repo, 'HEAD') == head and snapshot._index_state(repo) == index,
             'selection is too large or the worktree changed while scanning')
    result = {'schema': cli.SCHEMA, 'kind': 'comment-evidence', 'source': SOURCE, 'repo': str(repo),
              'base': base, 'head': head, 'index_sha256': index, 'engine': engine,
              'files': evidence, 'comments': comments, 'anchors': anchors, 'unsupported': unsupported}
    return {**_keep(out, result), 'files': len(scope), 'comments': len(comments), 'unsupported': unsupported}


def _ref(path, sha256):
    _require(isinstance(sha256, str) and SHA.fullmatch(sha256), 'evidence needs a SHA-256 digest')
    return {'path': str(_file(path)), 'sha256': sha256}


def _load(path, sha256, kind):
    record = json.loads(_read(path, 700_000, sha256))
    _require((record['schema'], record['source'], record['kind']) == (cli.SCHEMA, SOURCE, kind),
             'wrong evidence type')
    return record


def _bounded(text, limit):
    _require(isinstance(text, str) and 1 <= len(text) <= limit and text.isprintable(), 'expected bounded single-line prose')
    return text


def _reviewable(packet):
    return {key: value for key, value in packet['comments'].items() if value['kind'] not in ('protected', 'doc-contract')}


def _decisions(packet, answer):
    _require(isinstance(answer, dict) and set(answer) == {'decisions', 'flags'}, 'invalid comment review')
    notes = _reviewable(packet)
    decisions, flags = answer['decisions'], answer['flags']
    _require(isinstance(decisions, list) and {row['id'] for row in decisions} == set(notes)
             and len(decisions) == len(notes), 'review must decide on every scoped comment once')
    for row in decisions:
        _require(set(row) == {'id', 'action', 'reason', 'evidence'}, 'comment decision has unexpected fields')
        action = row['action']
        _require(action in ('delete', 'keep', 'repair', 'encode'), 'unknown comment decision')
        _bounded(row['reason'], 550)
        refs = row['evidence']
        anchor_ids = set(packet['anchors'])
        permitted = anchor_ids if action == 'keep' else anchor_ids | {row['id']}
        _require(isinstance(refs, list) and len(refs) <= 3 and set(refs) <= permitted,
                 'comment decision cites uncaptured evidence')
        note = notes[row['id']]
        if note['kind'] == 'suppression':
            _require(action == 'repair' or (action == 'keep' and refs), 'suppression requires a root-cause flag or explicit evidence')
            _require(not CORRECTNESS.search(note['text']) or action == 'repair',
                     'a correctness or type-safety suppression stays actionable')
        if note['kind'] == 'constraint':
            _require(action in ('encode', 'repair') or (action == 'keep' and refs), 'trace or encode the stated constraint')
        if action == 'keep':
            _require(refs, 'nonstandard keep needs supporting source or history')
        if action == 'encode':
            _require(note['kind'] == 'constraint', 'an encoding offer must name a stated constraint')
    _require(isinstance(flags, list) and len(flags) <= 40, 'too many root-cause flags')
    for flag in flags:
        _require(set(flag) == {'path', 'line', 'symbol', 'reason'} and flag['path'] in packet['files'],
                 'root-cause flag leaves review scope')
        lines = packet['files'][flag['path']]['text'].splitlines()
        symbol = flag['symbol']
        _require(isinstance(symbol, str) and re.fullmatch(r'[A-Za-z_$][\w$]{0,79}', symbol)
                 and type(flag['line']) is int and 1 <= flag['line'] <= len(lines)
                 and re.search(r'(?<![\w$])' + re.escape(symbol) + r'(?![\w$])', lines[flag['line'] - 1]),
                 'flag must name an exact scoped source symbol')
        _bounded(flag['reason'], 600)
    return decisions, flags


def review(*, evidence, sha256, model, out, timeout=180, provider='openai', reviewer=None):
    _require(provider in ('openai', 'glm'), 'choose an OpenAI or GLM reviewer')
    if reviewer is None and provider == 'glm':
        from model import glm_review
        from workbench import interrogate

        def review_glm(prompt, selected_model, seconds):
            return interrogate._glm_json(glm_review.review(prompt, seconds, expected_model=selected_model))

        reviewer = review_glm
    elif reviewer is None:
        reviewer = codex_review.review
    ref = _ref(evidence, sha256)
    packet = _load(**ref, kind='comment-evidence')
    inputs = {'notes': _reviewable(packet), 'source': {name: data['text'] for name, data in packet['files'].items()},
              'authoritative_context': packet['anchors']}
    prompt = ('Review the selected source as a fresh comment reviewer. Use only data supplied. '
              'Delete narration, redundant banners and dead comments. Keep only genuine external constraints '
              'or useful official context and cite the supplied source or history IDs. For correctness and '
              'type suppression, keep the behavior until root cause repair and flag the exact scoped symbol. '
              'Do not provide code or edits. Offer concrete tests, types or lint for stated constraints. '
              'Flag actual local workaround code only when the exact line supports the named symbol. '
              'The evidence array contains only IDs from authoritative_context. For non-keep decisions it '
              'may also include the same note ID, or be empty. Never put filenames or line numbers there. '
              'Return a single JSON object and no other text: {"decisions":[{"id":"C1","action":"delete|keep|repair|encode","reason":"...",'
              '"evidence":[]}],"flags":[{"path":"file.py","line":1,"symbol":"name","reason":"..."}]}. '
              'Decide once per supplied note. Source follows.\n' + json.dumps(inputs, ensure_ascii=False))
    _require(len(prompt.encode()) <= 85_000 and type(timeout) is int and 1 <= timeout <= 300,
             'reduce the selected source or choose up to 300 seconds for model review')
    deadline, attempts = time.monotonic() + timeout, []
    prefix = ''
    for round_no in (1, 2):
        draft = prefix + prompt
        remaining = int(deadline - time.monotonic() + 0.95)
        _require(remaining >= 1, 'comment review time limit reached')
        attempts.append(_digest(draft.encode()))
        try:
            answer = reviewer(draft, model, remaining)
            decisions, flags = _decisions(packet, answer)
        except (ValueError, KeyError, TypeError) as error:
            if round_no == 2:
                raise ValueError(f'comment reviewer gave another invalid report: {error}') from error
            prefix = 'Correct the first response. Validation failed: ' + str(error)[:400] + '\n'
        else:
            break
    data = {'schema': cli.SCHEMA, 'source': SOURCE, 'kind': 'comment-review', 'evidence': ref,
            'model': model, 'provider': provider, 'prompt_sha256': attempts[-1], 'attempts': attempts,
            'decisions': decisions, 'flags': flags}
    return {**_keep(out, data), 'deletions_offered': sum(row['action'] == 'delete' for row in decisions),
            'follow_up': sum(row['action'] in ('repair', 'encode') for row in decisions) + len(flags)}


def _delete(source, entries):
    for row in sorted(entries, key=lambda note: note['start'], reverse=True):
        start, end = row['start'], row['end']
        span = source[start:end]
        _require(span == row['text'] and _classified(span) == 'ordinary', 'selected deletion is not an ordinary comment')
        previous = source[source.rfind('\n', 0, start) + 1:start]
        if not previous.strip() or (span.startswith(('#', '//')) and '\n' not in span):
            while start > 0 and source[start - 1] in ' \t':
                start -= 1
        newlines = ''.join(re.findall(r'\r\n|\r|\n', span))
        separator = (' ' if start and end < len(source) and
                     not source[start - 1].isspace() and not source[end].isspace() else '')
        source = source[:start] + (newlines or separator) + source[end:]
    return source


def _patch(path, before, after):
    lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                 fromfile=f'a/{path}', tofile=f'b/{path}')
    return ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in lines)


def apply(*, review, sha256, approve, out, sandbox=run_sandbox):
    ref = _ref(review, sha256)
    report = _load(**ref, kind='comment-review')
    packet = _load(**report['evidence'], kind='comment-evidence')
    decisions, flags = _decisions(packet, {key: report[key] for key in ('decisions', 'flags')})
    acceptable = {row['id'] for row in decisions if row['action'] == 'delete'}
    _require(approve and len(approve) == len(set(approve)) and set(approve) <= acceptable,
             'approve only exact proposed deletion IDs')
    repo = Path(packet['repo']).resolve(strict=True)
    target = Path(out).expanduser().resolve()
    _require(not target.exists() and not target.is_relative_to(repo), 'choose a new receipt outside the checkout')
    touched = {}
    for name in approve:
        note = packet['comments'][name]
        touched.setdefault(note['path'], []).append(note)
    changed = {name: _delete(packet['files'][name]['text'], entries) for name, entries in touched.items()}
    verified = _parse(changed, packet['engine'], sandbox)
    patch = ''.join(_patch(name, packet['files'][name]['text'], value) for name, value in changed.items()).encode()
    _require(patch and all(verified[name]['fingerprint'] == packet['files'][name]['syntax'] for name in changed),
             'comment edit would change the parsed application')
    _require(snapshot._revision(repo, 'HEAD') == packet['head']
             and snapshot._index_state(repo) == packet['index_sha256'], 'HEAD or staging changed during review')
    for name, info in packet['files'].items():
        _require(snapshot._read(repo, name) == (info['mode'], info['text'].encode()), 'reviewed file changed')
    _git(repo, 'apply', '--check', '--whitespace=nowarn', '-', data=patch)
    _git(repo, 'apply', '--whitespace=nowarn', '-', data=patch)
    _require(all(snapshot._read(repo, name) == (packet['files'][name]['mode'], value.encode())
                 for name, value in changed.items()) and snapshot._index_state(repo) == packet['index_sha256'],
             'applied worktree or index differs from the verified edit')
    return _keep(target, {'schema': cli.SCHEMA, 'source': SOURCE, 'kind': 'comment-application',
                          'review': ref, 'removed': approve, 'patch_sha256': _digest(patch),
                          'worktree': str(repo), 'syntax': {name: verified[name]['fingerprint'] for name in changed},
                          'follow_up': [row for row in decisions if row['action'] in ('repair', 'encode')],
                          'flags': flags})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    stages = parser.add_subparsers(dest='action', required=True)
    names = {'collect': ('repo', 'out'), 'review': ('evidence', 'sha256', 'model', 'out'),
             'apply': ('review', 'sha256', 'out')}
    for action, keys in names.items():
        stage = stages.add_parser(action)
        for key in keys:
            stage.add_argument('--' + key, required=True)
        if action == 'collect':
            stage.add_argument('--base', default='main')
            stage.add_argument('--file', dest='files', action='append', default=[])
            stage.add_argument('--context', action='append', default=[])
            stage.add_argument('--image')
            stage.add_argument('--babel')
        if action == 'review':
            stage.add_argument('--timeout', type=int, default=180)
            stage.add_argument('--provider', choices=('openai', 'glm'), default='openai')
        if action == 'apply':
            stage.add_argument('--approve', action='append', required=True)
    args = vars(parser.parse_args(argv))
    action = args.pop('action')
    try:
        result = {'collect': collect, 'review': review, 'apply': apply}[action](**args)
    except ERRORS as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
