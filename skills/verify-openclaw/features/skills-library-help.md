# Discover the skill library commands

Run `openclaw skills library --help` to see the available operations. Running `openclaw skills library` with no subcommand shows the same set. Run `openclaw skills library read --help` for the required skill ID and optional revision and session selectors.

The `skills-library-help` journey checks both parent invocations and the read help in an isolated home. Both parent outputs must list exactly the 15 commands found at the factory call sites. The test preserves all three outputs and checks that the isolated configuration is unchanged before cleaning up.

The committed source map traces calls to `leaf` in `src/cli/skills-library-cli.ts` back to the `library.command(name)` registration. The [personal library lifecycle](skills-library-lifecycle.md) checks authenticated listing, revision changes and session selections. A separate [import journey](skills-library-import.md) covers directory, ZIP and registry sources. The inventory records the remaining prerequisites for team ownership.
