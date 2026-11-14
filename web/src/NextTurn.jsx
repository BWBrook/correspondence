import React, { useEffect, useRef, useState } from "react";
import { api, post, shortTime } from "./api";

const AGENTS = ["Claude", "Codex"];

export function continuePrompt(turn, primed) {
  if (!turn) return "Continue: take your next turn for this pair.";
  return primed ? turn.continue_prompts.short : turn.continue_prompts.full;
}

const STALE_CLAIM_MS = 120000;

function useMounted() {
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  return mounted;
}

function dispatchState(d, live) {
  const carrier = d.target === "Codex" ? "courier" : "messenger";
  const where = d.machine ? `the ${d.machine} ${carrier}` : `the ${carrier}`;
  if (d.status === "failed") return `Failed: ${d.detail || "no detail"}`;
  if (d.answered_at) return `Answered ${shortTime(d.answered_at)}`;
  if (d.status === "delivered") return "Delivered, waiting for a reply";
  if (d.claimed_at) return `Handed to ${where}`;
  if (d.target === "Claude" && !live)
    return "Waiting for the Claude session to start";
  if (d.target === "Claude" && live.status !== "idle")
    return `Waiting for ${live.name} to finish its turn`;
  return `Waiting for ${where}`;
}

function cancellable(d) {
  if (d.status !== "queued") return null;
  if (!d.claimed_at) return "Cancel";
  return Date.now() - Date.parse(d.claimed_at) >= STALE_CLAIM_MS ? "Give up" : null;
}

function autoStatus(auto) {
  if (auto.running)
    return `Auto-continue: ${auto.hops_left} ${auto.hops_left === 1 ? "hand-off" : "hand-offs"} left; stops at ${auto.ceiling}% of any plan limit, or when an agent hands to you.`;
  if (auto.stopped_reason)
    return `Auto-continue stopped ${shortTime(auto.stopped_at)}: ${auto.stopped_reason}.`;
  return "Prompts go when you press Send.";
}

function AutoControl({ pairId, auto, onChange }) {
  const mounted = useMounted();
  const [hops, setHops] = useState(6);
  const [ceiling, setCeiling] = useState(80);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function change(path, body) {
    setError("");
    setBusy(true);
    try {
      const next = await post(`/pairs/${pairId}/${path}`, body);
      if (mounted.current) onChange(next);
    } catch (e) {
      if (mounted.current) setError(e.message);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <div className="auto-control">
      {auto.running ? (
        <button className="outline" disabled={busy} onClick={() => change("auto/stop", {})}>
          Stop
        </button>
      ) : (
        <span className="auto-form">
          <button
            disabled={busy}
            onClick={() => change("auto", { hops, ceiling })}
            title="Send each turn's Continue prompt to the agent it names, without waiting for Send"
          >
            Auto-continue
          </button>
          <label>
            for{" "}
            <input type="number" min={1} max={50} value={hops}
              onChange={(e) => setHops(Number(e.target.value))} />{" "}
            hand-offs,
          </label>
          <label>
            stop at{" "}
            <input type="number" min={10} max={100} value={ceiling}
              onChange={(e) => setCeiling(Number(e.target.value))} />
            % of any plan limit
          </label>
        </span>
      )}
      <span className={`hint auto-status${auto.running ? " running" : ""}`}>
        {autoStatus(auto)}
      </span>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function newerSession(agents, agent) {
  const pinned = agents.sessions.find((s) => s.session_id === agents.pins[agent]);
  return agents.sessions.find(
    (s) =>
      s.agent === agent &&
      !s.automation &&
      s.session_id !== agents.pins[agent] &&
      (s.pair_id == null || s.pair_id === agents.pair_id) &&
      (!pinned || s.last_at > pinned.last_at),
  );
}

function MainSessions({ pairId, agents, onChange }) {
  const mounted = useMounted();
  const [error, setError] = useState("");
  async function pin(agent, session_id) {
    setError("");
    try {
      const next = await post(`/pairs/${pairId}/agents`, {
        agent,
        session_id: session_id || null,
      });
      if (mounted.current) onChange(next);
    } catch (e) {
      if (mounted.current) setError(e.message);
    }
  }
  return (
    <details className="main-sessions">
      <summary>Pair sessions</summary>
      <p className="hint">
        Prompts go only to these sessions. Turns from other sessions, such as
        advisors or automations, are greyed and never woken.
      </p>
      {AGENTS.map((agent) => (
        <label key={agent}>
          {agent}
          <select
            value={agents.pins[agent] || ""}
            onChange={(event) => pin(agent, event.target.value)}
          >
            <option value="">Not set</option>
            {agents.sessions
              .filter((s) => s.agent === agent)
              .map((s) => (
                <option key={s.session_id} value={s.session_id}
                  disabled={s.pair_id != null && s.pair_id !== pairId}>
                  {s.session_id.slice(0, 8)} · {s.turns}{" "}
                  {s.turns === 1 ? "turn" : "turns"} · last{" "}
                  {shortTime(s.last_at)}
                  {s.automation ? " · automation" : ""}
                  {s.machine ? ` · ${s.machine}` : ""}
                  {s.pair_id != null && s.pair_id !== pairId ? ` · used by ${s.pair_name}` : ""} ·{" "}
                  {s.excerpt.replace(/\s+/g, " ").slice(0, 50)}
                </option>
              ))}
          </select>
        </label>
      ))}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </details>
  );
}

export function NextTurn({ pairId, pairName, lastTurn, onPinsChanged }) {
  const mounted = useMounted();
  const [agents, setAgents] = useState({
    pins: {},
    machines: {},
    couriers: {},
    sessions: [],
  });
  const [dispatches, setDispatches] = useState([]);
  const [auto, setAuto] = useState({ running: false });
  const [text, setText] = useState("");
  const [edited, setEdited] = useState(false);
  const [sending, setSending] = useState(null);
  const [error, setError] = useState("");
  const other = lastTurn
    ? AGENTS.find((agent) => agent !== lastTurn.agent)
    : null;
  const declared = lastTurn?.next?.agent;
  const suggested = declared === "User" ? null : declared || other;
  const recipient = suggested || other;
  const defaultText = continuePrompt(
    lastTurn,
    Boolean(recipient && agents.primed?.[recipient]),
  );
  useEffect(() => {
    if (!edited) setText(defaultText);
  }, [defaultText, edited]);
  useEffect(() => {
    let live = true;
    const load = () => {
      api(`/pairs/${pairId}/agents`)
        .then((a) => live && setAgents({ ...a, pair_id: pairId }))
        .catch(() => {});
      api(`/dispatches?pair_id=${pairId}&limit=5`)
        .then((d) => live && setDispatches(d))
        .catch(() => {});
      api(`/pairs/${pairId}/auto`)
        .then((a) => live && setAuto(a))
        .catch(() => {});
    };
    load();
    const poll = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 20000);
    return () => {
      live = false;
      clearInterval(poll);
    };
  }, [pairId]);
  const pending = dispatches.some((d) => d.status === "queued");
  const running = auto.running;
  useEffect(() => {
    if (!pending && !running) return undefined;
    let live = true;
    const poll = setInterval(() => {
      api(`/dispatches?pair_id=${pairId}&limit=5`)
        .then((next) => live && setDispatches(next))
        .catch(() => {});
      if (running)
        api(`/pairs/${pairId}/auto`)
          .then((next) => live && setAuto(next))
          .catch(() => {});
    }, 3000);
    return () => { live = false; clearInterval(poll); };
  }, [pending, running, pairId]);
  async function repin(agent, session_id) {
    setError("");
    try {
      const next = await post(`/pairs/${pairId}/agents`, { agent, session_id });
      if (!mounted.current) return;
      setAgents({ ...next, pair_id: pairId });
      onPinsChanged();
    } catch (e) {
      if (mounted.current) setError(e.message);
    }
  }
  async function cancel(id) {
    setError("");
    try {
      const result = await post(`/dispatches/${id}/cancel`, {});
      if (mounted.current) setDispatches((list) => list.map((d) => (d.id === id ? result : d)));
    } catch (e) {
      if (mounted.current) setError(e.message);
    }
  }
  async function send(target) {
    setSending(target);
    setError("");
    try {
      const result = await post("/dispatches", {
        pair_id: pairId,
        target,
        body: text,
        expected_pins: agents.pins,
      });
      if (!mounted.current) return;
      setDispatches((list) => [result, ...list].slice(0, 5));
      if (result.status === "failed")
        setError(`Not delivered: ${result.detail}`);
      else setEdited(false);
    } catch (e) {
      if (mounted.current) setError(e.message);
    } finally {
      if (mounted.current) setSending(null);
    }
  }
  function blocked(agent) {
    if (!agents.pins[agent])
      return `Choose this pair’s ${agent} session under Pair sessions first.`;
    return null;
  }
  return (
    <section className="next-turn" aria-label="Next turn">
      <header>
        <h3>Next turn <span className="pair-name">· {pairName}</span></h3>
        <span className="hint">
          {lastTurn
            ? `Last: ${lastTurn.agent}, ${shortTime(lastTurn.recorded_at)}`
            : "No turns yet"}
          {declared && declared !== "User" && ` · asks for ${declared}`}
        </span>
      </header>
      <AutoControl
        pairId={pairId}
        auto={auto}
        onChange={(next) => {
          setAuto(next);
          api(`/dispatches?pair_id=${pairId}&limit=5`)
            .then((next) => mounted.current && setDispatches(next))
            .catch(() => {});
        }}
      />
      {declared === "User" && (
        <p className="your-call" role="status">
          Your call: {lastTurn.agent} handed this to you
          {lastTurn.next.reason ? ` (${lastTurn.next.reason})` : ""}.
        </p>
      )}
      <textarea
        value={text}
        rows={8}
        aria-label="Prompt to send"
        onChange={(event) => {
          setText(event.target.value);
          setEdited(true);
        }}
      />
      <div className="next-turn-actions">
        {AGENTS.map((agent) => (
          <button
            key={agent}
            className={agent === suggested && !blocked(agent) ? "" : "outline"}
            disabled={Boolean(sending) || Boolean(blocked(agent)) || !text.trim()}
            title={blocked(agent) || `Send this prompt to ${agent}`}
            onClick={() => send(agent)}
          >
            {sending === agent ? "Sending…" : `Send to ${agent}`}
          </button>
        ))}
        {agents.pins.Claude && (
          <span className="hint">
            Claude{agents.machines.Claude ? ` on ${agents.machines.Claude}` : ""}:{" "}
            {agents.claude_live
              ? `${agents.claude_live.name} · ${agents.claude_live.status || "running"}`
              : "session not running, so prompts wait"}{" "}
            ·{" "}
            {agents.messenger_listening
              ? "messenger listening"
              : "messenger not listening, so prompts wait"}
          </span>
        )}
        {agents.pins.Claude && !agents.claude_live && newerSession(agents, "Claude") && (
          <span className="your-call" role="status">
            A newer Claude session ({newerSession(agents, "Claude").session_id.slice(0, 8)},
            last turn {shortTime(newerSession(agents, "Claude").last_at)}) is recording
            turns here, perhaps the same conversation reopened.{" "}
            <button className="text-button" onClick={() => repin("Claude", newerSession(agents, "Claude").session_id)}>
              Make it main
            </button>
          </span>
        )}
        {agents.pins.Codex && agents.machines.Codex && (
          <span className="hint">
            Codex on {agents.machines.Codex}:{" "}
            {agents.couriers[agents.machines.Codex]
              ? "courier online"
              : "courier offline, so prompts wait"}
          </span>
        )}
        {edited && (
          <button
            className="text-button"
            onClick={() => {
              setEdited(false);
              setText(defaultText);
            }}
          >
            Reset to Continue
          </button>
        )}
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {dispatches.length > 0 && (
        <ul className="dispatches">
          {dispatches.map((d) => (
            <li key={d.id} className={d.status}>
              <span>
                To {d.target} · {shortTime(d.requested_at)}
                {d.origin === "auto" && <span className="auto-tag">auto</span>}
              </span>
              <span>
                {dispatchState(d, agents.claude_live)}
                {cancellable(d) && (
                  <button className="text-button" onClick={() => cancel(d.id)}>
                    {cancellable(d)}
                  </button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      <MainSessions
        pairId={pairId}
        agents={agents}
        onChange={(next) => {
          setAgents({ ...next, pair_id: pairId });
          onPinsChanged();
        }}
      />
    </section>
  );
}
