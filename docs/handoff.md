# Hand-offs between agents

The user works with two agents, Claude and Codex, on the same projects. Without
the board they carry every hand-off by hand: they notice that one agent has
finished, copy its closing message into the other agent's chat and write
"continue". The board now does the carrying. The user still decides every time.

## Principles

- **The user holds the throttle and the brake.** A prompt reaches an agent only
  when the user presses Send in the browser. Agents have no tool that wakes another
  agent, and nothing runs on a timer. Faster modes, if they come, will be board
  settings that the user controls, never decisions agents make.
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
   Install the hook before starting the thread, or restart the Codex app once
   afterwards.

   The Codex Windows app runs no hooks, even approved ones, so on such a
   machine the courier (see 6) records Codex turns from each pinned thread's
   rollout file in `$CODEX_HOME/sessions/`. Every `task_complete` event becomes
   the record the hook would have written, with the model taken from the
   turn's `turn_context`, and goes through the same spool and `--forward`
   command. A thread seen for the first time starts at the end of its file.
2. **Turns panel.** When no discussion is open, the reading pane shows the
   latest turn per project, or a project's recent turns.
3. **Main sessions.** Each project pins one Claude session and one Codex thread.
   Other sessions in the same folder, such as read-only advisors or scheduled
   automations, still log turns, but they are greyed and are never woken.
4. **Next turn.** The composer is prefilled with a Continue prompt that quotes
   the latest closing message, and the user edits it to steer. The first prompt
   to a session explains where it comes from. Once a session has had a board
   prompt delivered, later ones say only "Claude replied on <time>, closing
   with:" and the quote, and the messenger's relay note shrinks to one line. The board records
   each send and later marks it Answered when that session's next turn appears.
5. **Delivery.**
   - Codex: `codex queue --thread <thread> --message <text>`. The Codex desktop
     app watches its queue and runs the turn. The command's warning about a
     missing daemon is harmless.
   - Claude: the prompt waits in the board. The Board Messenger, a Claude
     session on Haiku at low effort with its working directory in
     `messenger/`, keeps `correspondence-dispatch wait` running in the
     background. That command holds each prompt until the target session is
     idle, so a board prompt always starts the next turn, as Codex's queue does;
     steering inside a running turn belongs in the agent's own app. It then
     claims each waiting prompt once, looks up the
     target session's current name in Claude Code's session records, prints
     the prompt under a one-line note ("relayed from the user … answer here in your
     own conversation; do not SendMessage the messenger") and exits, which wakes
     the messenger. Without the note, a recipient answers a relayed question by
     replying to the messenger, where the user never sees it. The messenger relays the
     text unchanged with `SendMessage`, records the outcome with
     `correspondence-dispatch done`, and starts listening again. Idle listening
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
     `codex queue` and records Codex turns as above. On `wsl` it must write to
     the Codex app's queue database (`CODEX_SQLITE_HOME=~/.codex/sqlite`).
     A session appears on the board only once it has a recorded turn, so a
     new one is introduced with `--announce <project>=<id>`, which sends the
     latest turn of that Codex thread or Claude session once and makes it
     available to pin. Codex compresses older rollout files to `.jsonl.zst`;
     the courier reads those too, and after a file is replaced it sends only
     turns completed after the last one it saw.
   - **Messenger.** The machine runs its own Board Messenger with the same
     `wait` and `done` commands; `CORRESPONDENCE_BOARD`, set to the SSH command
     for `serve` in the messenger's `.claude/settings.local.json`, points them
     at the remote board. A board connection that fails is retried every
     minute without waking the messenger.

## Stage 3

Built in four steps, each usable on its own and checked by the user before the
next. 3a and 3b are live.

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
- **3c. Budget display.** The hook and the courier record each turn's token
  use: Claude's from the transcript's per-message `usage`, Codex's from the
  rollout's `token_count`. Codex rollouts also carry the weekly limit's
  `used_percent` and the credit balance; Claude's plan limits need a probe.
  The board shows tokens per turn, per project per day, and each agent's
  latest limit reading.
- **3d. Hold and Cruise.** Each project gets a mode: Manual (the default),
  Hold or Cruise. Hold is the brake: it cancels unclaimed prompts and blocks
  sends until released. Cruise is the throttle: when a turn lands that
  declares `Next: Claude` or `Next: Codex`, the board sends the short Continue
  prompt to that agent itself. It stops after a hand-off count the user sets,
  on `Next: User` or a missing declaration, on any failed delivery, and at a
  token ceiling from 3c. This rewords the repository's "no automatic agent
  dispatch" rule: the user still starts every cruise, and agents still cannot
  wake each other.
