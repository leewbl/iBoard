---
name: setup-benny
description: Configure and preflight the Benny read-only Slack report and GitHub verification route.
disable-model-invocation: true
---

# Set up Benny report verification

Copy [configuration.example.json](configuration.example.json) to a project-specific file outside the target checkout. Fill in the Slack workspace, source channel, configured triage identity, GitHub target and time limits. Use a separate configuration for another source channel. Store the Slack credential in an owner-only file and supply it to iBoard as a path through `--token-file`. Keep it out of the project, configuration and any model prompt.

Choose a specific, explicitly authorized source message. Use the read actions in [reproduce-and-fix-issues](../reproduce-and-fix-issues/SKILL.md) to confirm the credential's workspace, the root permalink, and the trusted verdict. If a reply triggered the run, keep both that reply's `message_ts` and the root `thread_ts`. Use a known test report when exercising the integration. A start without a verdict returns `awaiting-verdict` or waits within the optional configured budget.

The Slack adapter uses [auth.test](https://docs.slack.dev/reference/methods/auth.test/), [conversations.replies](https://docs.slack.dev/reference/methods/conversations.replies/) and [chat.getPermalink](https://docs.slack.dev/reference/methods/chat.getPermalink/). Ensure the selected Slack token is authorized for the source conversation. The adapter never posts. It does not subscribe to new reports, create Cursor automation routines or manage trackers.

For runtime verification, also provide a source-attested local Docker image, an executable verification manifest, and the two approved probes for the behavior. The existing GitHub artifact must reside in the configured repository. A valid source receipt by itself only qualifies the starting point. The verifier still requires the before/after observations and unchanged source and artifact identities.
