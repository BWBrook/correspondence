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
  try {
    if (pairId == null) localStorage.removeItem(`correspondence.pair.${projectId}`);
    else localStorage.setItem(`correspondence.pair.${projectId}`, String(pairId));
  }
  catch { /* Selection still works without browser storage. */ }
}

function PairSettings({ projectId, pair, onSaved, onArchived }) {
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
    setBusy("save");
    setError("");
    try {
      const next = await post(`/pairs/${pair.id}`, { name, discussion_ids: discussionIds, expected_state: pair.state_token });
      if (mounted.current) onSaved(next);
    } catch (e) {
      if (mounted.current) setError(e.message);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  async function archive() {
    setBusy("archive");
    setError("");
    try {
      const next = await post(`/pairs/${pair.id}/archive`, { expected_state: pair.state_token });
      if (mounted.current) onArchived(next);
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
          {busy === "save" ? "Saving…" : "Save pair settings"}
        </button>
        {error && <p className="error" role="alert">{error}</p>}
      </form>
      <div className="pair-archive-action">
        <p className="hint">Archive this pair when its exchange is finished. History, sessions and linked discussions are preserved. Auto stops and prompts not yet picked up are withdrawn; in-flight work may finish.</p>
        <button className="outline" disabled={Boolean(busy)} onClick={archive}>
          {busy === "archive" ? "Archiving…" : "Archive pair"}
        </button>
      </div>
    </details>
  );
}

function PairControls({ projectId, pairs, pair, archived, counts, onViewChange, onSelect, onSaved, onArchived }) {
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
      <div className="pair-views" role="group" aria-label="Pair view">
        <button className={!archived ? "active" : "outline"} aria-pressed={!archived}
          onClick={() => onViewChange(false)}>Active pairs ({counts.active})</button>
        <button className={archived ? "active" : "outline"} aria-pressed={archived}
          onClick={() => onViewChange(true)}>Archived pairs ({counts.archived})</button>
      </div>
      <div className="pair-toolbar">
        {pairs.length > 1 ? (
          <label>
            {archived ? "Archived pair" : "Pair"}
            <select aria-label={archived ? "Archived pair" : "Exchange pair"} value={pair?.id || ""} disabled={busy}
              onChange={(event) => onSelect(Number(event.target.value))}>
              {pairs.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
          </label>
        ) : <strong>{pair?.name || (archived ? "No archived pairs" : "No active pairs")}</strong>}
        {!archived && <button className="text-button" onClick={() => { setAdding(!adding); setError(""); }} disabled={busy}>
          {adding ? "Cancel new pair" : "Add pair"}
        </button>}
      </div>
      {!archived && pairs.length > 1 && <p className="hint">Each pair has its own sessions, prompts and Auto-continue run. Plan limits are shared.</p>}
      {!archived && !pairs.length && <p className="hint">Add a new pair, or open Archived pairs to restore a previous exchange.</p>}
      {!archived && adding && (
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
      {!archived && pair && <PairSettings key={`${pair.id}:${pair.state_token}`} projectId={projectId} pair={pair} onSaved={onSaved} onArchived={onArchived} />}
    </section>
  );
}

function ArchivedPair({ pair, onRestored }) {
  const [history, setHistory] = useState(null);
  const [error, setError] = useState("");
  const [restoring, setRestoring] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    let live = true;
    const load = () => Promise.all([
      api(`/pairs/${pair.id}/agents?archived=true`),
      api(`/dispatches?pair_id=${pair.id}&archived=true&limit=100`),
    ]).then(([agents, dispatches]) => {
      if (live) { setHistory({ agents, dispatches }); setError(""); }
    }).catch((e) => live && setError(e.message));
    load();
    const poll = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 20000);
    return () => { live = false; mounted.current = false; clearInterval(poll); };
  }, [pair.id]);
  async function restore() {
    setRestoring(true);
    setError("");
    try {
      const next = await post(`/pairs/${pair.id}/restore`, { expected_state: pair.state_token });
      if (mounted.current) onRestored(next);
    } catch (e) {
      if (mounted.current) setError(e.message);
    } finally {
      if (mounted.current) setRestoring(false);
    }
  }
  return (
    <section className="archived-pair" aria-label="Archived pair history">
      <div className="archived-pair-heading">
        <div>
          <strong>Archived · {pair.name}</strong>
          <p className="hint">Archived {stamp(pair.archived_at)}. History is read-only; restore the pair to continue its exchange.</p>
        </div>
        <button className="outline" disabled={restoring} onClick={restore}>
          {restoring ? "Restoring…" : "Restore pair"}
        </button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {!history && !error && <p className="hint" role="status">Loading archived history…</p>}
      {history && <>
        <details className="archived-pair-sessions">
          <summary>Preserved sessions</summary>
          {Object.entries(history.agents.pins).map(([agent, session]) => (
            <p key={agent}><strong>{agent}</strong>: <code>{session}</code>
              {history.agents.machines?.[agent] ? ` · ${history.agents.machines[agent]}` : ""}</p>
          ))}
          {!Object.keys(history.agents.pins).length && <p className="hint">No sessions were pinned.</p>}
        </details>
        <details className="archived-pair-deliveries">
          <summary>Latest 100 deliveries ({history.dispatches.length} shown)</summary>
          {history.dispatches.length ? <ul className="dispatches">
            {history.dispatches.map((dispatch) => <li key={dispatch.id} className={dispatch.status}>
              <span>To {dispatch.target} · {stamp(dispatch.requested_at)}</span>
              <span>{dispatch.status}{dispatch.answered_at ? ` · answered ${shortTime(dispatch.answered_at)}` : ""}</span>
              <details>
                <summary>Prompt and receipt</summary>
                {dispatch.detail && <p>{dispatch.detail}</p>}
                <pre>{dispatch.body}</pre>
              </details>
            </li>)}
          </ul> : <p className="hint">No prompts were sent through the board.</p>}
        </details>
      </>}
    </section>
  );
}

function PairAttention({ pairId, revision, onStatusChanged }) {
  const [requests, setRequests] = useState([]);
  const [error, setError] = useState("");
  const [handling, setHandling] = useState(null);
  const mounted = useRef(true), busy = useRef(false), sequence = useRef(0);
  useEffect(() => {
    mounted.current = true;
    let live = true;
    const load = () => {
      if (busy.current) return;
      const request = ++sequence.current;
      api(`/pairs/${pairId}/attention`)
        .then((result) => {
          if (live && request === sequence.current) { setRequests(result.decisions); setError(""); }
        })
        .catch((e) => live && request === sequence.current && setError(e.message));
    };
    load();
    const poll = setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 20000);
    return () => { live = false; mounted.current = false; clearInterval(poll); };
  }, [pairId, revision]);
  async function handle(key) {
    busy.current = true;
    sequence.current += 1;
    setHandling(key);
    setError("");
    try {
      const result = await post(`/pairs/${pairId}/attention/handle`, { key });
      if (!mounted.current) return;
      setRequests(result.decisions);
      onStatusChanged();
    } catch (e) {
      if (!mounted.current) return;
      setError(e.message);
      api(`/pairs/${pairId}/attention`).then((result) => mounted.current && setRequests(result.decisions)).catch(() => {});
    } finally {
      busy.current = false;
      if (mounted.current) setHandling(null);
    }
  }
  if (!requests.length && !error) return null;
  return (
    <section className="decision-requests" aria-label="Your decisions">
      {requests.map((request) => (
        <div className="decision-request" key={request.key}>
          <div>
            <strong><span className="project-status decision" aria-hidden="true" /> {request.agent} needs your decision</strong>
            <p>{request.summary}</p>
          </div>
          <button className="outline" disabled={Boolean(handling)} onClick={() => handle(request.key)}>
            {handling === request.key ? "Marking…" : "Mark handled"}
          </button>
        </div>
      ))}
      {requests.length > 0 && <p className="hint">Reply through the prompt below, or in the agent’s chat. Opening this section does not dismiss the request.</p>}
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}

export function TurnsPanel({ projectId, projectNames, onOpenProject, onBack, onStatusChanged, fallback }) {
  const [pairState, setPairState] = useState({ projectId: null, active: null, archived: null, selectedId: null, archivedId: null });
  const [pairView, setPairView] = useState({ projectId: null, archived: false });
  const [turnState, setTurnState] = useState({ scope: null, items: null });
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const pairRequest = useRef(0);
  const archived = pairView.projectId === projectId && pairView.archived;
  const pairs = pairState.projectId === projectId ? pairState[archived ? "archived" : "active"] : null;
  const pair = pairs?.find((item) => item.id === (archived ? pairState.archivedId : pairState.selectedId)) || null;
  const pairId = pair?.id || null;
  const scope = `${projectId || "all"}:${archived ? "archived" : "active"}:${pairId || "all"}`;
  const turns = turnState.scope === scope ? turnState.items : null;
  useEffect(() => {
    if (!projectId) return;
    let live = true;
    const load = () => {
      const request = ++pairRequest.current;
      return Promise.all([
        api(`/projects/${encodeURIComponent(projectId)}/pairs`),
        api(`/projects/${encodeURIComponent(projectId)}/pairs?archived=true`),
      ]).then(([active, archivedPairs]) => {
        if (!live || request !== pairRequest.current) return;
        setPairState((old) => {
          const selected = old.projectId === projectId ? old.selectedId : storedPair(projectId);
          const selectedId = active.some((item) => item.id === selected) ? selected : active[0]?.id || null;
          const previousArchive = old.projectId === projectId ? old.archivedId : null;
          const archivedId = archivedPairs.some((item) => item.id === previousArchive) ? previousArchive : archivedPairs[0]?.id || null;
          return { projectId, active, archived: archivedPairs, selectedId, archivedId };
        });
        setError("");
      })
      .catch((e) => live && request === pairRequest.current && setError(e.message));
    };
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
      ? `/turns?project_id=${encodeURIComponent(projectId)}&pair_id=${pairId}&archived=${archived}&limit=10`
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
  }, [projectId, pairId, archived, scope, reload]);
  function selectPair(id) {
    if (!archived) rememberPair(projectId, id);
    setError("");
    setPairState((old) => ({ ...old, [archived ? "archivedId" : "selectedId"]: id }));
  }
  function savePair(next) {
    pairRequest.current += 1;
    rememberPair(projectId, next.id);
    setPairState((old) => ({
      ...old,
      projectId,
      active: old.active.some((item) => item.id === next.id)
        ? old.active.map((item) => item.id === next.id ? next : item)
        : [...old.active, next],
      selectedId: next.id,
    }));
    setReload((n) => n + 1);
  }
  function archivePair(next) {
    pairRequest.current += 1;
    const active = pairState.active.filter((item) => item.id !== next.id);
    const selectedId = active[0]?.id || null;
    rememberPair(projectId, selectedId);
    setPairState((old) => ({
      ...old, active, selectedId, archivedId: next.id,
      archived: [...old.archived.filter((item) => item.id !== next.id), next],
    }));
    setReload((n) => n + 1);
    onStatusChanged();
  }
  function restorePair(next) {
    pairRequest.current += 1;
    rememberPair(projectId, next.id);
    setPairState((old) => ({
      ...old, selectedId: next.id,
      active: [...old.active.filter((item) => item.id !== next.id), next],
      archived: old.archived.filter((item) => item.id !== next.id),
    }));
    setPairView({ projectId, archived: false });
    setReload((n) => n + 1);
    onStatusChanged();
  }
  if (!projectId && turns && !turns.length && !error) return fallback;
  const lastTurn = (turns || []).find((turn) => turn.role === "main" && !turn.automation);
  return (
    <main className="reader turns-panel" aria-label="Agent turns">
      <header className="turns-header">
        <button className="outline section-discussions" onClick={onBack}>Discussions</button>
        <h2>{projectId ? projectNames[projectId] || "Agent turns" : "Latest agent turns"}</h2>
        <p>
          {projectId
            ? archived
              ? "Preserved closing messages for the archived pair."
              : "Each agent’s closing message for the selected pair, newest first."
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
        <PairControls key={`${projectId}:${archived}`} projectId={projectId} pairs={pairs} pair={pair}
          archived={archived} counts={{ active: pairState.active.length, archived: pairState.archived.length }}
          onViewChange={(view) => { setPairView({ projectId, archived: view }); setError(""); }}
          onSelect={selectPair} onSaved={savePair} onArchived={archivePair} />
      )}
      {projectId && !pairs && !error && <p className="hint" role="status">Loading pairs…</p>}
      {projectId && pair && archived && <ArchivedPair key={`${projectId}:${pair.id}:${pair.state_token}`} pair={pair} onRestored={restorePair} />}
      {projectId && pair && !archived && (
        <PairAttention key={`attention:${pair.id}:${pair.state_token}:${lastTurn?.recorded_at || "none"}`} pairId={pair.id}
          revision={lastTurn?.recorded_at} onStatusChanged={onStatusChanged} />
      )}
      {projectId && pair && !archived && turns && (
        <NextTurn key={`${projectId}:${pair.id}:${pair.state_token}`} pairId={pair.id} pairName={pair.name}
          expectedState={pair.state_token} lastTurn={lastTurn} onPinsChanged={(next) => {
            pairRequest.current += 1;
            setPairState((old) => ({
              ...old,
              active: old.active.map((item) => item.id === pair.id ? { ...item, state_token: next.state_token } : item),
            }));
            setReload((n) => n + 1);
            onStatusChanged();
          }} onStateChanged={onStatusChanged} />
      )}
      {projectId && pair && turns?.length === 0 && <p className="hint">{archived ? "No captured closing messages in this pair’s history." : "No turns from this pair yet. Choose its sessions under Pair sessions."}</p>}
      {projectId && pair && archived && turns?.length > 0 && <h3 className="archived-turns-heading">Latest 10 closing messages</h3>}
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
