# Manage personal library revisions and session selections

The `skills-library-lifecycle` journey starts a disposable single-user Gateway. Every operation goes through the real OpenClaw CLI. The CLI's token connection receives the local Gateway's durable owner profile. Use a fresh state directory, a separate input bundle, and one selected session plus an unselected control session.

1. Create a private skill and read its instruction and support file. Attach the exact revision to the target session.
2. Update only `SKILL.md` and check that the supporting file and original session pin survive. Attempt a stale update and read the current revision again.
3. Refresh the target pin to the new revision and verify that the old selection is rejected. Check that the control session still has no selection and cannot use the target's pin to read the new revision.
4. Disable and enable the skill, then roll back the current revision. Restart the Gateway and check that the owner, sessions and pinned revision still match.
5. Remove the skill and check that it disappears from the list. The owner can read the current revision by ID and see that it is marked removed. Verify that edits and new selections are rejected. The selected session can still read its retained revision. Detach it and verify that the same session-scoped read is denied.

Six authenticated `chat.send` runs exercise next-turn behavior. A deterministic local CLI backend receives the actual system prompt from OpenClaw and reads the skill and support file at the projected path. It sees the initial revision after attach and after updating the library. It sees the updated revision after refresh and after a Gateway restart even though the library was rolled back. The unselected control and the detached target receive no personal library path.

The release gate checks the operation sequence, six distinct completed turn IDs, revision and content hashes, the two session identities and clean shutdown. This proves instruction delivery and file access in the CLI backend. The [import journey](skills-library-import.md) covers the independent publication paths. Model adherence and team permissions need separate evidence.
