# Hand-offs between agents

Barry works with two agents, Claude and Codex, on the same projects. Before
this feature he carried every hand-off by hand: he noticed that one agent had
finished, copied its closing message into the other agent's chat and wrote
"continue". The board now does the carrying. Barry still decides every time.

## Principles

- **Barry holds the throttle and the brake.** A prompt reaches an agent only
  when Barry presses Send in the browser. Agents have no tool that wakes another
  agent, and nothing runs on a timer. Faster modes, if they come, will be board
  settings that Barry controls, never decisions agents make.
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
2. **Turns panel.** When no discussion is open, the reading pane shows the
   latest turn per project, or a project's recent turns.
3. **Main sessions.** Each project pins one Claude session and one Codex thread.
   Other sessions in the same folder, such as read-only advisors or scheduled
   automations, still log turns, but they are greyed and are never woken.
4. **Next turn.** The composer is prefilled with a Continue prompt that quotes
   the latest closing message, and Barry edits it to steer. The board records
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
     the prompt under a one-line note ("relayed from Barry … answer here in your
     own conversation; do not SendMessage the messenger") and exits, which wakes
     the messenger. Without the note, a recipient answers a relayed question by
     replying to the messenger, where Barry never sees it. The messenger relays the
     text unchanged with `SendMessage`, records the outcome with
     `correspondence-dispatch done`, and starts listening again. Idle listening
     costs no tokens. The messenger's settings deny file, web and board tools.
     A heartbeat file lets the board show whether it is listening.

## Not yet

Hold and Cruise modes, a budget display, and agents declaring who acts next
(which would let turns link to discussions).
