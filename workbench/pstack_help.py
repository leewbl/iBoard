"""Find an iBoard entry point for a task, using the pinned pstack catalog."""

import argparse
import json
from pathlib import Path
from typing import NamedTuple

from workbench.pstack_catalog import CATALOG
from workbench.prompt_profiles import PROFILES, PSTACK_COMMIT


ROOT = Path(__file__).resolve().parents[1]
HELP_SOURCE = 'skills/poteto-help/SKILL.md'


class Scenario(NamedTuple):
    goal: str
    source: str
    guide: str
    command: str
    evidence: str
    boundary: str = ''


SCENARIOS = {
    'setup': Scenario(
        'Load the iBoard plugin', 'docs/guide/01-setup.md', 'README.md#工作台插件原型',
        'claude --plugin-dir /path/to/iboard', 'Claude Code exposes /agent-workbench:poteto-help',
        'The plugin is loaded locally. pstack’s per-role model and reasoning-budget setup is not implemented.'),
    'help': Scenario(
        'Find a workflow for a task', HELP_SOURCE, 'skills/poteto-help/SKILL.md',
        'pstack_help', 'The pinned source, local guide, available entry point and its evidence requirements'),
    'how': Scenario(
        'Explain how code works', 'skills/how/SKILL.md', 'skills/investigate/SKILL.md',
        'investigate how', 'A source-backed explanation for an exact repository revision'),
    'why': Scenario(
        'Explain why a design was chosen', 'skills/why/SKILL.md', 'skills/investigate/SKILL.md',
        'investigate why', 'Source and commit history that support the explanation',
        'Pass a PR explicitly to include its discussion.'),
    'teach': Scenario(
        'Learn how a subsystem works and why', 'skills/teach/SKILL.md', 'skills/investigate/SKILL.md',
        'investigate teach', 'An explanation that combines verified source and history findings',
        'The CLI writes a report. Interactive lessons and progressive diagrams are outside this entry.'),
    'investigate': Scenario(
        'Investigate a question without editing the repository', 'skills/poteto-mode/playbooks/investigation.md',
        'skills/investigate/SKILL.md', 'investigate investigation', 'A report with source and history citations'),
    'fix-bug': Scenario(
        'Reproduce and fix a specified bug', 'skills/poteto-mode/playbooks/bug-fix.md',
        'skills/optimize/SKILL.md', 'start --kind bug', 'Baseline reproduction and independent candidate runs on the same scenario',
        'Provide a task budget and observable wrong result. Delivery needs an explicitly authorized fork.'),
    'test-first': Scenario(
        'Use the same regression test before and after a fix', 'skills/tdd/SKILL.md',
        'skills/tdd/SKILL.md', 'start --kind bug', 'Two baseline failures at the expected assertion and two isolated candidate passes',
        'Ask for a focused deterministic regression in the bug request. There is no --tdd switch.'),
    'refactor': Scenario(
        'Change code structure while preserving observed behavior', 'skills/poteto-mode/playbooks/refactoring.md',
        'skills/refactor/SKILL.md', 'refactor freeze', 'A frozen baseline and byte-for-byte receipts from each commit',
        'Freeze the probes before editing, then use workbench.start --kind refactor for guarded delivery.'),
    'build-feature': Scenario(
        'Deliver a feature using an approved design', 'skills/poteto-mode/playbooks/feature.md',
        'skills/feature-handoff/SKILL.md', 'start --kind feature',
        'The approved design, scoped changes, runtime evidence and independent acceptance',
        'Supply the approved handoff and acceptance criteria. Delivery needs an explicitly authorized fork.'),
    'plan-design': Scenario(
        'Compare designs before implementing them', 'skills/architect/SKILL.md',
        'skills/architect/SKILL.md', 'architect plan', 'Competing designs tied to a pinned source revision'),
    'compare-patches': Scenario(
        'Try competing implementations of the same scoped change', 'skills/arena/SKILL.md',
        'skills/arena/SKILL.md', 'arena', 'Independent artifacts, blinded assessments and a verified winning patch',
        'The native Arena entry compares code patches.'),
    'prototype': Scenario(
        'Gather evidence to choose a design', 'skills/poteto-mode/playbooks/prototype.md',
        'skills/prototype/SKILL.md', 'prototype', 'Per-option behavior, artifacts and observations from the declared trial'),
    'plan-phases': Scenario(
        'Plan or dispatch a multi-stage change', 'skills/poteto-mode/playbooks/multi-phase-plan.md',
        'skills/multi-phase-plan/SKILL.md', 'multiphase', 'A phase graph and per-phase acceptance checks',
        'Planning, authorized dispatch and GitHub delivery each have separate entry points.'),
    'review-pr': Scenario(
        'Challenge a change with independent checks', 'skills/interrogate/SKILL.md',
        'skills/interrogate/SKILL.md', 'interrogate', 'Source-bound observations and independently repeated probes',
        'Choose the scope and run approved probes. The command does not discover and authorize new probes.'),
    'pr-status': Scenario(
        'Check what blocks a specific PR or stack', 'skills/poteto-mode/playbooks/babysit.md',
        'skills/babysit/SKILL.md', 'pr_watch --mode check', 'Current PR heads and blocking checks or threads',
        'The watcher reads GitHub without fixing, merging, or responding to review threads.'),
    'verify-pr': Scenario(
        'Independently verify an existing PR', 'skills/poteto-mode/playbooks/shipping.md',
        'skills/verify-pr/SKILL.md', 'verify_pr', 'Baseline and candidate evidence bound to the selected PR revisions'),
    'ship-stack': Scenario(
        'Plan delivery of a verified PR stack', 'skills/poteto-mode/playbooks/shipping.md',
        'skills/shipping/SKILL.md', 'stack_ship plan', 'An ordered plan for the contiguous verified prefix',
        'Shipping itself requires explicit authorization and runs as a separate command.'),
    'prepare-stack': Scenario(
        'Reverify a selected PR stack for handoff', 'skills/poteto-mode/playbooks/autopilot-stack.md',
        'skills/autopilot-stack/SKILL.md', 'stack_autopilot freeze', 'A frozen manifest and per-PR verification checkpoints',
        'The native workflow starts with existing PRs and stops before merging.'),
    'inspect-task': Scenario(
        'Inspect a previously started iBoard task', 'skills/poteto-mode/playbooks/session-pickup.md',
        'skills/pickup/SKILL.md', 'pickup', 'Saved task boundaries, evidence and the next safe step',
        'Pass an iBoard task record. This entry only reads saved task state.'),
    'resume-task': Scenario(
        'Resume a paused iBoard task at its saved checkpoint', 'skills/poteto-mode/playbooks/session-pickup.md',
        'README.md#工作台插件原型', 'resume', 'A restorable task with matching inputs and a valid budget',
        'Inspect the checkpoint first. Resume may run the saved task within its original authorization.'),
    'catch-up': Scenario(
        'See progress since a selected time', 'skills/ketchup/SKILL.md',
        'skills/pickup/SKILL.md', 'pickup', 'Task events, supporting artifacts and optional live PR state',
        'Pass the task record and a timestamp with --since. The command does not read the chat transcript.'),
    'generate-verify': Scenario(
        'Create a project-specific verification skill', 'skills/create-verification-skill/SKILL.md',
        'skills/create-verification-skill/SKILL.md', 'project_verify create',
        'A source-anchored feature map and reports from doctor and a selected smoke journey',
        'Provide the project verification contract and real drivers.'),
    'maintain-verify': Scenario(
        'Check project verification against a new revision', 'skills/maintain-verification-skill/SKILL.md',
        'skills/maintain-verification-skill/SKILL.md', 'project_verify maintain',
        'Source-drift findings, rerun receipts and skipped or blocked journeys'),
    'create-formal-verify': Scenario(
        'Verify a named property and generate its local skill', 'skills/create-formal-verification-skill/SKILL.md',
        'skills/create-formal-verification-skill/SKILL.md', 'formal_verify create',
        'Source anchors, declared scope, positive checks and a counterexample from a real-code mutation',
        'Provide a runnable verifier and its dependencies in the exact local source image.'),
    'maintain-formal-verify': Scenario(
        'Sync a property check with newer code', 'skills/maintain-formal-verification-skill/SKILL.md',
        'skills/maintain-formal-verification-skill/SKILL.md', 'formal_verify maintain',
        'Commit review, as-installed result, new verification, negative control and a separate diff',
        'An independently reproduced witness is required to report a product bug.'),
    'check-performance': Scenario(
        'Verify a performance claim against a baseline', 'skills/benchmark-checklist/SKILL.md',
        'skills/benchmark-checklist/SKILL.md', 'benchmark', 'Controlled workload, execution samples and comparable results'),
    'improve-performance': Scenario(
        'Improve a metric without weakening behavior checks', 'skills/poteto-mode/playbooks/hillclimb.md',
        'skills/hillclimb/SKILL.md', 'hillclimb', 'Measurements and decisions for each candidate that was kept or rejected',
        'Use a frozen budget and a finite queue of candidate patches or hypotheses.'),
    'capture-cpu': Scenario(
        'Capture CPU evidence from a live workload', 'skills/poteto-mode/playbooks/runtime-forensics.md',
        'skills/runtime-forensics/SKILL.md', 'runtime_forensics cpu', 'An offline Node profile and findings tied to Git source',
        'For memory retention use the documented heap mode.'),
    'analyze-trace': Scenario(
        'Examine an existing runtime capture', 'skills/poteto-mode/playbooks/trace-forensics.md',
        'skills/trace-forensics/SKILL.md', 'trace_forensics', 'The original artifact digest and findings that refer to decoded data',
        'Input formats are the supported Node, V8, Chrome and macOS capture formats.'),
    'prove-visual-parity': Scenario(
        'Check whether required visual states are pixel-exact', 'skills/poteto-mode/playbooks/visual-parity.md',
        'skills/visual-parity/SKILL.md', 'visual_parity baseline', 'Frozen browser captures and pixel comparisons for declared states',
        'Capture the approved baseline before comparing the candidate.'),
    'audit-decisions': Scenario(
        'Review a task’s decision trail', 'skills/show-me-your-work/SKILL.md',
        'skills/show-me-your-work/SKILL.md', 'decisions audit', 'Decision records with file digests or Git commit references'),
}


def _load_catalog():
    catalog = json.loads(CATALOG.read_text(encoding='utf-8'))
    if catalog['commit'] != PSTACK_COMMIT:
        raise ValueError('catalog and native prompt profiles refer to different pstack commits')
    return catalog


def _source_link(catalog, source):
    return f"{catalog['source']}/blob/{catalog['commit']}/pstack/{source}"


def _entry_names(entry):
    source = entry['source']
    yield source
    if entry['role'] == 'playbook':
        yield Path(source).stem
    if entry['metadata'].get('name'):
        yield entry['metadata']['name']


def _scenario(name, catalog):
    scenario = SCENARIOS[name]
    if not any(entry['source'] == scenario.source for entry in catalog['entries']):
        raise ValueError(f'pstack source for {name} is missing from the pinned catalog')
    command = scenario.command
    if name == 'setup':
        entry_paths = ('.claude-plugin/plugin.json', HELP_SOURCE)
    else:
        entry_paths = (f'workbench/{command.split()[0]}.py',)
        command = f'python3 -m workbench.{command}'
    guide_path = scenario.guide.split('#', 1)[0]
    present = all((ROOT / path).is_file() for path in (*entry_paths, guide_path))
    return {
        'scenario': name,
        'goal': scenario.goal,
        'status': 'native-entry' if present else 'missing-local-files',
        'command': command if present else None,
        'local_guide': scenario.guide if present else None,
        'pstack_source': _source_link(catalog, scenario.source),
        'expected_evidence': scenario.evidence,
        'boundary': scenario.boundary,
    }


def _show(query, catalog):
    query = query.removeprefix('/pstack:')
    entries = [entry for entry in catalog['entries'] if query in _entry_names(entry)]
    if len(entries) != 1:
        raise ValueError('source entry is unknown or ambiguous. Use an exact source path from '
                         'python3 -m workbench.pstack_catalog list')
    entry = entries[0]
    source = entry['source']
    routes = [_scenario(name, catalog) for name, scenario in SCENARIOS.items() if source == scenario.source]
    stages = [f'{phase}/{kind}' for (phase, kind), (sources, _) in PROFILES.items() if source in sources]
    if routes:
        status = 'native-entry' if any(route['command'] for route in routes) else 'missing-local-files'
    else:
        status = 'guidance-only' if stages else 'catalog-only'
    return {'source': source, 'pstack_source': _source_link(catalog, source), 'status': status,
            'native_routes': routes, 'guidance_stages': stages}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('list', help='List scenarios with local guides')
    scenario = commands.add_parser('scenario', help='Find the local entry point for a known task')
    scenario.add_argument('name', choices=SCENARIOS)
    show = commands.add_parser('show', help='Look up a pstack skill name, playbook, or exact source path')
    show.add_argument('entry')
    args = parser.parse_args(argv)
    try:
        catalog = _load_catalog()
        result = {'pstack_version': catalog['version'], 'pstack_commit': catalog['commit']}
        if args.command == 'scenario':
            result.update(_scenario(args.name, catalog))
        elif args.command == 'show':
            result.update(_show(args.entry, catalog))
        else:
            result['scenarios'] = {name: value.goal for name, value in SCENARIOS.items()}
    except (OSError, KeyError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
