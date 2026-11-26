"""iBoard-owned guidance adapted from the pinned pstack source."""

import hashlib

PSTACK_COMMIT = '41e400932eca14f289e7db717a70455256b6d388'

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


def guidance(phase, kind):
    if phase.startswith(('repair-', 'ci-repair-')):
        phase = 'repair'
    profile = PROFILES.get((phase, kind)) or PROFILES.get((phase, '*'))
    if profile is None:
        return '', {}
    sources, instruction = profile
    return instruction, {
        'pstack_commit': PSTACK_COMMIT,
        'phase': phase,
        'kind': kind,
        'sources': list(sources),
        'guidance_sha256': hashlib.sha256(instruction.encode()).hexdigest(),
    }
