---
name: ketchup-video
description: Catch the operator up in a short silent video using existing evidence and separately list the decisions that are still waiting.
disable-model-invocation: true
---

# Ketchup video

Start with the last human message before this invocation. Use actual receipts, captured screenshots, recording frames, and source changes for the slides. Check the live state of selected tasks and PRs. Keep earlier decisions that still wait on the operator. Record the required action, the gain and cost of each real choice, and a recommendation. Keep work the agent can do in a separate line.

1. From the iBoard root run `python3 -m workbench.ketchup_video collect --repo /absolute/project --transcript /absolute/session.jsonl --invocation-line LINE --manifest /absolute/video-input.json --out /absolute/evidence.json`. The transcript is a selected Claude Code JSONL export, with the current slash command at LINE. Keep output files outside the target project.
2. Use `plan --evidence /absolute/evidence.json --sha256 EVIDENCE_SHA --model MODEL --out /absolute/storyboard.json` on the same module. Or pass a hand-edited JSON file with `--storyboard /absolute/visual-plan.json`. Keep one connected diagram and change the focus on each slide. Use a short subtitle with one point per slide. Pin any shown number to an observed data field.
3. Run `render --plan /absolute/storyboard.json --sha256 PLAN_SHA --out /absolute/video-output`. Chromium, FFmpeg, and FFprobe must be available. The renderer checks text fit, moves between slides, and measures each hold from the decoded video frames. Inspect a decoded frame of every slide at chat size before sharing. If a label or subtitle is hard to read, revise the storyboard and render again.
4. Use `verify --receipt /absolute/video-output/verification.json --sha256 RECEIPT_SHA` to recheck the video and its still frames. Read `reply.json` for the opening, the approved video, the remaining agent work, and the waiting calls. If rendering failed, give the short failure message and the decisions. Keep the exact option names.

The input manifest accepts `evidence` and optional `task`, `pull_requests`, and `pending` lists. An evidence entry has a kind, path, and SHA-256. Give an ISO timestamp with `at`, or select the artifact's JSON time with `time_field`. A data artifact uses `facts` to map short names to JSON paths. For example, `"facts":{"runs":["verification","offline_runs"]}` selects the real count. A diff entry uses `lines` with its first and last lines. Use a real PNG for a screenshot. For a recording give a selected MP4, WebM, or MOV and `sample_seconds`. For evidence from before the video window, mark `earlier_for_decision` and cite it only in a pending decision. Cite collected IDs such as `E1` and `P1`.

A storyboard has a single `takeaway`, `nodes` with ID, label and evidence IDs, `links` as pairs of node IDs, `slides`, and `remaining_work`. Each slide names its title, point, focus node, subtitle, and evidence IDs. An optional number names its evidence ID, field, and label. An excerpt names its source ID. A media entry names an observed screenshot or recording and a label. Render at most ten slides. Use a plain flow diagram when useful. Draw a real UI only from a capture.
