import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, formatUsage, getUserName, shortTime, stamp, tokens } from "./api";
import { BoardMarkdown } from "./components";
import { NextTurn } from "./NextTurn";

function ago(value) {
  const minutes = Math.floor((Date.now() - new Date(value)) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 24 * 60) return `${Math.floor(minutes / 60)} h ago`;
  return shortTime(value);
}

function ClampedBody({ text }) {
  const ref = useRef(null);
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  useLayoutEffect(() => {
    const element = ref.current;
    const measure = () =>
      setOverflows(element.scrollHeight > element.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [text]);
  return (
    <>
      <div ref={ref} className={`turn-body${expanded ? "" : " clamped"}${overflows && !expanded ? " faded" : ""}`}>
        <BoardMarkdown>{text || "_No closing message recorded._"}</BoardMarkdown>
      </div>
      {(overflows || expanded) && (
        <button
          className="text-button"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Show less" : "Show more…"}
        </button>
      )}
    </>
  );
}

function LimitReading({ agent, reading }) {
  if (!reading) return null;
  const parts = reading.windows.map((w) =>
    w.resets_at && Date.parse(w.resets_at) <= Date.now()
      ? `${w.label} reset since`
      : `${w.label} ${Math.round(w.percent)}%`,
  );
  if (reading.credits != null)
    parts.push(`${Math.round(reading.credits).toLocaleString()} credits`);
  const resets = reading.windows
    .filter((w) => w.resets_at)
    .map((w) => `${w.label} resets ${stamp(w.resets_at)}`)
    .join("; ");
  return (
    <span className="limit-reading" title={resets || undefined}>
      <strong>{agent}</strong> {parts.join(" · ")}{" "}
      <span className="read-at">(read {shortTime(reading.read_at)})</span>
    </span>
  );
}

function UsageStrip({ projectId, reload }) {
  const [usage, setUsage] = useState(null);
  useEffect(() => {
    let live = true;
    const path = projectId ? `/usage?project_id=${encodeURIComponent(projectId)}` : "/usage";
    const load = () => api(path).then((u) => live && setUsage(u)).catch(() => {});
    load();
    const poll = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 60000);
    return () => {
      live = false;
      clearInterval(poll);
    };
  }, [projectId, reload]);
  if (!usage) return null;
  const today = usage.days.length ? usage.days[0].day : null;
  const totals = {};
  for (const row of usage.days.filter((r) => r.day === today)) {
    const t = (totals[row.agent] ||= { turns: 0, input: 0, cached: 0, output: 0 });
    for (const key of ["turns", "input", "cached", "output"]) t[key] += row[key];
  }
  const { Claude, Codex } = usage.limits;
  if (!Claude && !Codex && !today) return null;
  return (
    <div className="usage-strip">
      {(Claude || Codex) && (
        <p>
          <span className="usage-label">Plan limits</span>
          <span className="usage-items">
            <LimitReading agent="Claude" reading={Claude} />
            <LimitReading agent="Codex" reading={Codex} />
          </span>
        </p>
      )}
      {today && (
        <p>
          <span className="usage-label">
            {today === new Date().toLocaleDateString("en-CA") ? "Today" : today}
            {projectId ? " in this project" : ""}
          </span>
          <span className="usage-items">
            {Object.entries(totals).map(([agent, t]) => (
              <span key={agent}>
                <strong>{agent}</strong> {t.turns} {t.turns === 1 ? "turn" : "turns"} · {formatUsage(t)}
              </span>
            ))}
          </span>
        </p>
      )}
    </div>
  );
}

export function TurnsPanel({ projectId, projectNames, onOpenProject, fallback }) {
  const [turns, setTurns] = useState(null);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let live = true;
    const path = projectId
      ? `/turns?project_id=${encodeURIComponent(projectId)}&limit=10`
      : "/turns";
    const load = () =>
      api(path)
        .then((list) => {
          if (live) {
            setTurns(list);
            setError("");
          }
        })
        .catch((e) => live && setError(e.message));
    load();
    const poll = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 20000);
    return () => {
      live = false;
      clearInterval(poll);
    };
  }, [projectId, reload]);
  useEffect(() => setTurns(null), [projectId]);
  if (turns && !turns.length && !error) return fallback;
  const lastTurn = (turns || []).find(
    (turn) => turn.role !== "other" && !turn.automation,
  );
  return (
    <main className="reader turns-panel" aria-label="Agent turns">
      <header className="turns-header">
        <h2>{projectId ? "Recent agent turns" : "Latest agent turns"}</h2>
        <p>
          {projectId
            ? "Each agent’s closing message, newest first, recorded when its turn ended."
            : "The most recent turn in each project, newest first. Closing messages are shown as the agents wrote them."}
        </p>
      </header>
      <UsageStrip projectId={projectId} reload={reload} />
      {error && (
        <p className="error-banner" role="alert">
          {error}
        </p>
      )}
      {projectId && turns && (
        <NextTurn
          projectId={projectId}
          lastTurn={lastTurn}
          onPinsChanged={() => setReload((n) => n + 1)}
        />
      )}
      {(turns || []).map((turn, index) => (
        <article
          className={`message turn${turn.role === "other" || turn.automation ? " other-session" : ""}`}
          key={`${turn.recorded_at}-${turn.agent}-${index}`}
        >
          <div className={`avatar ${turn.agent.toLowerCase()}`}>
            {turn.agent[0]}
          </div>
          <div className="message-content">
            <header className="message-heading">
              <strong>{turn.agent}</strong>
              {turn.model && <span className="model">{turn.model}</span>}
              {turn.usage && (
                <span
                  className="model usage"
                  title={`${turn.usage.input.toLocaleString()} new input, ${turn.usage.cached.toLocaleString()} cached input, ${turn.usage.output.toLocaleString()} output tokens`}
                >
                  {tokens(turn.usage.output)} out
                </span>
              )}
              {turn.next && (
                <span
                  className={`session-tag next-tag${turn.next.agent === "User" ? " your-call-tag" : ""}`}
                  title={turn.next.reason || undefined}
                >
                  Next: {turn.next.agent === "User" ? getUserName() : turn.next.agent}
                </span>
              )}
              {(turn.role === "other" || turn.automation) && (
                <span className="session-tag">
                  {turn.automation ? "automation" : "other session"}
                </span>
              )}
              {!projectId && (
                <button
                  className="text-button turn-project"
                  onClick={() => onOpenProject(turn.project)}
                >
                  {projectNames[turn.project] || turn.project}
                </button>
              )}
              <time
                className="message-number"
                dateTime={turn.recorded_at}
                title={stamp(turn.recorded_at)}
              >
                {ago(turn.recorded_at)}
              </time>
            </header>
            <ClampedBody text={turn.message} />
            {turn.discussions?.length > 0 && (
              <p className="turn-discussions">
                Posted in{" "}
                {turn.discussions.map((d, i) => (
                  <React.Fragment key={d.thread_id}>
                    {i > 0 && " · "}
                    <a href={`#thread=${d.thread_id}&message=${d.message_ids[0]}`}>
                      {d.title}
                    </a>
                    {d.message_ids.length > 1 && ` (${d.message_ids.length} messages)`}
                  </React.Fragment>
                ))}
              </p>
            )}
          </div>
        </article>
      ))}
    </main>
  );
}
