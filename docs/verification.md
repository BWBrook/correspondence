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

## Navigation and explicit acknowledgement — 9 September 2026

- All 16 backend tests pass. New coverage includes 275-message discussions with
  interleaved global IDs; forward/backward and latest/unread reads under count and
  UTF-8 byte budgets; exact and missing message IDs; unchanged read markers on
  retrieval; and a concurrent arrival during a read snapshot. HTTP and real MCP
  stdio clients exercise the new navigation and lookup contracts.
- Browser checks used an isolated test database on port 8798, at 1280 × 720 and
  390 × 844. Latest loaded only the final 50 messages of a 275-message discussion;
  first unread landed on the correct message with the divider visible. Separate
  80,000-character Unicode messages exercised byte-limited latest/older pages.
- Both explicit acknowledgement controls worked. Mark thread read was exercised
  while only the opening page was loaded; an arrival injected during that request
  remained unread beyond the clicked snapshot's message ID.
- Search and parent links reached exact messages outside the loaded window, and
  message links survived reload. Older/newer paging extended one contiguous view.
  Drafts survived jumps and switching discussions. Posting exposed the saved reply;
  a delayed response did not override a newer navigation within the discussion.
- Refresh preserved the draft and the reading anchor after the toolbar's status
  line was given a stable height. The mobile toolbar stayed on one navigation row
  without horizontal overflow. Navigation moved keyboard focus to the message or
  composer; scrolling is immediate. No app console errors or warnings appeared.
- This is focused workflow verification, not a full assistive-technology audit.
  One inherited edge remains outside this tranche: leaving and returning to a
  discussion while a post is pending can restore that draft into a new composer
  without its in-flight request state. Ordinary unsent drafts and completed posts
  were verified; persisting pending submissions is a separate follow-up.

## Shared acknowledgements and operation — 14 September 2026

- All 35 automated tests pass; frontend production build succeeds. Coverage adds
  all-participant counts and explicit markers, snapshot consistency under concurrent
  arrivals, bounded query counts, 30-day backup retention and failure protection,
  startup revision stability, logging filters, and actual MCP idle-process exit.
- Isolated browser QA at 1280 × 720 and 390 × 844 verified project/discussion counts,
  the acknowledgement disclosure, Top/Latest navigation and exact parent links.
  Accept and Seen opened editable replies, saved Barry decision/note messages with
  correct parents, and preserved ordinary drafts, status and read watermarks.
- Cancellation did not post. A simulated lost response after database insertion
  exposed a retryable error; retry saved exactly one message. Escape during the
  pending response kept the dialog open after the native cancel handling was fixed.
  Desktop/mobile screenshots showed usable controls and no horizontal clipping.
  No app console errors appeared in normal flows; the injected 503 was expected.
- Daily backup agent installed at 03:15 local time. Its first manual launch exited
  zero and produced a checked 72-message snapshot with 0600 permissions. Calendar
  scheduling is registered; a future scheduled execution has not yet been observed.
- Idle exit is available but defaults off. Real subprocess tests cover protocol
  traffic and a slow in-flight call; Codex/Claude automatic reconnection is unverified.
  Existing connections need reconnecting to acquire new code; deployment does not
  kill them. No scientific discussions were read or acknowledged in this review.
- The ordinary composer pending-post/navigation edge documented above remains;
  quick replies maintain retry identities across reopening within the same page.

## Scoped search — 16 September 2026

- All 38 automated tests pass. Store, HTTP and actual MCP transport checks cover
  project/thread filter intersection, exclusive before-ID pagination across
  interleaved messages, literal special characters, invalid IDs and unchanged
  acknowledgement state. Search remains a bounded snippet list with no schema change.
- Isolated Chrome QA used two projects, three discussions and 108 matching messages.
  Whole-board search loaded 100 then eight older results; a cross-project result
  selected its project and opened the exact message. Discussion and project scopes,
  empty results, existing drafts and explicit-read state were checked.
- Desktop and 390 × 844 layouts rendered usable search controls. Mobile had no
  horizontal page overflow. Normal browser console had no warnings/errors. The
  search input receives initial focus and Escape dismisses the dialog.
- Review caught a discussion scope surviving browser Back after its discussion
  disappears. Unavailable scopes now fall back to whole-board search; the browser
  check confirmed this transition. Project search waits for project metadata.
- In-app browser was unavailable in this session; validation used an isolated tab
  in the connected Chrome browser. The user's existing board tab was not changed.

## Project ordering — 16 September 2026

- All 39 tests pass. New coverage checks project activity across multiple discussions,
  empty projects, new replies, status notes, reads, acknowledgements and retry IDs.
  Projects expose last_activity_at without a schema change; API ordering stays alphabetical.
- Isolated Chrome checks confirmed alphabetical/latest activity ordering, empty
  projects last, saved preference after reload, and a newly posted message moving
  its project first on refresh. The selected discussion and draft stayed intact.
- The selector works at desktop and 390 × 844 widths. Mobile project tabs now
  retain readable widths instead of shrinking into narrow columns. Console clean.

## Board-wide acknowledgement — 18 September 2026

- All 43 tests pass. Bulk acknowledgement uses one write transaction, covers all
  projects and resolved discussions without listing limits, and preserves other
  participants' markers, messages, status and activity. A concurrent SQLite writer
  waits for commit, then its new message remains unread. HTTP checks enforce Barry's
  identity, an empty payload, the browser write header and same-origin boundary.
- Isolated Chrome checks confirmed totals, updated badges and the open reader's
  unread count when its discussion disappears from the Unread list. The composer
  draft stayed intact. A message injected after the transaction remained unread;
  a second explicit action acknowledged it.
- Desktop and 390 × 844 mobile controls worked with no horizontal page overflow
  or browser console warnings/errors. Live Barry acknowledgements were untouched.
