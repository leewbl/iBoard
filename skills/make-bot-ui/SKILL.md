---
name: make-bot-ui
description: Create a button panel that wakes a scoped iBoard or configured Cursor webhook routine.
disable-model-invocation: true
---

# Make a bot UI

Choose the exact tasks to inspect or frozen workflows to run. A button sends a named action. The panel server sets the webhook credential, stores failed payloads for later delivery, and handles repeated request IDs in the native receiver. The receiver parses the body of its wake event as untrusted JSON.

1. Run `python3 -m workbench.bot_ui create --manifest /absolute/panel.json --directory /absolute/routine` from the iBoard root. A manifest has `title`, `locale` of `zh` or `en`, and `actions`. Each action has `id`, `label`, `description`, and `operation`. Use `{"kind":"inspect-task","task":"/absolute/task.json"}` or `{"kind":"run-plan","plan":"/absolute/plan.json","sha256":"APPROVED_SHA"}`. The run path uses the existing frozen-plan guard.
2. Run `serve --directory /absolute/routine --port 0 --ready /absolute/listening.json` on the same module. This starts the native receiver and the panel. Use the reported URL. Retrieve the panel access key from the owner-only `operator.key` file and enter it in the panel.
3. Run `doctor --directory /absolute/routine --url http://127.0.0.1:PORT`. The doctor opens the page and sends the ignored probe through the server. Check the subsequent outcome of an actual authorized action in the panel.
4. If the sender could not confirm delivery, use the retained-request action or `drain --directory /absolute/routine --url http://127.0.0.1:PORT`. This wakes a native drain job that sends the saved JSON. The routine keeps the same request ID. Another native receipt for that ID does not rerun the action.

To use an existing Cursor webhook, create the routine in Cursor with the webhook trigger and adapt `webhook-contract.json` to its approved actions. Copy the exact URL from the routine panel. Put the sender key in an owner-only credential file using the host's secret entry, then run `configure --directory /absolute/routine --webhook URL --sender-file /absolute/sender-key` before restarting the panel. The server sends the key in the Bearer and X-Automation-Key headers. A provider HTTP 200 confirms reception. Use the provider’s task record to verify execution. The external routine needs to deduplicate the request ID before acting.

For private access from other devices, connect the existing Tailscale node to the intended network and start `serve` with `--tailnet`. Run `locations --port PORT` to verify that the node IP answers and read both the private IP and DNS URLs. The node owner completes any Tailscale sign-in with the official client.
