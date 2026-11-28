---
name: unslop
description: Remove formulaic language and filler from a selected text while preserving meaning and tone.
disable-model-invocation: true
---

# Unslop

Use the original text and the intended reader as input. Flag real reading problems with the pinned rule IDs. Remove inflated terms, generic claims, unnecessary hedges, and repetitive phrasing when the edit helps the reader. Preserve facts, commitments, useful uncertainty, the author's tone, and necessary terminology.

Run `python3 -m workbench.technical_writing collect --mode unslop --action rewrite --format prose --input /absolute/original.md --out /absolute/writing-input.json`. Choose `--action audit` for findings only. Follow the review, inspect, verify, and export commands in [Technical writing](../technical-writing/SKILL.md). Compare the proposed diff with the original. The rule signals are editing clues. The independent check must establish fidelity before export.
