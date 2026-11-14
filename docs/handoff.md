# Hand-offs between agents

The user works with two agents, Claude and Codex, on the same projects. Without
the board they carry every hand-off by hand: they notice that one agent has
finished, copy its closing message into the other agent's chat and write
"continue". The board now does the carrying. The user still decides every time.

## Principles

- **The user holds the throttle and the brake.** A prompt reaches an agent when
  the user presses Send in the browser, or within an auto-continue run the user
  started and bounded, which Stop ends at once. Agents have no tool that wakes
  another agent: they only say who should go next.
- **Show, don't summarise.** The board shows each agent's closing message as
  written. It adds no model calls of its own, and so no third opinion.
- **Plain substrates.** Turns are lines in a JSONL file and dispatches are rows
  in the board's SQLite database. Vendor features (Stop hooks, `codex queue`,
  Claude's cross-session messages) are adapters and can be replaced.

## Pieces

1. **Turn capture.** `correspondence-turn-hook` runs as a `Stop` hook in each
   agent's project settings. It appends the closing message, agent, session and
   time to `turns.jsonl` in the board's data directory. It never blocks a turn.
   On another machine (the `wsl` track), the hook runs with `--machine` and
   `--forward "ssh <board host> …/correspondence-turn-hook --append"`. It
   spools each line locally and sends the spool over SSH, clearing it only on
   success, so turns recorded while the board's host is unreachable arrive
   with the next one. The board ignores exact duplicate lines.

   A Codex thread reads its project's `.codex/hooks.json` when the app loads
   the thread, so a thread already open when the file is added never runs the
   hook, even after approval. Seen on 7 November 2026 (Codex app with CLI
   0.166): a new session in the same folder ran it, the open thread did not.
   Install and approve the hook before starting the thread, or restart the
   Codex app once afterwards.

   The Codex Windows app runs no hooks, even approved ones, so on such a
   machine the courier (see 6) records Codex turns from each pinned thread's
   rollout file in `$CODEX_HOME/sessions/`. Every `task_complete` event becomes
   the record the hook would have written, with the model taken from the
   turn's `turn_context`, and goes through the same spool and `--forward`
   command. A thread seen for the first time starts at the end of its file.
2. **Turns panel.** When no discussion is open, the reading pane shows the
   latest turn per pair, or a selected pair's recent turns.
3. **Named pairs.** Each section contains named pairs. Each pair pins one Claude
   session and one Codex thread; a session can belong to only one pair in that
   section. Select a pair in the turns pane, then choose its **Pair sessions**.
   Other sessions in the same folder, such as read-only advisors or scheduled
   automations, still log turns, but they are greyed and are never woken.
   The Claude app sometimes reopens a conversation under a new session id
   (seen 11 November 2026 with EPRP Workspace), which leaves the pin on a
   session that no longer runs. When the pinned Claude session is not running
   and a newer Claude session is recording turns in the project, the Next turn
   box offers to make that one main.
4. **Next turn.** The composer is prefilled with a Continue prompt that quotes
   the latest closing message, and the user edits it to steer. The first prompt
   to a session explains where it comes from. Once a session has had a board
   prompt delivered, later ones say only "Claude replied on <time>, closing
   with:" and the quote, and the messenger's relay note shrinks to one line. The board records
   each send and later marks it Answered when that session's next turn appears.
   Each agent has at most one prompt on its way at a time. The user can cancel
   a prompt until a messenger or courier picks it up. A picked-up prompt is
   never handed out again, because it may have been delivered with only its
   report lost; if no outcome arrives within two minutes, the user can give up
   on it, which marks it failed and frees the agent for the next prompt.
   Changing a session or its machine stops only that pair's run and withdraws
   its unclaimed auto prompts, plus prompts waiting for its old session.
   Claimed prompts are preserved because they may already have been delivered,
   and archiving a project withdraws all of its waiting prompts.
5. **Delivery.**
   - Codex: `codex queue --thread <thread> --message <text>`. The Codex desktop
     app watches its queue and runs the turn. The command's warning about a
     missing daemon is harmless.
   - Claude: the prompt waits in the board. The Board Messenger, a Claude
     session on Haiku at low effort with its working directory in
     `messenger/`, keeps `correspondence-dispatch wait` running in the
     background. That command holds each prompt until the target session is
     running and idle, so a board prompt always starts the next turn, as Codex's
     queue does; steering inside a running turn belongs in the agent's own app.
     A prompt to a session that is not running, for example after the app
     restarted, waits until the user opens it again. It then
     claims each waiting prompt once, looks up the
     target session's current name in Claude Code's session records, prints
     the prompt under a one-line note ("relayed from the user … answer here in your
     own conversation; do not SendMessage the messenger") and exits, which wakes
     the messenger. Without the note, a recipient answers a relayed question by
     replying to the messenger, where the user never sees it. The messenger relays the
     text unchanged with `SendMessage`, records the outcome with
     `correspondence-dispatch done`, and starts listening again. `SendMessage`
     addresses sessions by name and refuses a name that two agents share, as
     when a Remote Control entry from another machine has the same title. The
     messenger then resends to the one local session of that name by its
     `[ref]`, and fails the send if there is not exactly one. Idle listening
     costs no tokens. The messenger's settings deny file, web and board tools.
     A heartbeat file lets the board show whether it is listening.

6. **Other machines.** A pin records the machine its session's turns came
   from (none for the board's own host). Prompts to a session on another
   machine, Codex ones included, wait in the board until that machine collects
   them. The board's host cannot reach such a machine (the `wsl` laptop only
   connects out), so the machine reaches the board instead, through
   `correspondence-dispatch serve --machine <name>` run over SSH. That command
   answers one JSON request per line, sees only that machine's pins and
   prompts, and exits after ten idle minutes.
   - **Courier.** `correspondence-courier --machine <name> --board "<ssh … serve>"
     --forward "<ssh … --append>"` runs as a user service on that machine. Every
     few seconds it reads the machine's pins and reports whether each pinned
     Claude session is running and idle, which also tells the board the
     courier is online. It queues waiting Codex prompts with the local
     `codex queue` and records Codex turns as above. If the board connection
     drops after a delivery, the courier reports the outcome on its next round. On `wsl` it must write to
     the Codex app's queue database (`CODEX_SQLITE_HOME=~/.codex/sqlite`).
     A session appears on the board only once it has a recorded turn, so a
     new one is introduced with `--announce <project>=<id>`, which sends the
     latest turn of that Codex thread or Claude session once and makes it
     available to pin. The board's own host runs no courier; there, build the
     record with the courier's `scan` and `turn_record`, drop `machine`, and
     pipe it to `correspondence-turn-hook --append`. dSDM needed this on
     12 November 2026: its Codex thread's only turn came before the hook was
     approved, so there was nothing to pin and no way to send a first prompt.
     Codex compresses older rollout files to `.jsonl.zst`;
     the courier reads those too, and after a file is replaced it sends only
     turns completed after the last one it saw.
   - **Messenger.** The machine runs its own Board Messenger with the same
     `wait` and `done` commands; `CORRESPONDENCE_BOARD`, set to the SSH command
     for `serve` in the messenger's `.claude/settings.local.json`, points them
     at the remote board. A board connection that fails is retried every
     minute without waking the messenger.

## Stage 3

Built in four steps, each usable on its own and checked by the user before the
next. All four are live.

- **3a. Who acts next.** An agent ends its closing message with one line,
  `Next: Claude`, `Next: Codex` or `Next: User` (the user's configured name
  also works), optionally followed by a reason: `Next: Codex — review the
  diff`. The board parses that line when it reads the ledger, so the hooks and
  couriers do not change; markdown emphasis and backticks are ignored. Each
  turn shows a Next tag. The Next turn box highlights the declared agent and
  shows "asks for Codex"; `Next: User` highlights neither Send button and
  shows "Your call" with the reason. With no declaration, the board suggests
  the other agent. The convention lives in the shared agent policy, so it
  survives compaction and needs no wrapper text.
- **3b. Turns link to discussions.** Each turn lists the discussions in its
  project that the same agent posted to since that session's previous turn,
  compared to the second, with a link to the first such message. A session's
  first recorded turn has no window and so no links. Posts carry no session,
  so an advisor session's posts in the same window are attributed to the
  main session's turn.
- **3c. Budget display.** The turn hook and the courier record each turn's
  token use as new input, cached input and output: Claude's from the
  transcript's per-message `usage` since the turn's prompt, Codex's as the
  change in the rollout's cumulative `token_count` across the turn. Subagents
  and delegated jobs with their own transcripts are not counted. Codex turns
  also carry the latest limit reading from the rollout (the weekly window's
  percentage and the credit balance). Claude's plan limits are visible only
  inside the Claude app, so after each relay the Board Messenger calls the
  app's read-only usage tool and passes the report to
  `correspondence-dispatch limits`, which keeps the latest reading on the board.
  Each turn shows its output tokens (hover for the rest); the turns view shows
  the latest plan limits for both agents and today's tokens per agent, for one
  project or all of them.
- **3d. Auto-continue.** The Next turn box offers "Auto-continue for N
  hand-offs (1–50), stop at X% of any plan limit (10–100)", and Stop while a
  run is going. Every few seconds the board checks each running pair: when
  the latest turn from a main session declares `Next: Claude` or `Next: Codex`,
  no prompt has been sent since that turn arrived and none in the pair is
  still on its way, the board sends that turn's Continue prompt to the named
  agent, as Send would, and marks it `auto` in the send list. If the latest
  turn already names an agent when the run starts, the first hand-off goes at
  once. A run stops, showing why, when the hand-offs are used up; when a turn
  declares `Next: User`, declares nothing, or names its own agent; when a
  prompt sent during the run fails or is cancelled; when any current
  plan-limit reading reaches the ceiling (a window that has reset since it was
  read no longer counts); when a main session is unpinned; or when the user
  presses Stop. Stopping withdraws the run's prompts not yet picked up, and a
  run started afterwards tries that turn again. One run goes at a time in each pair. Turns
  from advisor sessions never drive a run.

  Each prompt records which turn was the pair's latest when it was sent, and
  a run tests that record, not timestamps, so machines whose clocks disagree
  cannot make it send twice. A run spends a hand-off in the same database
  transaction that records the prompt, and only while it is still going, so a
  Stop cannot be overtaken by a send decided a moment before. A first version
  (7 November 2026) had three modes, Manual, Hold and Cruise. Hold only
  cancelled waiting prompts and blocked Send, which Cancel and Stop already
  cover, so the switch became one control.

## Human attention and section visits

The human sidebar summarises named pairs: an outstanding decision request takes
priority over a running Auto exchange, which takes priority over unseen activity.
The Live only filter also retains a pair with a prompt awaiting delivery or reply.
Sections open on the controls and latest closing messages; discussions remain
available without requiring the human to keep up with every message.

A pinned agent requests a decision by ending its closing message with
`Next: User — decision needed: <question>` (the configured human name also works).
An ordinary hand-off to the human is not a decision request. The request remains
pending until that requesting session's next real closing message no longer asks
for it, or the human marks that exact request handled. A later message from the
other agent cannot clear it. A pending request stops only that pair's Auto run;
marking it handled does not send a prompt or restart the run.

Opening an active section acknowledges a snapshot of its discussion messages,
real agent turns and run-stop events for the human. Polling does not acknowledge
later arrivals. Agent read markers remain explicit and independent. Status reads
do not mutate state. Seen snapshots and handled request keys use the existing
settings table; the append-only messages and turn ledger are unchanged.

## Several pairs in one section

Use **Add pair** to create an exchange, name it under **Pair settings**, link
its discussions, and choose its two sessions under **Pair sessions**. A pair
can work in a separate Git worktree or the same checkout as another pair.
Its session IDs, not its working directory or branch, control routing. The
selector changes the prompt, recent turns, delivery history and Auto-continue
controls together; an unsent draft is cleared when changing pairs.

Each run has its own hand-off allowance, pending prompts and Stop. A failure,
repin or Stop in one pair leaves other pairs alone. Plan-limit readings are
account-wide: a ceiling can stop several runs using that account. Sends check
the selected source and target pins again inside the write transaction; a
repin after a prompt was prepared rejects the stale send without spending a
hand-off. Pin changes stop the affected run, which can then be started anew.

Link a pair's discussions explicitly. Turn-to-discussion links are filtered
to those IDs; in a multi-pair section an unconfigured pair gets no inferred
links. Links still correlate posts by agent and time window, not a posting
session: a deliberately shared discussion remains shared.

The same Stop-hook command can be used in each checkout with the section's
`--project` ID. It already records the session ID; no pair ID or branch
heuristic is required in the hook. Gitignored local hook files are not
copied into a newly created worktree, so install the local Codex and Claude
hook settings there before opening worktree-rooted chats. A chat opened in
the main checkout and using `cd` to work in a worktree keeps its existing
capture setup. Do not duplicate a user-level hook in project settings.

The schema upgrade creates a Main pair for each existing section and preserves
all existing pins, prompts, claims, run state and correspondence. It does not
start any run. Internal browser APIs now use `/api/pairs/{pair_id}/agents`,
`/auto` and `/auto/stop`, and dispatch requests carry `pair_id` plus the
expected pin map. The discussion MCP contract is unchanged.
