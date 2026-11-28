"""Validate skill frontmatter and local links against a committed Git tree."""

from contextlib import contextmanager
import json
from pathlib import PurePosixPath
import posixpath
import re
import subprocess
from urllib.parse import unquote, urlsplit

from workbench import cli


SKILL = r'(?:(?:\.claude|\.agents)/)?skills/([^/]+)'
SKILL_PATH = re.compile(SKILL + r'/(.+)')
NAME = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')
PAIRS = re.compile(r'(?m)^([A-Za-z][A-Za-z0-9_-]*):[ \t]*(.*)$')
FENCE = re.compile(r'^[ \t]{0,3}(' + chr(96) + r'{3,}|~{3,})(.*)$')
DESTINATION = r'(?:<([^<>\n]+)>|([^\s<>()]+(?:\([^\s<>()]*\)[^\s<>()]*)*))'
INLINE = re.compile(r'(?<!\\)\]\([ \t]*' + DESTINATION +
                    r'(?:[ \t]+(?:"[^"\n]*"|\'[^\'\n]*\'))?[ \t]*\)')
DEFINITION = re.compile(r'(?m)^[ \t]{0,3}\[[^]\n]+\]:[ \t]*' + DESTINATION)
HEADING = re.compile(r'^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$')
ANCHOR = re.compile(r'<(?:a|span)\b[^>]*\b(?:id|name)=["\']([^"\']+)["\']', re.I)
REGULAR = {'100644', '100755'}


def _run(repo, *args):
    return cli.git(repo, *args, timeout=20)


def _tree(repo, commit):
    tree = {}
    for entry in _run(repo, 'ls-tree', '-rz', '--full-tree', commit).split('\0'):
        if entry:
            meta, path = entry.split('\t', 1)
            mode, _, oid = meta.split()
            tree[path] = (mode, oid)
    return tree


@contextmanager
def _blobs(repo):
    with subprocess.Popen(['git', '-C', str(repo), 'cat-file', '--batch'],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        def read(oid):
            process.stdin.write(oid.encode('ascii') + b'\n')
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3 or header[1] != b'blob':
                raise cli.WorkbenchError('Git did not return the expected blob')
            size = int(header[2])
            if size > 320_000:
                raise cli.WorkbenchError('skill document exceeds the source size bound')
            result = process.stdout.read(size)
            if len(result) != size or process.stdout.read(1) != b'\n':
                raise cli.WorkbenchError('Git returned an incomplete blob')
            return result.decode('utf-8')

        try:
            yield read
        finally:
            process.stdin.close()
            # On an oversized blob, Git can still be writing the unread payload.
            process.stdout.close()
            process.wait(timeout=5)


def _metadata(text, expected):
    lines = text.splitlines()
    if not lines or lines[0] != '---' or '---' not in lines[1:]:
        raise ValueError('start SKILL.md with YAML frontmatter')
    end = lines.index('---', 1)
    header = '\n'.join(lines[1:end])
    matches = list(PAIRS.finditer(header))
    fields = {}
    for index, match in enumerate(matches):
        key, value = match.groups()
        if key not in {'name', 'description'}:
            continue
        if key in fields:
            raise ValueError(f'duplicate {key} in frontmatter')
        boundary = matches[index + 1].start() if index + 1 < len(matches) else len(header)
        if re.fullmatch(r'[|>][+-]?', value.strip()):
            tail = header[match.end():boundary].splitlines()
            separator = '\n' if value.lstrip().startswith('|') else ' '
            value = separator.join(line.strip() for line in tail if line.strip() and line[0].isspace())
        else:
            value = value.strip()
            if value.startswith(("'", '"')):
                quoted = re.fullmatch(r"""(['"])(.*)\1[ \t]*(?:#.*)?""", value)
                if not quoted:
                    raise ValueError(f'malformed quoted {key} in frontmatter')
                try:
                    value = quoted[2].replace("''", "'") if quoted[1] == "'" else json.loads('"' + quoted[2] + '"')
                except json.JSONDecodeError as error:
                    raise ValueError(f'malformed quoted {key} in frontmatter') from error
            else:
                value = re.split(r'\s+#', value, maxsplit=1)[0]
                if value.startswith(('[', '{', '&', '*', '!', '>', '|')) or re.search(r':(?:\s|$)', value):
                    raise ValueError(f'{key} must be a YAML string')
                if re.fullmatch(r'(?i:null|~|true|false|[-+]?\d+(?:\.\d+)?)', value):
                    raise ValueError(f'{key} must be a YAML string')
        fields[key] = value.strip()
    name, description = fields.get('name', ''), fields.get('description', '')
    if not NAME.fullmatch(name) or name != expected or len(name) > 64:
        raise ValueError('name must be a lowercase slug matching the skill directory')
    if not 1 <= len(description) <= 1024:
        raise ValueError('description must be nonempty and at most 1024 characters')
    body = '\n'.join(lines[end + 1:])
    if not body.strip():
        raise ValueError('SKILL.md has no instructions')
    return fields, body


def _visible(text):
    result = []
    fence = ''
    for line in text.splitlines():
        marker = FENCE.fullmatch(line)
        if marker:
            token, trailing = marker.groups()
            if not fence:
                fence = token
            elif token.startswith(fence) and not trailing.strip():
                fence = ''
            continue
        if not fence:
            result.append(line)
    return result


def _slug(title):
    return re.sub(r'[^\w -]', '', title.lower()).replace(' ', '-')


def _anchors(text):
    rows = _visible(text)
    anchors = set(ANCHOR.findall('\n'.join(rows)))
    counts = {}
    for index, line in enumerate(rows):
        heading = HEADING.fullmatch(line)
        if heading:
            title = heading[1]
        elif index + 1 < len(rows) and line.strip() and re.fullmatch(r'[ \t]*(?:=+|-+)[ \t]*', rows[index + 1]):
            title = line.strip()
        else:
            continue
        slug = _slug(title)
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add(f'{slug}-{count}' if count else slug)
    return anchors


def _targets(body):
    text = '\n'.join(_visible(body))
    # Markdown examples in inline code are not references. Keep the surrounding link syntax.
    tick = chr(96)
    text = re.sub(tick + r'+[^' + tick + r'\n]+' + tick + '+', '', text)
    return sorted({match[1] or match[2] for pattern in (INLINE, DEFINITION) for match in pattern.finditer(text)})


def _resolve(path, target, tree, directories, read):
    parsed = urlsplit(target)
    if parsed.scheme and parsed.scheme.lower() == 'file':
        raise ValueError('file URLs are not portable repository references')
    if parsed.scheme or parsed.netloc:
        return None
    relative = unquote(parsed.path)
    if relative.startswith('/') or '\\' in relative or any(ord(c) < 32 for c in relative):
        raise ValueError('local reference must use a relative repository path')
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), relative)) if relative else path
    if resolved == '..' or resolved.startswith('../'):
        raise ValueError('reference leaves the repository')
    if resolved not in directories and tree.get(resolved, ('',))[0] not in REGULAR:
        raise ValueError('reference does not resolve to a tracked file or directory')
    fragment = unquote(parsed.fragment)
    if fragment and resolved.endswith('.md') and resolved in tree and fragment not in _anchors(read(tree[resolved][1])):
        raise ValueError(f'missing heading or anchor #{fragment}')
    return resolved


def _inspect(tree, read):
    reports = {}
    directories = {str(parent) for path in tree for parent in PurePosixPath(path).parents}
    for path in sorted(tree):
        match = SKILL_PATH.fullmatch(path)
        if not match or match[2] != 'SKILL.md':
            continue
        mode, oid = tree[path]
        report = {'blob': oid, 'references': [], 'problems': []}
        reports[path] = report
        try:
            if mode not in REGULAR:
                raise ValueError('SKILL.md must be a regular file')
            fields, body = _metadata(read(oid), match[1])
        except (UnicodeError, ValueError) as error:
            report['problems'].append(str(error))
            continue
        report['frontmatter'] = fields
        for target in _targets(body):
            try:
                resolved = _resolve(path, target, tree, directories, read)
            except (UnicodeError, ValueError) as error:
                report['problems'].append(f'{target}: {error}')
            else:
                if resolved is not None:
                    report['references'].append(resolved)
    return reports


def compare(repo, base, head):
    """Return authored skills, their dependency checks, and any new broken references."""
    before_tree, after_tree = _tree(repo, base), _tree(repo, head)
    changed = {path for path in before_tree.keys() | after_tree.keys() if before_tree.get(path) != after_tree.get(path)}
    touched = {path[:match.start(2) - 1] for path in changed if (match := SKILL_PATH.fullmatch(path))}
    with _blobs(repo) as read:
        before, after = _inspect(before_tree, read), _inspect(after_tree, read)
    affected = {}
    problems = []
    for path, report in after.items():
        previous = before.get(path, {})
        direct = path.rsplit('/', 1)[0] in touched
        new = set(report['problems']) - set(previous.get('problems', []))
        impacted = new or any(ref in changed for ref in report['references'])
        if direct or impacted:
            affected[path] = report
            for problem in sorted(set(report['problems']) if direct else new):
                problems.append({'skill': path, 'problem': problem})
    for root in sorted(touched):
        path = root + '/SKILL.md'
        if path not in after and any(file.startswith(root + '/') for file in after_tree):
            problems.append({'skill': path, 'problem': 'remaining skill files have no SKILL.md entry point'})
    removed = sorted(path for path in before if path not in after)
    structural = sorted(path for path in changed if (match := SKILL_PATH.fullmatch(path)) and (
        match[2].startswith('scripts/') or '100755' in (before_tree.get(path, ('',))[0], after_tree.get(path, ('',))[0])))
    return {'authored': sorted(touched), 'affected': affected, 'removed': removed, 'problems': problems,
            'structural': structural}
