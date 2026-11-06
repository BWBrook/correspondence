import React, { useEffect, useState } from "react";
import { api, getUserName, post, shortTime, stamp } from "./api";

const AGENTS = ["Claude", "Codex"];

export function continuePrompt(turn, projectName, primed) {
  if (!turn) return "Continue: take your next turn on this project.";
  const quoted = (turn.message || "(no closing message recorded)")
    .split("\n")
    .map((line) => `> ${line}`)
    .join("\n");
  if (primed)
    return `${turn.agent} replied on ${stamp(turn.recorded_at)}, closing with:

${quoted}`;
  return `Continue — from ${getUserName()}, sent through the board.

${turn.agent}'s latest turn in ${projectName} ended at ${stamp(turn.recorded_at)}. Its closing message, relayed word for word:

${quoted}

Take your next turn.`;
}

function dispatchState(d, live) {
  const carrier = d.target === "Codex" ? "courier" : "messenger";
  const where = d.machine ? `the ${d.machine} ${carrier}` : `the ${carrier}`;
  if (d.status === "failed") return `Failed: ${d.detail || "no detail"}`;
  if (d.answered_at) return `Answered ${shortTime(d.answered_at)}`;
  if (d.status === "delivered") return "Delivered, waiting for a reply";
  if (d.claimed_at) return `Handed to ${where}`;
  if (d.target === "Claude" && live && live.status !== "idle")
    return `Waiting for ${live.name} to finish its turn`;
  return `Waiting for ${where}`;
}

function machineName(machine) {
  return machine || "the Mac mini";
}

function MainSessions({ projectId, agents, onChange }) {
  const [error, setError] = useState("");
  async function pin(agent, session_id) {
    setError("");
    try {
      onChange(
        await post(`/projects/${encodeURIComponent(projectId)}/agents`, {
          agent,
          session_id: session_id || null,
        }),
      );
    } catch (e) {
      setError(e.message);
    }
  }
  return (
    <details className="main-sessions">
      <summary>Main sessions</summary>
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
                <option key={s.session_id} value={s.session_id}>
                  {s.session_id.slice(0, 8)} · {s.turns}{" "}
                  {s.turns === 1 ? "turn" : "turns"} · last{" "}
                  {shortTime(s.last_at)}
                  {s.automation ? " · automation" : ""}
                  {s.machine ? ` · ${s.machine}` : ""} ·{" "}
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

export function NextTurn({ projectId, projectName, lastTurn, onPinsChanged }) {
  const [agents, setAgents] = useState({
    pins: {},
    machines: {},
    couriers: {},
    sessions: [],
  });
  const [dispatches, setDispatches] = useState([]);
  const [text, setText] = useState("");
  const [edited, setEdited] = useState(false);
  const [sending, setSending] = useState(null);
  const [error, setError] = useState("");
  const suggested = lastTurn
    ? AGENTS.find((agent) => agent !== lastTurn.agent)
    : null;
  const defaultText = continuePrompt(
    lastTurn,
    projectName,
    Boolean(suggested && agents.primed?.[suggested]),
  );
  useEffect(() => {
    if (!edited) setText(defaultText);
  }, [defaultText, edited]);
  useEffect(() => {
    let live = true;
    const load = () => {
      api(`/projects/${encodeURIComponent(projectId)}/agents`)
        .then((a) => live && setAgents(a))
        .catch(() => {});
      api(`/dispatches?project_id=${encodeURIComponent(projectId)}&limit=5`)
        .then((d) => live && setDispatches(d))
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
  }, [projectId]);
  const pending = dispatches.some((d) => d.status === "queued");
  useEffect(() => {
    if (!pending) return undefined;
    const poll = setInterval(() => {
      api(`/dispatches?project_id=${encodeURIComponent(projectId)}&limit=5`)
        .then(setDispatches)
        .catch(() => {});
    }, 3000);
    return () => clearInterval(poll);
  }, [pending, projectId]);
  async function cancel(id) {
    setError("");
    try {
      const result = await post(`/dispatches/${id}/cancel`, {});
      setDispatches((list) => list.map((d) => (d.id === id ? result : d)));
    } catch (e) {
      setError(e.message);
    }
  }
  async function send(target) {
    setSending(target);
    setError("");
    try {
      const result = await post("/dispatches", {
        project_id: projectId,
        target,
        body: text,
      });
      setDispatches((list) => [result, ...list].slice(0, 5));
      if (result.status === "failed")
        setError(`Not delivered: ${result.detail}`);
      else setEdited(false);
    } catch (e) {
      setError(e.message);
    } finally {
      setSending(null);
    }
  }
  function blocked(agent) {
    if (!agents.pins[agent])
      return `Choose the main ${agent} session under Main sessions first.`;
    const machine = agents.machines[agent];
    if (agent === "Claude" && machine && !agents.couriers[machine])
      return `${machine} is offline or its courier is not running.`;
    if (agent === "Claude" && !agents.claude_live)
      return `The main Claude session is not running on ${machineName(machine)}.`;
    return null;
  }
  return (
    <section className="next-turn" aria-label="Next turn">
      <header>
        <h3>Next turn</h3>
        <span className="hint">
          {lastTurn
            ? `Last: ${lastTurn.agent}, ${shortTime(lastTurn.recorded_at)}`
            : "No turns yet"}
        </span>
      </header>
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
              : "session not running"}{" "}
            ·{" "}
            {agents.messenger_listening
              ? "messenger listening"
              : "messenger not listening, so prompts wait"}
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
              </span>
              <span>
                {dispatchState(d, agents.claude_live)}
                {d.status === "queued" && !d.claimed_at && (
                  <button className="text-button" onClick={() => cancel(d.id)}>
                    Cancel
                  </button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      <MainSessions
        projectId={projectId}
        agents={agents}
        onChange={(next) => {
          setAgents(next);
          onPinsChanged();
        }}
      />
    </section>
  );
}
