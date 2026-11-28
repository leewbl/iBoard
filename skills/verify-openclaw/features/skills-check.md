## Sub-features

Discover a skill in the active workspace, explain a missing prerequisite, and show the skill as eligible when the prerequisite is present. Check that the command leaves the source skill files and configuration intact.

## How to get to it (user POV)

Install a skill into the workspace and run `openclaw skills check --json`. A skill that requires an environment variable should appear under `missingRequirements` when the variable is absent and under `eligible` once it is set.

## Driving it with iBoard

Use the [verification entry point](../SKILL.md) with the `skills-check` journey and an unused report path. The helper creates two workspace skills with synthetic data. It runs the real CLI in separate processes with the required environment variable absent and then present. The report shows the missing prerequisite, both eligible fixture names on the second run, and whether the input files remained unchanged.

## Gotchas

This fixture covers workspace discovery and environment-based eligibility in the JSON CLI. It uses temporary state, disables plugins, and needs neither a running Gateway nor a provider account. It leaves other behaviors such as human-readable formatting, global discovery, plugin sources and external skill installation to separate checks.

The delivery gate selects this journey for changes in `src/skills/`, `src/cli/skills-cli*` (the CLI and its companion files), `src/shared/frontmatter.ts`, and `src/agents/agent-scope-config.ts`. Task-specific verification should cover relevant changes elsewhere.
