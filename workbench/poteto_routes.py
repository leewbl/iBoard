"""Map the pinned pstack playbooks to the existing iBoard command boundaries."""

from typing import NamedTuple


class Action(NamedTuple):
    module: str
    prefix: tuple = ()
    fixed: tuple = ()
    effect: str = 'local'


class Playbook(NamedTuple):
    trigger: str
    scenario: str
    actions: dict


def stages(module, **effects):
    return {name.replace('_', '-'): Action(module, (name.replace('_', '-'),), effect=effect)
            for name, effect in effects.items()}


def start(kind):
    return Action('start', fixed=('--kind', kind), effect='selected-task')


PLAYBOOKS = {
    'authoring-a-skill': Playbook('Write or revise a skill and check the candidate before delivery.', 'author-skill',
        {**stages('skill_authoring', draft='local', check='local', attach='local', publish='inherited-delivery'),
         'maintenance-source-review': Action('project_source_review'),
         'maintenance-check': Action('project_verify_delivery', ('check',)),
         'maintenance-attach': Action('project_verify_delivery', ('attach',)),
         'maintenance-publish': Action('project_verify_delivery', ('publish',), effect='inherited-delivery')}),
    'autonomous-run': Playbook('Drive one explicitly approved task until its measurable condition is satisfied.',
        'autonomous-run', stages('autonomous_run', freeze='local', run='inherited-delivery')),
    'autopilot-full': Playbook('Supervise several already approved tasks through their original delivery policies.',
        'autopilot-full', stages('autopilot_full', freeze='local', run='inherited-delivery',
                                 show='observe', hold='local', release='local')),
    'autopilot-stack': Playbook('Reverify or prepare a stack of related PRs for handoff.', 'prepare-stack',
        stages('stack_autopilot', freeze='local', run='observe')),
    'babysit': Playbook('Observe PR CI, conflicts, reviews and readiness or wait on a selected PR.', 'pr-status',
        {name: Action('pr_watch', fixed=('--mode', name), effect='observe')
         for name in ('check', 'drive', 'threads-only')}),
    'bug-fix': Playbook('Reproduce and repair a reported observable fault.', 'fix-bug', {'start': start('bug')}),
    'eval': Playbook('Blindly compare multiple implementations or models under a shared rubric.', 'blind-eval',
        stages('blind_eval', freeze='local', run='local', audit='observe')),
    'feature': Playbook('Design and implement a product requirement or capability.', 'build-feature',
        {'design': Action('architect', ('plan',)), 'start': start('feature')}),
    'hillclimb': Playbook('Iterate on one measured objective, preserving functional constraints.', 'improve-performance',
        {'run': Action('hillclimb')}),
    'investigation': Playbook('Explain code or design with source and history, without implementing.', 'investigate',
        {name: Action('investigate', (name,), effect='observe')
         for name in ('investigation', 'how', 'why', 'teach')}),
    'multi-phase-plan': Playbook('Design or execute a dependency-ordered change across separately verified phases.',
        'plan-phases', {'plan': Action('multiphase'), 'dispatch': Action('phase_dispatch', ('run',)),
                        'approve': Action('phase_dispatch', ('approve',)),
                        'deliver': Action('phase_ship', effect='inherited-delivery'),
                        'audit': Action('phase_audit', effect='observe')}),
    'opening-a-pr': Playbook('Prepare and publish one independently verified candidate as a PR.', 'open-verified-pr',
        stages('pr_brief', prepare='observe', attach='local', open='inherited-delivery')),
    'pause-safely': Playbook('Hold a named running task and record its safe continuation point.', 'pause-task',
        stages('run_control', hold='local', show='observe')),
    'perf-issue': Playbook('Measure and fix a single performance fault at the actual affected entry point.', 'perf-issue',
        {**stages('perf_issue', freeze='local', verify='local'),
         'prepare': Action('start', fixed=('--kind', 'bug', '--prepare-only')),
         'fix': Action('resume', effect='inherited-delivery')}),
    'prototype': Playbook('Settle a testable architectural or interaction choice with a small trial.', 'prototype',
        {'run': Action('prototype')}),
    'refactoring': Playbook('Change structure while preserving existing behavior across the relevant entry points.',
        'refactor', {**stages('refactor', freeze='local', verify='local'), 'start': start('refactor')}),
    'runtime-forensics': Playbook('Capture live CPU, heap or Chromium evidence inside the isolated runtime.', 'capture-cpu',
        {**stages('runtime_forensics', cpu='local', heap='local'), 'browser': Action('browser_forensics')}),
    'session-pickup': Playbook('Find a checkpoint from earlier work, or resume its retained task.', 'pickup-history',
        {'inspect': Action('pickup', effect='observe'), 'history': Action('pickup_history', effect='observe'),
         'resume': Action('resume', effect='inherited-delivery')}),
    'shipping': Playbook('Land a ready PR stack in dependency order after independent verification.', 'ship-stack',
        {**stages('stack_ship', plan='observe', land='inherited-delivery'),
         'continue': Action('stack_rebase', ('continue',), effect='inherited-delivery'),
         'verify': Action('verify_pr', effect='observe')}),
    'trace-forensics': Playbook('Explain a trace already captured and identify the supported observations.', 'analyze-trace',
        {'inspect': Action('trace_forensics', effect='observe')}),
    'visual-parity': Playbook('Compare real baseline and candidate screenshots for pixel-exact states.',
        'prove-visual-parity', stages('visual_parity', baseline='local', compare='local')),
    'worktree-cleanup': Playbook('Inventory Git worktrees, simulator resources and developer caches before approved removal.',
        'worktree-cleanup', {**stages('worktree_cleanup', inventory='observe', freeze='local', apply='approved-cleanup'),
         'resource-inventory': Action('resource_cleanup', ('inventory',), effect='observe'),
         'resource-freeze': Action('resource_cleanup', ('freeze',)),
         'resource-apply': Action('resource_cleanup', ('apply',), effect='approved-cleanup')}),
    'figure-it-out': Playbook('Specify a verifiable exit condition for broad work without a suitable existing playbook.',
        'figure-it-out', stages('figure_it_out', freeze='local', run='local', audit='observe')),
}
