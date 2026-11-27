"""Find an iBoard entry point for a task, using the pinned pstack catalog."""

import argparse
import json
from pathlib import Path
from typing import NamedTuple

from workbench.pstack_catalog import CATALOG
from workbench.prompt_profiles import PSTACK_COMMIT, source_stages


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
        'The plugin is loaded locally. Use setup-models to configure task model roles and the reasoning budget.'),
    'setup-models': Scenario(
        'Probe available models and approve per-role model settings', 'skills/setup-pstack/SKILL.md',
        'skills/setup-pstack/SKILL.md', 'model_profiles probe',
        'Confirmed model options, a budget-derived role table, and the exact task snapshot',
        'Use the same approved profile with the main iBoard stages, Arena or Swarm model investigations.'),
    'help': Scenario(
        'Find a workflow for a task', HELP_SOURCE, 'skills/poteto-help/SKILL.md',
        'pstack_help', 'The pinned source, local guide, available entry point and its evidence requirements'),
    'poteto-mode': Scenario(
        'Choose and execute a pinned pstack workflow', 'skills/poteto-mode/SKILL.md',
        'skills/poteto-mode/SKILL.md', 'poteto_mode list',
        'A cited selection, an explicit stage plan and per-stage native receipts',
        'Route selection gives advice. Run only the exact approved native plan.'),
    'in-your-own-words': Scenario(
        'Confirm the goal, underlying problem, constraints and unknowns', 'skills/in-your-own-words/SKILL.md',
        'skills/in-your-own-words/SKILL.md', 'intent_review collect --mode in-your-own-words',
        'Restatement with exact original request line references'),
    'bro': Scenario(
        'Make the prior reply easier to understand', 'skills/bro/SKILL.md',
        'skills/bro/SKILL.md', 'intent_review collect --mode bro',
        'A plain-language restatement that retains the original numbers'),
    'reframe': Scenario(
        'Find useful technical language grounded in opened primary sources', 'skills/reframe/SKILL.md',
        'skills/reframe/SKILL.md', 'intent_review collect --mode reframe',
        'Verified quotes, their primary URLs, relevant analogies and a task brief',
        'Select public primary references before collecting. Incomplete evidence withholds the brief.'),
    'sharpen': Scenario(
        'Deepen a task brief within its original scope', 'skills/sharpen/SKILL.md',
        'skills/sharpen/SKILL.md', 'intent_review collect --mode sharpen',
        'Concrete source material, independent checks, proposed experiments and a revised task brief'),
    'technical-writing': Scenario(
        'Draft or review technical documentation, a PR brief or a commit message', 'skills/technical-writing/SKILL.md',
        'skills/technical-writing/SKILL.md', 'technical_writing collect --mode technical-writing',
        'A selected document structure, source-linked findings and an independently verified draft'),
    'unslop': Scenario(
        'Remove formulaic prose while keeping meaning and tone', 'skills/unslop/SKILL.md',
        'skills/unslop/SKILL.md', 'technical_writing collect --mode unslop',
        'Rule IDs, a proposed diff and independent fidelity checks'),
    'typescript-best-practices': Scenario(
        'Review TypeScript design and verify the selected project with the compiler',
        'skills/typescript-best-practices/SKILL.md', 'skills/typescript-best-practices/SKILL.md',
        'typescript_discipline check',
        'Compiler identity, baseline and independent candidate observations, and source-reviewed type findings',
        'Use a local source image with the compiler API and project dependencies already installed.'),
    'how': Scenario(
        'Explain how code works', 'skills/how/SKILL.md', 'skills/investigate/SKILL.md',
        'investigate how', 'A source-backed explanation for an exact repository revision'),
    'blast-radius': Scenario(
        'Find and prove downstream behavior that a change could break', 'skills/blast-radius/SKILL.md',
        'skills/blast-radius/SKILL.md', 'blast_radius analyze',
        'Source-anchored impact facts, reproducible mismatches, paired runtime evidence and a publication gate',
        'Select exact downstream source and approved existing probes. Unsupported facts remain visibly unproven.'),
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
    'prove-bug-cause': Scenario(
        'Establish the mechanism behind a specified bug', 'skills/poteto-mode/playbooks/bug-fix.md',
        'skills/bug-cause/SKILL.md', 'start --kind bug --require-root-cause',
        'Same-entrypoint trigger and control on the baseline and candidate, with an independent verdict',
        'Use a committed driver with one variable argument and an observable faulty and healthy outcome.'),
    'swarm': Scenario(
        'Verify required paths or compare candidates in parallel', 'skills/swarm/SKILL.md',
        'skills/swarm/SKILL.md', 'swarm freeze',
        'One receipt per lane, a combined verdict and the frozen race rule when selected',
        'The native runner uses committed commands in offline Docker. Required coverage can gate task delivery.'),
    'swarm-investigate': Scenario(
        'Investigate selected source ranges with parallel model workers', 'skills/swarm/SKILL.md',
        'skills/swarm/SKILL.md', 'swarm_investigate freeze',
        'Citation-validated findings per selected scope and a frozen comparison rule when requested',
        'Explicit GLM or OpenAI models receive selected source excerpts in restricted read-only sessions.'),
    'blind-eval': Scenario(
        'Compare task variants under the same request and a hidden rubric', 'skills/poteto-mode/playbooks/eval.md',
        'skills/blind-eval/SKILL.md', 'blind_eval freeze',
        'Observed input reads, offline execution receipts, cited scorecards and an auditable recommendation',
        'The runner uses selected committed files, separate GLM workspaces, and two blinded OpenAI review sessions.'),
    'correct': Scenario(
        'Prevent a repository mistake that has happened at least twice', 'skills/correct/SKILL.md',
        'skills/correct/SKILL.md', 'correct freeze',
        'Distinct historical misses, repaired controls, and the same guard passing on current code',
        'Supply exact bad/fix commits and a reviewed probe. The harness replays the selected source in offline Docker.'),
    'automate-me': Scenario(
        'Turn recurring working conventions into a personal skill', 'skills/automate-me/SKILL.md',
        'skills/automate-me/SKILL.md', 'automate_me collect',
        'Workspace-scoped text evidence, cross-session proposals and a draft of the confirmed rules',
        'Select transcripts explicitly. Approve the evidence digest before the read-only model call.'),
    'reflect': Scenario(
        'Learn from a session and update selected local skills', 'skills/reflect/SKILL.md',
        'skills/reflect/SKILL.md', 'reflect collect',
        'Cited edits, rejected findings, an executable-check backlog and a local application receipt',
        'Review the destinations and select the exact edit IDs before applying them.'),
    'author-skill': Scenario(
        'Author or revise a skill and verify it before opening a PR',
        'skills/poteto-mode/playbooks/authoring-a-skill.md', 'skills/author-skill/SKILL.md', 'skill_authoring check',
        'Exact-commit metadata, link checks, and retained offline structural results',
        'Use the normal iBoard publication gate after attaching the successful skill receipt.'),
    'no-comments': Scenario(
        'Review scoped comments, suppressions and workaround code',
        'skills/no-comments/SKILL.md', 'skills/no-comments/SKILL.md', 'no_comments collect',
        'Verified comment-only patch and cited follow-up actions for suppressions and constraints',
        'Select exact files or a working diff. Approval applies only to ordinary comments.'),
    'recall': Scenario(
        'Recover project decisions and outstanding work from previous chats', 'skills/recall/SKILL.md',
        'skills/recall/SKILL.md', 'recall collect',
        'A cited handoff, current Git and GitHub artifact states, and one concrete next action',
        'Discover Claude Code, Cursor or Codex chats in the selected workspace, or choose exact transcripts. Inspect the snapshot before model review.'),
    'review-worktree': Scenario(
        'Review uncommitted changes in independent sandboxes', 'skills/interrogate/SKILL.md',
        'skills/interrogate/SKILL.md', 'interrogate_snapshot',
        'Hash-bound candidate and retained functional review receipts',
        'Capture selected worktree files or the visible diff. The original branch and staging remain intact.'),
    'discover-functional-checks': Scenario(
        'Select existing product checks for an exact code change', 'skills/interrogate/SKILL.md',
        'skills/interrogate/SKILL.md', 'interrogate_discovery propose',
        'Diff-linked catalog selections and a plan for two independent offline runs',
        'The review panel selects approved tracked checks. Execution requires the exact selection digest.'),
    'figure-it-out': Scenario(
        'Design and execute a custom playbook when no narrower workflow fits', 'skills/figure-it-out/SKILL.md',
        'skills/figure-it-out/SKILL.md', 'figure_it_out freeze',
        'A frozen product predicate, per-hypothesis outcomes, independent reviews and a final verified patch chain',
        'The custom runner sequences the approved units and exports local evidence for the normal delivery gates.'),
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
    'verify-existing-fix': Scenario(
        'Measure a reported fault on the original version and its existing GitHub fix',
        'automations/benny/skills/reproduce-and-fix-issues/references/verify-existing-fix.md',
        'skills/reproduce-and-fix-issues/SKILL.md', 'verify_existing_fix',
        'Paired user-path evidence, two independent observations per side, and a GitHub artifact recheck',
        'This route reads the selected artifact and runs offline. It does not edit or publish a PR.'),
    'setup-benny': Scenario(
        'Check a configured workspace and read an authorized source report',
        'automations/benny/skills/setup-benny/SKILL.md', 'skills/setup-benny/SKILL.md', 'benny freeze',
        'A source receipt with its root permalink and validated workspace and verdict',
        'Uses a private operator-supplied Slack credential. Live subscriptions and tracker actions are separate.'),
    'deploy-benny': Scenario(
        'Preview, install and verify the two Benny roles from a committed project package',
        'automations/benny/FOR_AGENTS.md', 'skills/setup-benny/SKILL.md#install-and-verify-the-project-package',
        'benny_deploy preview', 'Committed Git revision, package digest and native dependency checks',
        'An installed package is inactive until its authorized event receiver and worker are configured.'),
    'receive-benny-events': Scenario(
        'Run the signed receiver and process explicitly selected report events',
        'automations/benny/templates/reproduce-automation-prompt.md',
        'skills/setup-benny/SKILL.md#run-the-two-event-roles', 'benny_automation serve',
        'Private queued event identity and a worker result bound to the source thread and deployment revision',
        'Each role uses the existing handoff, offline reproduction and draft authorization rules.'),
    'triage-issue-reports': Scenario(
        'Classify a selected Benny report and check for existing tracker items',
        'automations/benny/skills/triage-issue-reports/SKILL.md',
        'skills/triage-issue-reports/SKILL.md', 'benny_triage',
        'An unchanged source thread, quoted facts, selected code and history, and a bounded tracker recommendation',
        'Uses only the selected report and tracker searches. Produces a local verdict proposal without external writes.'),
    'benny-handoff': Scenario(
        'Review and apply the source-thread verdict and tracker handoff',
        'automations/benny/templates/triage-automation-prompt.md', 'skills/triage-issue-reports/SKILL.md',
        'benny_handoff prepare', 'Exact planned payloads, tracker evidence and a verified original-thread reply',
        'Apply is separately authorized. It uses the journal to avoid repeated writes and can close only a new issue it created.'),
    'benny-followup': Scenario(
        'Watch the original triage thread for direct questions and concrete corrections',
        'automations/benny/templates/triage-automation-prompt.md', 'skills/triage-issue-reports/SKILL.md',
        'benny_followup', 'One bounded, replayable window with cited replies and tracker correction receipts',
        'The window starts with the confirmed verdict. The coordinator stops on request and emits no second marker.'),
    'benny-reproduce': Scenario(
        'Recheck the Slack report and verify the existing fix',
        'automations/benny/skills/reproduce-and-fix-issues/SKILL.md',
        'skills/reproduce-and-fix-issues/SKILL.md', 'benny verify',
        'A source-bound route and independently repeated before/after runtime evidence',
        'Existing fixes are read-only. New edits and publication are not dispatched here.'),
    'benny-reproduce-new': Scenario(
        'Reproduce a trusted unclaimed report in offline Docker',
        'automations/benny/skills/reproduce-and-fix-issues/SKILL.md',
        'skills/reproduce-and-fix-issues/SKILL.md', 'benny_reproduce reproduce',
        'A revision-bound report, frozen drivers and repeatable primary and independent observations',
        'Requires a healthy doctor and two exact observations per probe before preparing repair.'),
    'benny-verify-candidate': Scenario(
        'Replay the reproduced report against a local candidate revision',
        'automations/benny/skills/reproduce-and-fix-issues/SKILL.md',
        'skills/reproduce-and-fix-issues/SKILL.md', 'benny_reproduce verify-candidate',
        'Two passing runs per frozen probe in fresh offline containers',
        'Verifies a local revision without writing to GitHub or Slack.'),
    'benny-repair': Scenario(
        'Run a bounded causal repair for a prepared Benny report',
        'automations/benny/skills/reproduce-and-fix-issues/SKILL.md',
        'skills/reproduce-and-fix-issues/SKILL.md', 'benny_repair',
        'Fresh ownership checks, autonomous causal repair, frozen offline replay and an independent review',
        'Applies to a prepared non-UI task. The result stays at the verified local delivery checkpoint.'),
    'review-browser-recording': Scenario(
        'Replay and independently review recorded browser evidence',
        'automations/benny/skills/reproduce-and-fix-issues/references/control-adapter.md',
        'skills/verify-openclaw/SKILL.md#browser-recordings', 'browser_recording',
        'Commit-bound browser recording, final screenshot, and a review of the sampled visible state',
        'The reviewer observes the supplied frames and final screenshot. It does not perform browser actions.'),
    'benny-draft': Scenario(
        'Submit a draft PR for a verified Benny repair',
        'automations/benny/skills/reproduce-and-fix-issues/SKILL.md',
        'skills/reproduce-and-fix-issues/SKILL.md', 'benny_draft',
        'Exact draft and branch receipt bound to the same before/after evidence and independent review',
        'Uses the configured personal fork. Existing PRs and uncertain writes are checked before any retry.'),
    'open-verified-pr': Scenario(
        'Open an independently verified task as a ready GitHub PR',
        'skills/poteto-mode/playbooks/opening-a-pr.md', 'skills/opening-a-pr/SKILL.md', 'pr_brief prepare',
        'A revision-bound title, compact brief, source-backed impact and verification receipts',
        'Uses the existing personal-fork gate. A selected parent PR must still match the independently verified base.'),
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
    'pickup-history': Scenario(
        'Recover the continuation point from an earlier chat, pushed branch, or PR',
        'skills/poteto-mode/playbooks/session-pickup.md', 'skills/pickup/SKILL.md', 'pickup_history',
        'Current branch and PR facts, inherited evidence, exact checkpoint and next playbook',
        'Optionally supply a digest-bound recall report and matching task record. The observation never executes the next step.'),
    'resume-task': Scenario(
        'Resume a paused iBoard task at its saved checkpoint', 'skills/poteto-mode/playbooks/session-pickup.md',
        'README.md#工作台插件原型', 'resume', 'A restorable task with matching inputs and a valid budget',
        'Inspect the checkpoint first. Resume may run the saved task within its original authorization.'),
    'pause-task': Scenario(
        'Hold a task at its next execution or delivery boundary', 'skills/poteto-mode/playbooks/pause-safely.md',
        'skills/pause-safely/SKILL.md', 'run_control hold',
        'An operator request ID and the verifiable checkpoint shown by run_control show',
        'Requires an explicit named task. Release is a separate action after the current step has finished.'),
    'worktree-cleanup': Scenario(
        'Inventory registered worktrees before considering cleanup', 'skills/poteto-mode/playbooks/worktree-cleanup.md',
        'skills/worktree-cleanup/SKILL.md', 'worktree_cleanup inventory',
        'Registered paths, Git state, protected references and a fresh check on each approved removal',
        'Inventory is read-only. Removal requires approval of specific worktrees and a refreshed usage snapshot.'),
    'autonomous-run': Scenario(
        'Drive an approved task to a frozen outcome', 'skills/poteto-mode/playbooks/autonomous-run.md',
        'skills/autonomous-run/SKILL.md', 'autonomous_run freeze',
        'A frozen outcome, a run journal, and exact candidate and delivery proof',
        'Uses the original task budget and observes pending CI without launching another worker.'),
    'autopilot-full': Scenario(
        'Run several approved tasks and retain each delivery outcome', 'skills/poteto-mode/playbooks/autopilot-full.md',
        'skills/autopilot-full/SKILL.md', 'autopilot_full freeze',
        'Frozen task plans, an independent outcome per task, and a queue journal',
        'Requires separate worktrees and existing delivery authorization. An operator can hold the entire queue.'),
    'catch-up': Scenario(
        'Catch up from the last human message', 'skills/ketchup/SKILL.md',
        'skills/ketchup/SKILL.md', 'ketchup', 'Exact chat window, selected task and PR state, visuals and waiting decisions',
        'Select the Claude Code command line and existing evidence. The skill writes the reply in the current chat.'),
    'ketchup-video': Scenario(
        'Make a verified silent briefing from existing evidence', 'skills/ketchup-video/SKILL.md',
        'skills/ketchup-video/SKILL.md', 'ketchup_video collect',
        'Exact transcript window, approved visual evidence, subtitled MP4 and per-slide frame measurements',
        'Select the invocation line and existing task artifacts. Render outside the target repository.'),
    'make-bot-ui': Scenario(
        'Trigger named iBoard routines from a local or tailnet page', 'skills/make-bot-ui/SKILL.md',
        'skills/make-bot-ui/SKILL.md', 'bot_ui create',
        'Panel and sender key separation, webhook receipt, scoped action and retained failed delivery',
        'Native routines work locally. Cursor webhooks and tailnet access require the corresponding accounts.'),
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
    'draw-statechart': Scenario(
        'Model-check a TLA+ specification and draw shortest violation paths', 'skills/statechart/SKILL.md',
        'skills/statechart/SKILL.md', 'statechart check',
        'TLC report, shortest paths, raw logs, Mermaid and Pele SVG within the graph budget',
        'Prepare a local pinned Java, TLC and Pele image before running the check offline.'),
    'perf-issue': Scenario(
        'Freeze an observed performance problem and remeasure the fix before shipping',
        'skills/poteto-mode/playbooks/perf-issue.md', 'skills/perf-issue/SKILL.md', 'perf_issue freeze',
        'Same-commit control, baseline and candidate profiles, material delta and publication evidence',
        'Bind the measurement before editing. Use an offline source-attested image and the same frozen workload.'),
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
    stages = [f'{phase}/{kind}' for phase, kind in source_stages(source)]
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
