"""Look up pstack skills and prompts and their native iBoard stage mappings."""

import argparse
import json
from pathlib import Path

from workbench.arena import PSTACK_SOURCE as ARENA_SOURCE
from workbench.architect import PSTACK_SOURCE as ARCHITECT_SOURCE
from workbench.benchmark import PSTACK_SOURCE as BENCHMARK_SOURCE
from workbench.decisions import PSTACK_SOURCE as DECISION_SOURCE
from workbench.hillclimb import PSTACK_SOURCE as HILLCLIMB_SOURCE
from workbench.investigate import SOURCES as INVESTIGATION_SOURCES
from workbench.multiphase import PSTACK_SOURCE as MULTIPHASE_SOURCE
from workbench.pickup import PSTACK_SOURCES as PICKUP_SOURCES
from workbench.project_verify import PSTACK_COMMANDS as PROJECT_VERIFICATION_COMMANDS
from workbench.pr_watch import PSTACK_SOURCE as PR_WATCH_SOURCE
from workbench.prompt_profiles import PROFILES, PSTACK_COMMIT
from workbench.prototype import PSTACK_SOURCE as PROTOTYPE_SOURCE
from workbench.stack_verify import PSTACK_SOURCES as STACK_VERIFICATION_SOURCES
from workbench.stack_ship import PSTACK_SOURCE as STACK_SHIPPING_SOURCE
from workbench.stack_autopilot import PSTACK_SOURCE as STACK_AUTOPILOT_SOURCE


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
                                (['python3 -m workbench.stack_ship','python3 -m workbench.stack_rebase']
                                 if source == STACK_SHIPPING_SOURCE else []) +
                                (['python3 -m workbench.stack_autopilot'] if source == STACK_AUTOPILOT_SOURCE else []) +
                                (['python3 -m workbench.arena'] if source == ARENA_SOURCE else []) +
                                (['python3 -m workbench.architect plan', 'python3 -m workbench.architect review']
                                 if source == ARCHITECT_SOURCE else []) +
                                (['python3 -m workbench.benchmark'] if source == BENCHMARK_SOURCE else []) +
                                (['python3 -m workbench.hillclimb'] if source == HILLCLIMB_SOURCE else []) +
                                (['python3 -m workbench.multiphase', 'python3 -m workbench.phase_audit']
                                 if source == MULTIPHASE_SOURCE else []) +
                                (['python3 -m workbench.prototype'] if source == PROTOTYPE_SOURCE else []) +
                                ([f'python3 -m workbench.project_verify {PROJECT_VERIFICATION_COMMANDS[source]}']
                                 if source in PROJECT_VERIFICATION_COMMANDS else []) +
                                (['python3 -m workbench.project_verify discover', 'python3 -m workbench.project_triage']
                                 if PROJECT_VERIFICATION_COMMANDS.get(source) == 'maintain' else []) +
                                ([PICKUP_SOURCES[source]] if source in PICKUP_SOURCES else [])),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
