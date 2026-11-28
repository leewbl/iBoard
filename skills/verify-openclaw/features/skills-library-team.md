# Share and transfer team library skills

`skills-library-team` starts an isolated OpenClaw Gateway and authenticates three temporary accounts through a loopback proxy. The Gateway creates distinct durable profiles and grants administrator scope only to the third account. All library checks call the same Gateway methods that back the CLI commands. An unauthenticated connection is refused by the disposable proxy.

The owner creates a private skill and shares it. Another member can then list and read it, and a new session selects the exact revision. The member cannot change the skill. The owner cannot transfer it without administrator scope.

Unsharing removes the skill from the member's catalog and new sessions. The member's existing session retains the exact selected revision and can still read its original bytes. The administrator can transfer the skill to the team without changing the revision, content or original author. The previous owner loses write access. An administrator cannot make a team-owned skill personal by unsharing it.

The release gate checks identities, permission errors, session selections, content hashes and clean shutdown. This uses an isolated proxy and the real Gateway protocol. External identity providers and browser presentation have separate verification requirements.
