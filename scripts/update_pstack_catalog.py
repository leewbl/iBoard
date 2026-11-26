"""Inventory the pinned pstack checkout without executing any upstream files."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess


PIN = '41e400932eca14f289e7db717a70455256b6d388'
VERSION = '0.15.48'
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'docs' / 'pstack' / 'catalog.json'
LINK = re.compile(r'(?<!!)\[[^\]\n]+\]\(([^)\s]+)\)')
HEADING = re.compile(r'^ {0,3}#{1,6} +(.+?)\s*$')
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')


def role(path):
    parts = PurePosixPath(path).parts
    if parts[-1] == 'SKILL.md':
        return 'skill'
    if parts[0] == 'agents' or path.endswith('/worker.md'):
        return 'agent-prompt'
    if 'playbooks' in parts:
        return 'playbook'
    if 'templates' in parts:
        return 'automation-template'
    if 'references' in parts:
        return 'reference'
    if path.endswith('/FOR_AGENTS.md'):
        return 'agent-prompt'
    return 'guide'


def parse_markdown(text):
    lines = text.splitlines()
    metadata = {}
    if lines and lines[0] == '---':
        end = lines.index('---', 1)
        for line in lines[1:end]:
            key, sep, raw = line.partition(':')
            if sep and key in ('name', 'disable-model-invocation', 'model', 'tools'):
                metadata[key] = raw.strip().strip('"\'')
        lines = lines[end + 1:]

    headings, visible = [], []
    fence = None
    for line in lines:
        marker = FENCE.match(line)
        if marker:
            delimiter, rest = marker.groups()
            if fence is None:
                fence = delimiter
            elif delimiter.startswith(fence) and not rest.strip():
                fence = None
            continue
        if fence is not None:
            continue
        visible.append(line)
        heading = HEADING.match(line)
        if heading:
            headings.append(heading.group(1))
    return metadata, headings, LINK.findall('\n'.join(visible))


def source_files(root):
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if ('scripts' not in relative.parts and path.is_file() and not path.is_symlink()
                and (path.suffix == '.md'
                     or path.suffix in ('.yaml', '.yml', '.tsv')
                     and relative.parts[0] in ('automations', 'skills'))):
            yield relative.as_posix()


def catalog(root):
    upstream = root / 'pstack'
    claude = root / 'harness' / 'pstack' / 'claude-code'
    version = json.loads((upstream / '.cursor-plugin' / 'plugin.json').read_text(encoding='utf-8'))['version']
    if version != VERSION or not (claude / '.claude-plugin' / 'plugin.json').is_file():
        raise ValueError('source does not contain the pinned pstack and Claude Code layouts')
    files = sorted(source_files(upstream))
    known = set(files)
    entries = []
    for name in files:
        raw = (upstream / name).read_bytes()
        metadata, headings, targets = parse_markdown(raw.decode('utf-8'))
        references = set()
        for target in targets:
            relative = posixpath.normpath(posixpath.join(posixpath.dirname(name), target.split('#', 1)[0]))
            if relative in known and relative != name:
                references.add(relative)
        alternate = claude / name
        entries.append({
            'source': name,
            'role': role(name),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'claude_sha256': hashlib.sha256(alternate.read_bytes()).hexdigest() if alternate.is_file() else None,
            'metadata': metadata,
            'headings': headings,
            'references': sorted(references),
        })
    return {
        'version': version,
        'commit': PIN,
        'source': 'https://github.com/cursor/plugins',
        'scope': 'Skills, playbooks, agent prompts, automation templates, and documented references and guides. Generated test fixtures are excluded.',
        'counts': dict(sorted(Counter(e['role'] for e in entries).items())),
        'entries': entries,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkout', type=Path, help='Checkout of cursor/plugins at the pinned commit')
    parser.add_argument('--check', action='store_true', help='Compare with the committed catalog')
    args = parser.parse_args()
    upstream = args.checkout.resolve(strict=True)
    commit = subprocess.check_output(['git', '-C', upstream, 'rev-parse', 'HEAD'], text=True).strip()
    if commit != PIN:
        parser.error(f'expected checkout at {PIN}')
    for directory in ('pstack', 'harness/pstack/claude-code'):
        status = subprocess.check_output(['git', '-C', upstream, 'status', '--porcelain',
                                          '--untracked-files=all', '--', directory], text=True)
        if status:
            parser.error(f'upstream directory has uncommitted changes: {directory}')
    rendered = json.dumps(catalog(upstream), ensure_ascii=False, indent=2) + '\n'
    if args.check:
        if OUTPUT.read_text(encoding='utf-8') != rendered:
            parser.error('catalog differs from the pinned source')
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(rendered, encoding='utf-8')


if __name__ == '__main__':
    main()
