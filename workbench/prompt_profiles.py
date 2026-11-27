"""iBoard-owned guidance adapted from the pinned pstack source."""

import hashlib

from workbench import pstack_agents
from workbench.pstack_principles import PRINCIPLES

PSTACK_COMMIT = '41e400932eca14f289e7db717a70455256b6d388'
DELEGATED_PHASES = ('implementation', 'repair')
TYPESCRIPT_SOURCES = (
    'skills/typescript-best-practices/SKILL.md', 'skills/typescript-best-practices/references/patterns.md',
    'skills/principle-type-system-discipline/SKILL.md', 'skills/principle-boundary-discipline/SKILL.md',
)
TYPESCRIPT_GUIDANCE = (
    'When reading or editing TypeScript, use discriminated variants and the simplest total domain type. '
    'Brand primitives only when distinct meanings can be confused. Parse external unknown data once at its '
    'boundary with the project schema or a validating predicate. Derive types from that authority. '
    'Prefer discriminants and native narrowing, verify every predicate, cover variant switches exhaustively, '
    'and prefer satisfies over an unverified cast. Use named object arguments where positional calls are ambiguous '
    'and avoid unnecessary allocation on measured hot paths. Use structured diagnostics. '
    'Compile with the repository toolchain and check real caller behavior. A green type check does not validate a guard.'
)

# Each profile keeps the coordinator's existing schema, permission model and
# execution boundaries. Upstream documents are never interpolated into prompts.
PROFILES = {
    ('plan', 'bug'): (
        ('skills/poteto-mode/playbooks/bug-fix.md', 'skills/tdd/SKILL.md',
         'skills/blast-radius/SKILL.md'),
        'Identify the reported user entry point, trigger, and observable wrong result. '
        'Plan a baseline failure and a candidate check on that same surface with equivalent inputs. '
        'Map the affected callers and choose the smallest relevant negative or boundary check. '
        'Use the existing JSON schema and do not add checklist items unrelated to the request.',
    ),
    ('plan', 'feature'): (
        ('skills/poteto-mode/playbooks/feature.md', 'skills/architect/SKILL.md',
         'skills/create-verification-skill/SKILL.md'),
        'Start from the caller or user interaction. Turn the requested behavior into observable '
        'outcomes and side effects through that entry point, including compatibility or failure '
        'paths when relevant. Prefer an existing type or interface and identify any new boundary '
        'the task actually needs. Use the existing JSON schema.',
    ),
    ('plan', 'refactor'): (
        ('skills/poteto-mode/playbooks/refactoring.md', 'skills/principle-test-behavior-not-implementation/SKILL.md'),
        'Trace the affected callers. Assess the already frozen behavior contract and name the small structural '
        'change that makes the code easier to read. Preserve the contract and its checks. Choose additional '
        'existing runtime checks within the approved scope. Use the existing JSON schema.',
    ),
    ('implementation', 'refactor'): (
        ('skills/poteto-mode/playbooks/refactoring.md', 'skills/principle-subtract-before-you-add/SKILL.md'),
        'Perform the named structural change within the frozen scope. Preserve user-visible behavior, '
        'outputs and errors. Prefer removing redundant branching or indirection. Do not edit the frozen '
        'drivers, split off unrelated behavior changes, or commit changes yourself.',
    ),
    ('final-review', 'refactor'): (
        ('skills/poteto-mode/playbooks/refactoring.md', 'skills/principle-prove-it-works/SKILL.md'),
        'Read the changed callers and independent evidence. Reject a change of behavior, an untested necessary '
        'entry point, or a patch that adds complexity without reaching the approved target. Cite the frozen '
        'comparison and observe the same public inputs and outputs through the candidate source.',
    ),
    ('baseline-review', '*'): (
        ('skills/poteto-mode/playbooks/bug-fix.md', 'skills/principle-prove-it-works/SKILL.md'),
        'Verify the trigger, surface, and observed failure against the original request. '
        'A command or environment failure does not count as the reported defect. '
        'Return blocked if the run did not reach the required behavior.',
    ),
    ('implementation', '*'): (
        ('skills/principle-fix-root-causes/SKILL.md',
         'skills/principle-test-behavior-not-implementation/SKILL.md'),
        'Tie the change to the reported behavior and its demonstrated cause, or to the '
        'requested user outcome for a feature. Preserve relevant public behavior and cover '
        'it at the existing user or caller boundary. Keep unrelated edits out of the patch.',
    ),
    ('repair', '*'): (
        ('skills/principle-attack-the-premise/SKILL.md', 'skills/principle-fix-root-causes/SKILL.md'),
        'Use the retained failure to discriminate a product defect from setup or harness failure. '
        'If the evidence is inconclusive, report what would distinguish them. '
        'Keep the acceptance conditions and the ability of tests to catch the original defect.',
    ),
    ('final-review', '*'): (
        ('skills/blast-radius/SKILL.md', 'skills/principle-prove-it-works/SKILL.md',
         'skills/principle-test-behavior-not-implementation/SKILL.md'),
        'Check that the observed result came through the affected user or caller entry point. '
        'Look for one plausible regression in a related caller, state transition, or side effect. '
        'For a necessary check that is missing or only covered by an irrelevant proxy, return '
        'blocked and name the missing observation. Cite actual evidence using the existing schema.',
    ),
}


def _bundle(phase, kind, sections):
    instruction = '\n'.join(text for _, text in sections)
    parts = [{'sources': list(sources), 'sha256': hashlib.sha256(text.encode()).hexdigest()}
             for sources, text in sections]
    return instruction, {
        'pstack_commit': PSTACK_COMMIT,
        'phase': phase,
        'kind': kind,
        'sources': list(dict.fromkeys(source for sources, _ in sections for source in sources)),
        'guidance_sha256': hashlib.sha256(instruction.encode()).hexdigest(),
        'sections': parts,
    }


def guidance(phase, kind, *, delegated=False, benny=False):
    if phase.startswith(('repair-', 'ci-repair-')):
        phase = 'repair'
    profile = PROFILES.get((phase, kind)) or PROFILES.get((phase, '*'))
    sections = [profile] if profile else []
    sections.extend(((path,), text) for path, (phases, text) in PRINCIPLES.items() if phase in phases)
    if not sections:
        return '', {}
    if phase in ('plan', 'implementation', 'repair', 'final-review'):
        sections.append((TYPESCRIPT_SOURCES, TYPESCRIPT_GUIDANCE))
    if delegated and phase in DELEGATED_PHASES:
        sections.extend((pstack_agents.DELEGATE, pstack_agents.WORKER_CONTRACT))
    if benny:
        sections.append(pstack_agents.BENNY_REPAIR)
    return _bundle(phase, kind, sections)


def specialist(name):
    return _bundle(name, '*', pstack_agents.SPECIALISTS[name])


def source_stages(source):
    phases = ('plan', 'baseline-review', 'implementation', 'repair', 'final-review')
    labels = []
    for phase in phases:
        if source in guidance(phase, '*')[1].get('sources', ()):
            labels.append((phase, '*'))
        else:
            labels.extend((phase, kind) for kind in ('bug', 'feature', 'refactor')
                          if source in guidance(phase, kind)[1].get('sources', ()))
        if phase in DELEGATED_PHASES and source in (*pstack_agents.DELEGATE[0], *pstack_agents.WORKER_CONTRACT[0]):
            labels.append((phase, 'delegated'))
        if source in pstack_agents.BENNY_REPAIR[0]:
            labels.append((phase, 'benny'))
    labels.extend((name, '*') for name, sections in pstack_agents.SPECIALISTS.items()
                  if any(source in paths for paths, _ in sections))
    return labels
