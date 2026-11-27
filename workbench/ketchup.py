"""Collect read-only evidence for a text catch-up from the last human message."""

import argparse
import json
from pathlib import Path
import re
from urllib.parse import quote

from workbench import cli, ketchup_video
from workbench.automate_me import _keep, _require
from workbench.prompt_profiles import PSTACK_COMMIT


SOURCE = 'skills/ketchup/SKILL.md'
PR_STATES = {'OPEN': 'open', 'CLOSED': 'closed', 'MERGED': 'merged', 'unknown': 'unconfirmed'}


def collect(*, repo, transcript, invocation_line, manifest, out=None, reader=None):
    repo = Path(repo).expanduser().resolve(strict=True)
    if out is not None:
        _require(not Path(out).expanduser().resolve().is_relative_to(repo), 'write catch-up evidence outside the project')
    window = ketchup_video._anchor(repo, transcript, invocation_line, command='ketchup', context_limit=None)
    sources, pending = ketchup_video._collect_sources(window=window, manifest=manifest, reader=reader,
                                                     include_task_events=True)
    packet = {'schema': cli.SCHEMA, 'kind': 'text-catch-up', 'source': SOURCE, 'pstack_commit': PSTACK_COMMIT,
              'repo': str(repo), 'window': window, 'evidence': sources, 'pending': pending}
    return packet if out is None else _keep(out, packet)


def _literal(text):
    return re.sub(r'([\\`*_{}\[\]<>#~|!&])', r'\\\1', text)


def render(packet):
    """Render verified state and selected decisions without inferring next actions."""
    pending = packet['pending']
    lines = ['Decisions still waiting on you.' if pending else 'No decisions were identified for you.']
    sources = packet['evidence']
    for item in sources.values():
        if item.get('decision_only') or item['kind'] not in ('screenshot', 'recording'):
            continue
        label = 'Screenshot' if item['kind'] == 'screenshot' else 'Recording'
        lines.append(f"![{label}]({quote(item['path'], safe='/')})")
    for item in sources.values():
        if item['kind'] != 'live-pr':
            continue
        facts, selector, checks = item['facts'], item['selector'], item['checks']
        name = f"{selector['repo']}#{selector['number']}"
        link = f"[{name}]({facts['url']})" if 'url' in facts else name
        state = 'an open draft' if facts['state'] == 'OPEN' and facts.get('isDraft') is True else PR_STATES[facts['state']]
        status = f'- {link} is {state}.'
        if checks is None:
            status += ' Check status could not be confirmed.'
        elif not checks:
            status += ' GitHub showed no checks.'
        else:
            observed = ', '.join(f'{count} {name.replace("_", " ")}' for name, count in sorted(checks.items()))
            status += f' GitHub showed {observed}.'
        lines.append(status)
    for decision in pending:
        lines.append(f"- {_literal(decision['question'])} Consider **{_literal(decision['recommendation'])}**.")
        for option in decision['options']:
            lines.append(f"  - **{_literal(option['name'])}**. Gain · {_literal(option['gain'])}. Cost · {_literal(option['cost'])}.")
    return '\n\n'.join((lines[0], '\n'.join(lines[1:]))).rstrip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('repo', 'transcript', 'manifest'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--invocation-line', required=True, type=int)
    parser.add_argument('--out', help='Save to a new private file outside the target project')
    parser.add_argument('--markdown', action='store_true', help='Print verified PR state, visuals and waiting decisions')
    options = vars(parser.parse_args(argv))
    markdown = options.pop('markdown')
    if markdown and options['out'] is not None:
        parser.error('choose Markdown on stdout or a private JSON packet')
    try:
        result = collect(**options)
    except ketchup_video.ERRORS as error:
        parser.error(str(error))
    print(render(result) if markdown else json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
