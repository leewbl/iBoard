# Import a skill into a personal library

`skills-library-import` starts an isolated Gateway and calls `openclaw skills library import` through the authenticated CLI. It exercises a directory with a `.zip` suffix, a compressed ZIP containing two files, and an explicitly versioned ClawHub release served by a deterministic loopback fixture. The registry trace records metadata, trust, download and verification requests from the actual Gateway.

The CLI reads each private revision back with its exact instruction and support-file bytes. A session created before import stays unselected. A new session selects all three private revisions and can read each one by its exact identity. An invalid ZIP, a duplicate destination and a registry verdict that blocks the release all fail with nonzero exit codes. The original three revisions remain intact, and the blocked registry release is never downloaded.

The release gate checks the owner, content and revision hashes, both session identities, the registry request sequence and clean shutdown. Runs use a local fixture for the registry protocol. Public-registry availability and GitHub-backed registry resolutions have separate verification requirements. The [team journey](skills-library-team.md) covers sharing and ownership.
