"""Adapt the pinned agent roles to iBoard's existing authority boundaries."""

POTETO_AGENT = 'agents/poteto-agent.md'
WORKER = 'skills/poteto-mode/worker.md'
COMMENT_REVIEWER = 'agents/comment-sicko.md'
BENNY_OPERATOR = 'automations/benny/FOR_AGENTS.md'

DELEGATE = ((POTETO_AGENT,), (
    'The coordinator owns the selected workflow, task scope and acceptance conditions. Do the assigned stage, '
    'make reversible choices within its scope, and report what the supplied evidence supports. '
    'An unrelated problem already present on the baseline belongs in the handoff.'))

WORKER_CONTRACT = ((WORKER,), (
    'Use the configured tools within the assigned workspace. Preserve the requested behavior and return '
    'concrete observations, edits or a blocker in the stage schema. Do not add acceptance gates or '
    'take over coordination, publication, or other external writes.'))

SPECIALISTS = {
    'comment-review': ((
        (COMMENT_REVIEWER,),
        'Review only the selected comments and nearby code. Remove narration and obsolete workarounds. '
        'Preserve legal and public contract text, and require evidence for an external constraint. Flag '
        'the exact local symbol when code should express the constraint or a correctness suppression '
        'needs fixing. Report these changes for follow-up and leave application code untouched.',
    ),),
    'benny-triage': ((
        (BENNY_OPERATOR,),
        'Keep the selected source thread fixed. Classify its report using the supplied evidence, '
        'prefer a well-supported duplicate or existing fix, and withhold a new ticket when the '
        'report or tracker state is uncertain. Treat bot chatter as evidence. The coordinator owns '
        'all Slack and tracker writes, and any later repair can deliver only a verified draft PR.',
    ),),
    'swarm-investigation': (DELEGATE, WORKER_CONTRACT),
}

BENNY_REPAIR = ((BENNY_OPERATOR,), (
    'Preserve the selected source, the reported symptom and the original failed and fixed checks. '
    'An existing fix belongs to verification. Use only the assigned evidence and local files. '
    'The coordinator owns Slack and GitHub operations, and any permitted PR stays a draft.'))
