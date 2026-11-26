"""Look up pstack skills and prompts and their native iBoard stage mappings."""

import argparse
import json
from pathlib import Path

from workbench.decisions import PSTACK_SOURCE as DECISION_SOURCE
from workbench.investigate import SOURCES as INVESTIGATION_SOURCES
from workbench.pickup import PSTACK_SOURCES as PICKUP_SOURCES
from workbench.pr_watch import PSTACK_SOURCE as PR_WATCH_SOURCE
from workbench.prompt_profiles import PROFILES, PSTACK_COMMIT
from workbench.stack_verify import PSTACK_SOURCES as STACK_VERIFICATION_SOURCES
from workbench.stack_ship import PSTACK_SOURCE as STACK_SHIPPING_SOURCE


CATALOG = Path(__file__).resolve().parents[1] / 'docs' / 'pstack' / 'catalog.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    listing = subparsers.add_parser('list', help='List source paths by role')
    listing.add_argument('--role')
    show = subparsers.add_parser('show', help='Show an exact source path or skill name')
    show.add_argument('entry')
    args = parser.parse_args()
    catalog = json.loads(CATALOG.read_text(encoding='utf-8'))
    if catalog['commit'] != PSTACK_COMMIT:
        parser.error('catalog and native prompt profiles refer to different pstack commits')
    entries = catalog['entries']
    if args.command == 'list':
        if args.role and args.role not in catalog['counts']:
            parser.error('unknown role; choose from ' + ', '.join(catalog['counts']))
        result = [e['source'] for e in entries if not args.role or e['role'] == args.role]
    else:
        found = [e for e in entries if e['source'] == args.entry or e['metadata'].get('name') == args.entry]
        if len(found) != 1:
            parser.error('entry not unique or not found; use an exact path from list')
        entry = found[0]
        source = entry['source']
        result = {
            **entry,
            'url': f"{catalog['source']}/blob/{catalog['commit']}/pstack/{source}",
            'native_guidance_stages': [
                {'phase': phase, 'kind': kind}
                for (phase, kind), (sources, _) in PROFILES.items() if source in sources
            ],
            'native_commands': ([f'python3 -m workbench.investigate {mode}'
                                 for mode, path in INVESTIGATION_SOURCES.items() if path == source] +
                                (['python3 -m workbench.decisions audit', 'python3 -m workbench.decisions record',
                                  'python3 -m workbench.decision_review']
                                 if source == DECISION_SOURCE else []) +
                                (['python3 -m workbench.pr_watch'] if source == PR_WATCH_SOURCE else []) +
                                (['python3 -m workbench.verify_pr', 'python3 -m workbench.stack_verify']
                                 if source in STACK_VERIFICATION_SOURCES else []) +
                                (['python3 -m workbench.stack_ship'] if source == STACK_SHIPPING_SOURCE else []) +
                                ([PICKUP_SOURCES[source]] if source in PICKUP_SOURCES else [])),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
