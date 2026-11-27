---
name: teach
description: Teach how a selected subsystem works and why it was designed that way. Use for an explanation, walkthrough, or progressive lesson.
disable-model-invocation: true
---

# Teach

Use the current question and conversation to select the depth and starting point. Open the relevant committed source, then run the [read-only investigation](../investigate/SKILL.md) in `teach` mode. It runs `how` and `why` independently before synthesizing their results. Supply a concrete event or input from the question when it helps explain the mechanism.

For a flow with several moving parts, use `--lesson` to save a progressive HTML explanation beside the JSON report. Start with the smallest useful figure. Each next figure shows the previous one plus one connected part or one arrow. Use plain text for a small point. When position or a visual before and after is the subject, show the actual view or draw a short series if the host has an appropriate image tool.

Give the short answer first, then explain one concrete path. Use the plain wording from [unslop](../unslop/SKILL.md). Preserve the confidence of the original `why` findings, including any gaps in the history. Stop at a natural point so the reader can lead the next part of the conversation.
