---
name: author-skill
description: Write or revise a skill, check its committed metadata and links, and hand its evidence to the iBoard PR gate.
---

# Author a skill

Work in the task's isolated worktree. Write the instructions around the actual user request and the choices the skill needs to make. Keep the name and description good enough for the intended host to discover the skill. Link important source files, companion skills, and any resources the agent must read. For an edit, preserve existing host invocation settings.

For a new guide, put the instructions in a reviewed Markdown file and draft the skill into the chosen project skill root.

```sh
python3 -m workbench.skill_authoring draft --name "$NAME" \
  --description "$DESCRIPTION" --body "$BODY" --root "$SKILLS_DIR"
```

Commit the candidate before running the check. It validates frontmatter and local Markdown references using the committed files, including references from other skills affected by the edit. Use the target host to check discovery when loading or invocation changes.

```sh
python3 -m workbench.skill_authoring check --repo "$WORKTREE" \
  --base "$BASE" --candidate "$HEAD" --out "$RECEIPT"
```

When the skill adds an executable or machine-readable rule, choose a focused committed test. Changed files under the skill's `scripts/` directory and changed executable skill resources require a probe. Add `--probe tests/skill_contract.py --image "$LOCAL_IMAGE_ID"` and any required `--include PATH` values to the check. The image is a local immutable ID. The probe runs twice in separate offline containers with a read-only source snapshot. A wording-only edit can use the metadata and link checks.

Attach the passing receipt to the existing task with `skill_authoring attach --task … --receipt … --sha256 …`. For an authorized PR, `skill_authoring publish` accepts the same flags plus `--repository OWNER/REPO` and calls iBoard's existing publication gate. Include the skill's intended use, meaningful design choices and verification evidence in the handoff.
