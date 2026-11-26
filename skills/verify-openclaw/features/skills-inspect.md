# List and inspect workspace skills

## Sub-features

The skills list shows eligible skills in the selected workspace. A named skill exposes its metadata and exact source path. An absent name returns a structured error and a failing exit code.

## How to get to it (user POV)

Run `openclaw skills list --agent main --json` or `openclaw skills info <name> --agent main --json` in a workspace with a configured main agent.

## Driving it with iBoard

Run `python3 -m workbench.verify_openclaw skills-inspect` with the local attested image and a fresh evidence path. The helper seeds one disposable workspace skill, reads the list and detail JSON, checks an absent name, and verifies that the fixture and configuration contents stayed intact.

## Gotchas

Bundled skills are disabled to keep the fixture deterministic. Installation, library, search and Workshop have their own prerequisites in the project verification contract.
