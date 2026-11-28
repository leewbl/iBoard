| Journey | User-facing feature | Evidence |
| --- | --- | --- |
| [gateway-health](gateway-health.md) | Check whether a fresh Gateway can answer a real health RPC | Readiness and liveness HTTP responses, CLI health JSON, child exit, Docker cleanup |
| [skills-check](skills-check.md) | Discover workspace skills and see which prerequisites are missing | CLI JSON with and without an environment prerequisite, unchanged fixture files, Docker cleanup |
| [skills-inspect](skills-inspect.md) | List local skills and view the details of a named skill | CLI JSON for list, detail and an absent name, unchanged fixture files, Docker cleanup |
| [skills-library-help](skills-library-help.md) | Find library commands and the options for reading a revision | Exact names from bare and explicit parent help, read help, unchanged configuration, Docker cleanup |
| [workshop-roundtrip](workshop-roundtrip.md) | Archive a learned skill and restore its original content | CLI list before and after archive and restore, archive version, restored content, Docker cleanup |
| [config-validate](config-validate.md) | Check the validity of a local configuration | Exit codes and JSON for valid and invalid port types, unchanged fixture inputs, Docker cleanup |

The core `audit` runs the six journeys above. A compiled Control UI image with Chromium supports eight more journeys.

- [`control-ui-connected`](../SKILL.md#drive) checks the login form, the authenticated Gateway session-list response and the empty Sessions page.
- [`control-ui-session-rename`](../SKILL.md#session-rename) checks cancellation, a successful rename and an authenticated read of the same updated session after the Gateway restarts.
- [`control-ui-session-delete`](../SKILL.md#selected-session-deletion) checks cancellation, deletion of one selected session, and persistence after restart with the unselected session still present under the same ID.
- [`control-ui-session-status-filter`](../SKILL.md#session-status-filter) compares authenticated Gateway results and visible rows for Active, Archived and All.
- [`control-ui-session-archive-restore`](../SKILL.md#session-archive-and-restore) runs both row actions and checks that the same session ID survives two separate Gateway restarts.
- [`control-ui-session-archive-subtree-undo`](../SKILL.md#nested-session-archive-and-undo) checks cancellation, confirmed batch archive of a parent and child, and durable Undo.
- [`control-ui-session-sidebar-archive-automations`](../SKILL.md#sidebar-multi-select-and-automation-pause) checks the sidebar's Alt-click selection, named pause warning, targeted automation pause and session Undo.
- [`control-ui-session-archive-running`](../SKILL.md#archive-a-running-session) checks that cancellation leaves a real worker running, confirmation stops it for the original run ID, and the selected session stays archived after restart.

The PR gate requires the Workshop roundtrip for changes to `src/cli/skills-cli.ts` or under `src/skills/workshop/`. Changes under `ui/src/pages/sessions/` or `ui/src/lib/sessions/`, or to `ui/src/styles/sessions.css`, require all seven session journeys. Changes to `ui/src/lib/session-rename.ts` require rename, and changes to `src/gateway/server-methods/sessions-delete.ts` require deletion. The archive journeys cover the shared session menu and Gateway patches. The status and archive journeys cover the Gateway list and shared row encoding. The batch journeys cover confirmation and Undo. The sidebar journey also covers selection, the automation warning and server-side pause effects. The running journey also covers archive drain, abort settlement, and CLI process termination.
