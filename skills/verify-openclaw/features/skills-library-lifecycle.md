# Manage personal library revisions and session selections

The `skills-library-lifecycle` journey starts a disposable single-user Gateway. Every operation goes through the real OpenClaw CLI. The CLI's token connection receives the local Gateway's durable owner profile. Use a fresh state directory, a separate input bundle, and one selected session plus an unselected control session.

1. Create a private skill and read its instruction and support file. Attach the exact revision to the target session.
2. Update only `SKILL.md` and check that the supporting file and original session pin survive. Attempt a stale update and read the current revision again.
3. Refresh the target pin to the new revision and verify that the old selection is rejected. Check that the control session still has no selection and cannot use the target's pin to read the new revision.
4. Disable and enable the skill, then roll back the current revision. Restart the Gateway and check that the owner, sessions and pinned revision still match.
5. Remove the skill and check that it disappears from the list. The owner can read the current revision by ID and see that it is marked removed. Verify that edits and new selections are rejected. The selected session can still read its retained revision. Detach it and verify that the same session-scoped read is denied.

The release gate checks the operation sequence, revision and content hashes, the two session identities and clean shutdown. This fixture covers personal scope and stored next-turn selections. Team permissions, imports and actual execution of a selected skill on the next model turn require separate live cases.
