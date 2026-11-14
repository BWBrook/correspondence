import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, post, formatUsage, getUserName, shortTime, stamp, tokens } from "./api";
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

function storedPair(projectId) {
  try { return Number(localStorage.getItem(`correspondence.pair.${projectId}`)) || null; }
  catch { return null; }
}

function rememberPair(projectId, pairId) {
  try { localStorage.setItem(`correspondence.pair.${projectId}`, String(pairId)); }
  catch { /* Selection still works without browser storage. */ }
}

function PairSettings({ projectId, pair, onSaved }) {
  const [name, setName] = useState(pair.name);
  const [discussionIds, setDiscussionIds] = useState(pair.discussion_ids);
  const [threads, setThreads] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    api(`/threads?project_id=${encodeURIComponent(projectId)}&limit=1000`)
      .then((list) => mounted.current && setThreads(list))
      .catch((e) => mounted.current && setError(e.message));
    return () => { mounted.current = false; };
  }, [projectId]);
  async function save(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const next = await post(`/pairs/${pair.id}`, { name, discussion_ids: discussionIds });
      if (mounted.current) onSaved(next);
    } catch (e) {
      if (mounted.current) setError(e.message);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <details className="pair-settings">
      <summary onClick={(event) => {
        if (!event.currentTarget.parentElement.open) {
          setName(pair.name);
          setDiscussionIds(pair.discussion_ids);
        }
      }}>Pair settings</summary>
      <form onSubmit={save}>
        <label className="pair-field">
          Pair name
          <input value={name} maxLength={100} required
            onChange={(event) => setName(event.target.value)} />
        </label>
        <fieldset className="pair-discussions">
          <legend>Linked discussions</legend>
          <p className="hint">Choose the discussions for this exchange. Messages stay in this section.</p>
          {threads.length === 0 && <p className="hint">No discussions in this section yet.</p>}
          {threads.map((thread) => (
            <label key={thread.id}>
              <input type="checkbox" checked={discussionIds.includes(thread.id)}
                onChange={(event) => setDiscussionIds((ids) => event.target.checked
                  ? [...ids, thread.id] : ids.filter((id) => id !== thread.id))} />
              {thread.title}
            </label>
          ))}
        </fieldset>
        <button className="outline" type="submit" disabled={busy || !name.trim()}>
          {busy ? "Saving…" : "Save pair settings"}
        </button>
        {error && <p className="error" role="alert">{error}</p>}
      </form>
    </details>
  );
}

function PairControls({ projectId, pairs, pair, onSelect, onSaved }) {
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  async function create(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const next = await post(`/projects/${encodeURIComponent(projectId)}/pairs`, { name });
      if (!mounted.current) return;
      onSaved(next);
      setAdding(false);
      setName("");
    } catch (e) {
      if (mounted.current) setError(e.message);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <section className="pair-controls" aria-label="Exchange pairs">
      <div className="pair-toolbar">
        {pairs.length > 1 ? (
          <label>
            Pair
            <select aria-label="Exchange pair" value={pair?.id || ""} disabled={busy}
              onChange={(event) => onSelect(Number(event.target.value))}>
              {pairs.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          </label>
        ) : <strong>{pair?.name || "Exchange pairs"}</strong>}
        <button className="text-button" onClick={() => { setAdding(!adding); setError(""); }} disabled={busy}>
          {adding ? "Cancel new pair" : "Add pair"}
        </button>
      </div>
      {pairs.length > 1 && <p className="hint">Each pair has its own sessions, prompts and Auto-continue run. Plan limits are shared.</p>}
      {adding && (
        <form className="new-pair" onSubmit={create} aria-label="New pair">
          <label className="pair-field">
            New pair name
            <input value={name} required maxLength={100} autoFocus
              onChange={(event) => setName(event.target.value)} />
          </label>
          <button className="outline" disabled={busy || !name.trim()} type="submit">
            {busy ? "Creating…" : "Create pair"}
          </button>
          {error && <p className="error" role="alert">{error}</p>}
        </form>
      )}
      {pair && <PairSettings key={pair.id} projectId={projectId} pair={pair} onSaved={onSaved} />}
    </section>
  );
}

export function TurnsPanel({ projectId, projectNames, onOpenProject, fallback }) {
  const [pairState, setPairState] = useState({ projectId: null, items: null, selectedId: null });
  const [turnState, setTurnState] = useState({ scope: null, items: null });
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const pairs = pairState.projectId === projectId ? pairState.items : null;
  const pair = pairs?.find((item) => item.id === pairState.selectedId) || null;
  const pairId = pair?.id || null;
  const scope = `${projectId || "all"}:${pairId || "all"}`;
  const turns = turnState.scope === scope ? turnState.items : null;
  useEffect(() => {
    if (!projectId) return;
    let live = true;
    const load = () => api(`/projects/${encodeURIComponent(projectId)}/pairs`)
      .then((items) => {
        if (!live) return;
        setPairState((old) => {
          const selected = old.projectId === projectId ? old.selectedId : storedPair(projectId);
          const selectedId = items.some((item) => item.id === selected) ? selected : items[0]?.id || null;
          return { projectId, items, selectedId };
        });
        setError("");
      })
      .catch((e) => live && setError(e.message));
    load();
    const poll = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 20000);
    return () => { live = false; clearInterval(poll); };
  }, [projectId, reload]);
  useEffect(() => {
    if (projectId && !pairId) return;
    let live = true;
    const path = projectId
      ? `/turns?project_id=${encodeURIComponent(projectId)}&pair_id=${pairId}&limit=10`
      : "/turns";
    const load = () => api(path)
      .then((items) => {
        if (live) { setTurnState({ scope, items }); setError(""); }
      })
      .catch((e) => live && setError(e.message));
    load();
    const poll = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 20000);
    return () => { live = false; clearInterval(poll); };
  }, [projectId, pairId, scope, reload]);
  function selectPair(id) {
    rememberPair(projectId, id);
    setError("");
    setPairState((old) => ({ ...old, selectedId: id }));
  }
  function savePair(next) {
    rememberPair(projectId, next.id);
    setPairState((old) => ({
      projectId,
      items: old.items.some((item) => item.id === next.id)
        ? old.items.map((item) => item.id === next.id ? next : item)
        : [...old.items, next],
      selectedId: next.id,
    }));
    setReload((n) => n + 1);
  }
  if (!projectId && turns && !turns.length && !error) return fallback;
  const lastTurn = (turns || []).find((turn) => turn.role === "main" && !turn.automation);
  return (
    <main className="reader turns-panel" aria-label="Agent turns">
      <header className="turns-header">
        <h2>{projectId ? "Recent agent turns" : "Latest agent turns"}</h2>
        <p>
          {projectId
            ? "Each agent’s closing message for the selected pair, newest first."
            : "The most recent turn in each pair, newest first. Closing messages are shown as the agents wrote them."}
        </p>
      </header>
      <UsageStrip projectId={projectId} reload={reload} />
      {error && (
        <p className="error-banner" role="alert">
          {error}
        </p>
      )}
      {projectId && pairs && (
        <PairControls key={projectId} projectId={projectId} pairs={pairs} pair={pair}
          onSelect={selectPair} onSaved={savePair} />
      )}
      {projectId && !pairs && !error && <p className="hint" role="status">Loading pairs…</p>}
      {projectId && pair && turns && (
        <NextTurn key={`${projectId}:${pair.id}`} pairId={pair.id} pairName={pair.name}
          lastTurn={lastTurn} onPinsChanged={() => setReload((n) => n + 1)} />
      )}
      {projectId && pair && turns?.length === 0 && <p className="hint">No turns from this pair yet. Choose its sessions under Pair sessions.</p>}
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
              {turn.pair_name && <span className="session-tag pair-tag">{turn.pair_name}</span>}
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
                  onClick={() => {
                    if (turn.pair_id != null) rememberPair(turn.project, turn.pair_id);
                    onOpenProject(turn.project);
                  }}
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
