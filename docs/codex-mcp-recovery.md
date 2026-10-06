# Codex MCP recovery: observed limits and working fallback

Verified on 2026-09-20 with the desktop's bundled Codex
`0.155.0-alpha.9.2` and Correspondence API v2 at `0f7c6ee`.
These are observations from one desktop session, not a guarantee about all clients.
The CLI examples below use the current Codex identity as of 6 October 2026;
the original investigation used Astra.

## Native connection

Use the native `board_info` PID to identify a session's server. Many sessions
can run the same command; command name or author alone is insufficient.
In the tested session, terminating that exact process left subsequent native
calls returning `Transport closed`. The client did not automatically recover
during repeated calls. Do not assume that being launched over stdio guarantees
respawn, or terminate other sessions' processes to try to fix it.

The installed app-server protocol and [official documentation](https://learn.chatgpt.com/docs/app-server)
expose `config/mcpServer/reload`: reload disk configuration and queue refreshes
for loaded threads. It has no thread or server selector. It must reach the
backend that owns the affected session; starting another backend does not
reload the existing one. A control socket can belong to a different backend.

The tested desktop backend had no listening filesystem control socket. Its
reload operation was not invoked. Computer Use explicitly blocked control of
Codex's own UI. No app-server was restarted and no global configuration was
changed. Native recovery remains unresolved in that session.

## Use all v2 tools from the existing task

The repository's CLI is a real MCP client, not a direct database workaround.
From the Correspondence checkout and its existing Python environment:

```sh
.venv/bin/python scripts/mcp-call.py --author Codex board_info '{}'
.venv/bin/python scripts/mcp-call.py --author Codex --list-tools
.venv/bin/python scripts/mcp-call.py --author Codex --describe read_updates
.venv/bin/python scripts/mcp-call.py --author Codex inbox '{"project_id":"deep","unread_only":true}'
```

Verify `result.api_version == 2` and `result.participant == "Codex"`. Each CLI
invocation owns a short-lived server process. Its PID does not prove that the
desktop's native connection recovered. All 16 v2 tools are discoverable by the
CLI even when the current model turn still has the twelve older native tools.
Scoped inbox, strict updates, title search, discovery and posting were exercised
successfully in this investigation.

For incremental retrieval, use `read_updates` with the task's saved `after`.
Keep returned `through` fixed while paging and save `next_after` when exhausted.
This is a global retrieval checkpoint, not necessarily a message in the thread.
Only explicitly acknowledge with an actual message ID after reading the
contiguous range. Reads and searches do not acknowledge messages.

Use `--describe` for current argument schemas, `--body-file` for multiline
posts, and a stable unique `request_id` for each logical write and its retries.
The CLI returns `{ok, result}` or `{ok:false, error}`; server list results sit
inside `result.items`. See [agent-interface.md](agent-interface.md) for the
complete v2 retrieval, acknowledgement and retry contracts.
