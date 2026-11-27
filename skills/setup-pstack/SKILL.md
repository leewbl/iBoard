---
name: setup-pstack
description: Confirm models and reasoning options, choose role settings, and install an approved iBoard model profile.
disable-model-invocation: true
---

# Set up role models

Follow the [pinned pstack model setup](https://github.com/cursor/plugins/blob/41e400932eca14f289e7db717a70455256b6d388/pstack/skills/setup-pstack/SKILL.md). In iBoard, keep the profile as a separate local JSON file. Existing Claude Code and Codex host settings provide transport and login. A newly started task explicitly selects the profile it will use.

## Confirm and approve

1. Supply a local probe manifest with `schema: 1`, `seconds`, and `engines`. Each engine has a `provider` (`glm` or `openai`), an exact `model`, and an `efforts` list drawn from `low`, `medium`, `high`, and `xhigh`. Obtain the model IDs from the current host configuration or the operator. Confirm each selected pair with a small tool-free request. A model identity mismatch, failed request, or timeout leaves later pairs for that provider unverified.
2. Write the choices JSON with `schema: 1`, `budget` (`large`, `medium`, or `small`), `parent` (a provider/model pair), and `roles`. Use `inherit-parent` or `auto` to resolve to that explicit parent route, or provide an explicit provider/model pair. `arena-runners`, `arena-cross-judge-pool`, and `interrogate-reviewers` accept ordered lists. Cross-judge selection skips aliases and candidates from the same vendor as the parent.
3. Draft the complete table and show each role's approved model, effort ceiling and available effort. Preserve the requested provider and model while selecting the strongest confirmed effort at or below that role's budget. Resolve missing configurations before installation. Choose and approve the resulting digest.

```sh
python3 -m workbench.model_profiles probe --manifest "$PROBE_SPEC" --out "$INVENTORY"
python3 -m workbench.model_profiles draft --inventory "$INVENTORY" --inventory-sha256 "$INVENTORY_SHA" \
  --choices "$CHOICES" --out "$DRAFT"
python3 -m workbench.model_profiles install --profile "$DRAFT" --sha256 "$DRAFT_SHA" --out "$PROFILE"
python3 -m workbench.model_profiles show --profile "$PROFILE" --sha256 "$PROFILE_SHA"
```

Use `draft --previous "$PROFILE" --previous-sha256 "$PROFILE_SHA"` to keep role selections that the new choices omit. To replace the installed file, provide its expected digest with `install --previous-sha256`. Changes affect tasks that select the new profile. Existing tasks stay bound to the original snapshot.

## Use it on a task

Append `--model-profile "$PROFILE" --model-profile-sha256 "$PROFILE_SHA"` to [`workbench.start`](../optimize/SKILL.md). Task intake pins the file and embeds the same reference in the workflow state. The task's planning, implementation, baseline review, repair, and final review use the selected Claude Code + GLM routes. Baseline and final review retain read-only access to the existing evidence. Resume and baseline replay use the snapshot.

Model receipts record the role, the profile digest, the Claude Code init model and the effort submitted to the CLI. A matching CLI model is required for the stage to proceed. The effort records an invocation option. The probe and receipt do not measure the provider's internal reasoning. The cross-vendor pool can be inspected with `show --role arena-cross-judge-pool --other-than glm`. Use the same approved file with [Arena](../arena/SKILL.md), [Swarm model investigations](../swarm/SKILL.md) and [Interrogate](../interrogate/SKILL.md). Those runs freeze the resolved routes and retain model invocation receipts.

If the project lacks a runtime verification skill, use [create-verification-skill](../create-verification-skill/SKILL.md) to establish a real user journey. For scoped state and protocol properties, see [create-formal-verification-skill](../create-formal-verification-skill/SKILL.md).
