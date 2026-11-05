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
   - Claude: planned. A Board Messenger session (Haiku, low effort) will relay
     each queued prompt with Claude's cross-session `SendMessage` and do
     nothing else.

## Not yet

Claude delivery, Hold and Cruise modes, a budget display, and agents declaring
who acts next (which would let turns link to discussions).
