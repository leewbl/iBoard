# Send a message in the TUI and reopen the session

Run `tui-chat-pty` using the attested OpenClaw image. The driver starts a disposable loopback Gateway and opens `openclaw tui` in a real 120 × 40 pseudoterminal. An offline local backend responds to one fixed prompt. Leave enough time for Gateway readiness and two CLI starts.

Type the message and press Enter through the PTY. Replay the raw terminal bytes with OpenClaw's screen helper and require the rows containing the connection status, the message and its reply to be visible in completed frames. Exit using `/exit` and open the same session in a new terminal. Require both messages to be visible again and the backend to have run exactly once.

The separate Gateway client reads the session ID and exact user and assistant roles and text. The report retains both raw transcripts as base64 with their SHA-256 digests and the matched screen rows. Both TUI processes and the Gateway must exit cleanly before cleanup. The public model service and the `terminal` and `chat` local aliases have separate verification requirements.
