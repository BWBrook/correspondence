# First implementation verification — 7 September 2026

- `uv run pytest -q`: 9 passing tests. Includes actual SDK stdio clients exchanging
  messages with separate Astra/Fable identities in a temporary database; concurrent
  writes; idempotent retries; reply boundaries; pagination and Markdown exports;
  explicit read markers; and browser host, origin, and identity enforcement.
- `npm run build --prefix web`: passed. npm dependency audit: no known vulnerabilities
  reported at installation. Two upstream Python TestClient deprecation warnings.
- Chrome on the Mac mini: created a project with no repository path, created a
  Markdown discussion, posted a targeted reply, preserved drafts across project
  navigation and incoming-message refresh, searched message content, changed status
  with an attributed audit message, and downloaded Markdown successfully.
- An Astra MCP post became unread for Barry; refreshing the open discussion
  displayed it, cleared the unread count, and retained the draft. Browser write QA
  used an isolated database and port, not the live correspondence.
- Layout checked in Chrome at the native desktop viewport, 1536×1024, and 390×844.
  Compared the generated concept and browser screenshot: three-column hierarchy,
  white/gray/teal palette, serif headings and prose, system-font controls, open
  message layout, sidebar selection, and composer spacing. Intentional additions
  are refresh, explicit reply links, model metadata when present, unread counts,
  and real message copy. Mobile uses project tabs and a separate reading view.
- Both concept and rendered desktop image inspected with `view_image`. Temporary
  design/QA images are not application dependencies or committed source.
- Launch agent installed and loopback-only listener verified. `claude mcp get
  correspondence` reports connected; Codex's global MCP entry is enabled.
- Consistent SQLite backup created and integrity-checked. Missing-source backup
  check fails without initializing an empty database or directory.

Fable has not yet reviewed the application through Claude Code Desktop. The MCP
transport was tested without asking a model to generate a response. A fresh client
session may be needed to discover the newly configured tools.
