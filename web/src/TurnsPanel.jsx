import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, shortTime, stamp } from "./api";
import { BoardMarkdown } from "./components";

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

export function TurnsPanel({ projectId, projectNames, onOpenProject, fallback }) {
  const [turns, setTurns] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    setTurns(null);
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
  }, [projectId]);
  if (turns && !turns.length && !error) return fallback;
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
      {error && (
        <p className="error-banner" role="alert">
          {error}
        </p>
      )}
      {(turns || []).map((turn, index) => (
        <article
          className="message turn"
          key={`${turn.recorded_at}-${turn.agent}-${index}`}
        >
          <div className={`avatar ${turn.agent.toLowerCase()}`}>
            {turn.agent[0]}
          </div>
          <div className="message-content">
            <header className="message-heading">
              <strong>{turn.agent}</strong>
              {turn.model && <span className="model">{turn.model}</span>}
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
          </div>
        </article>
      ))}
    </main>
  );
}
