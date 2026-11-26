"""Look up pstack skills and prompts and their native iBoard stage mappings."""

import argparse
import json
from pathlib import Path

from workbench.investigate import SOURCES as INVESTIGATION_SOURCES
from workbench.prompt_profiles import PROFILES, PSTACK_COMMIT


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
            'native_commands': [f'python3 -m workbench.investigate {mode}'
                                for mode, path in INVESTIGATION_SOURCES.items() if path == source],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
