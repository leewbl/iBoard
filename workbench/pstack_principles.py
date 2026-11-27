"""Stage guidance adapted from the 24 pstack engineering principles."""

PRINCIPLES = {
    'skills/principle-attack-the-premise/SKILL.md': (
        ('repair', 'premise-diagnosis'),
        'When two fixes based on the same assumption fail the same gate, record the assumption and make an '
        'observation that would distinguish it from a competing explanation before making the next edit.',
    ),
    'skills/principle-boundary-discipline/SKILL.md': (
        ('implementation',),
        'When changing an external boundary, validate and translate raw input at entry, then keep domain logic '
        'independent of transport and framework types.',
    ),
    'skills/principle-build-the-lever/SKILL.md': (
        ('plan', 'implementation'),
        'For nontrivial work, prove one concrete example first and use the smallest rerunnable command or tool '
        'that reproduces the result. Reuse a suitable tool already in the project.',
    ),
    'skills/principle-encode-lessons-in-structure/SKILL.md': (
        ('repair',),
        'When a correction recurs, encode it in the smallest applicable type or executable check. When it '
        'requires judgment, record a concise rule and a concrete example that distinguishes the failure.',
    ),
    'skills/principle-exhaust-the-design-space/SKILL.md': (
        ('plan',),
        'For a novel interaction or architecture decision within scope, compare two or three small prototypes '
        'under the same user task and criteria when the budget and available tools permit.',
    ),
    'skills/principle-experience-first/SKILL.md': (
        ('plan', 'final-review'),
        'When product choices are in scope, judge the core workflow from the end user or caller perspective. '
        'Include the feedback or error state they encounter when it matters to acceptance.',
    ),
    'skills/principle-explain-the-number/SKILL.md': (
        ('plan', 'final-review'),
        'For a benchmark or evaluation, state the decision threshold and expected noise before measuring. '
        'Report run count, spread and any observed limiter, and verify equivalent completed work and cache conditions.',
    ),
    'skills/principle-fix-root-causes/SKILL.md': (
        ('baseline-review', 'repair'),
        'For a defect, trace the reproduced symptom to the earliest wrong decision. Handle valid states where '
        'they fail and investigate the origin of states that should be impossible.',
    ),
    'skills/principle-foundational-thinking/SKILL.md': (
        ('plan',),
        'For foundational changes, trace the main access paths, write down the invariants and sequence the '
        'supporting structures and checks before the work that depends on them.',
    ),
    'skills/principle-guard-the-context-window/SKILL.md': (
        ('baseline-review', 'repair', 'final-review'),
        'When logs or source are large, use bounded excerpts with references to the retained originals. '
        'Identify any missing evidence that could change the conclusion.',
    ),
    'skills/principle-laziness-protocol/SKILL.md': (
        ('implementation',),
        'Keep the patch as small as the approved behavior allows. Put an existing decision in one place and '
        'reuse its result at each caller.',
    ),
    'skills/principle-make-operations-idempotent/SKILL.md': (
        ('implementation', 'final-review'),
        'For operations that can restart or retry, derive the next step from current state, reconcile partial '
        'results and verify that repeated execution converges without duplicate effects.',
    ),
    'skills/principle-migrate-callers-then-delete-legacy-apis/SKILL.md': (
        ('implementation', 'final-review'),
        'When replacing an internal API with no external compatibility commitments, migrate every caller and '
        'remove the old entry in the same change. Verify the new contract and preserve supported consumers.',
    ),
    'skills/principle-minimize-reader-load/SKILL.md': (
        ('implementation',),
        'When touching hard-to-trace code, simplify forwarding layers and narrow mutable state so a reader '
        'can locate the source and the writers of a value.',
    ),
    'skills/principle-model-the-domain/SKILL.md': (
        ('plan', 'implementation'),
        'When state rules are scattered, express legal states and transitions in a clear domain model. '
        'Use a new structure only where it removes real branches or invalid states.',
    ),
    'skills/principle-never-block-on-the-human/SKILL.md': (
        ('implementation', 'repair'),
        'Make and record reversible choices within the approved task and budget. Keep review gates in force '
        'and obtain authorization for decisions or external actions outside the approved scope.',
    ),
    'skills/principle-outcome-oriented-execution/SKILL.md': (
        ('plan', 'final-review'),
        'For a planned migration, define the intended end state and any allowed temporary mismatch at explicit '
        'reversible boundaries. Verify the final state with the required static and runtime checks.',
    ),
    'skills/principle-prove-it-works/SKILL.md': (
        ('baseline-review', 'final-review'),
        'Observe the actual artifact through its real entry point, retain the command and result, and check '
        'that the observation addresses the criterion before accepting it.',
    ),
    'skills/principle-redesign-from-first-principles/SKILL.md': (
        ('plan',),
        'If an approved requirement changes a foundational assumption, trace the affected design and choose '
        'a coherent structure for the new invariant. Plan verifiable increments across affected consumers.',
    ),
    'skills/principle-separate-before-serializing-shared-state/SKILL.md': (
        ('plan', 'implementation'),
        'When concurrent actors might write together, give independent facts separate write targets. Enforce '
        'access with a lock or single writer only where one shared target is necessary.',
    ),
    'skills/principle-sequence-verifiable-units/SKILL.md': (
        ('plan',),
        'For multi-step work, order small units with a baseline and a specific check for each transition. '
        'Put the failing observation before the fix and advance after the current unit is proven.',
    ),
    'skills/principle-subtract-before-you-add/SKILL.md': (
        ('implementation',),
        'Before adding a mechanism, remove any dead path or redundant rule in the approved change. Preserve '
        'existing behavior and verify the simpler path with the retained checks.',
    ),
    'skills/principle-test-behavior-not-implementation/SKILL.md': (
        ('baseline-review', 'final-review'),
        'Check concrete results, errors or effects through the public interface. Require the check to catch '
        'the wrong behavior and survive an internal change that preserves all observable results.',
    ),
    'skills/principle-type-system-discipline/SKILL.md': (
        ('implementation', 'final-review'),
        'For typed code, distinguish primitives with different meanings, derive shapes from the authoritative '
        'schema and handle every declared variant. Verify compiler evidence alongside the relevant behavior.',
    ),
}
