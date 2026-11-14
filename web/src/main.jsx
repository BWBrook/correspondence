import React, {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";
import {
  Plus,
  Search,
  RefreshCw,
  UserRound,
  Monitor,
  FolderOpen,
  Archive,
  ArchiveRestore,
  Activity,
} from "lucide-react";
import {
  api,
  post,
  authorLabel,
  getUserName,
  setUserName,
  shortTime,
  plainExcerpt,
  stamp,
} from "./api";
import { CreateForm, UnreadCounts } from "./components";
import { ProjectArchiveDialog } from "./ProjectArchiveDialog";
import { ProjectRenameDialog } from "./ProjectRenameDialog";
import { ThreadReader } from "./ThreadReader";
import { SearchDialog } from "./SearchDialog";
import { TurnsPanel } from "./TurnsPanel";
import "./style.css";

function routeFromHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  return {
    archived: params.get("archive") === "1",
    projectId: params.get("project"),
    threadId: Number(params.get("thread")) || null,
    messageId: Number(params.get("message")) || null,
    mode: ["latest", "unread"].includes(params.get("view"))
      ? params.get("view")
      : "oldest",
  };
}
function orderProjects(projects, order) {
  return [...projects].sort((a, b) => {
    if (order === "activity") {
      const activity = (b.last_activity_at || "").localeCompare(
        a.last_activity_at || "",
      );
      if (activity) return activity;
    }
    return (
      a.name.localeCompare(b.name, undefined, { sensitivity: "base" }) ||
      a.id.localeCompare(b.id)
    );
  });
}
function projectStatusTitle(project) {
  const details = (project.status?.pairs || []).map((pair) => {
    const states = [];
    for (const request of pair.decisions) states.push(`${request.agent}: ${request.summary}`);
    if (pair.running) states.push("Auto active");
    if (pair.pending) states.push("awaiting a reply");
    if (pair.new_activity) states.push("new activity");
    return `${pair.name}: ${states.join("; ") || "quiet"}`;
  });
  return [project.status?.label || "Quiet", ...details].join("\n");
}

function ProjectDescription({ text }) {
  const id = useId();
  const ref = useRef(null);
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  useLayoutEffect(() => {
    const element = ref.current;
    const measure = () => {
      const lineHeight = parseFloat(getComputedStyle(element).lineHeight);
      setOverflows(element.scrollHeight > lineHeight * 3 + 1);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [text]);
  return (
    <div className="project-description">
      <p
        id={id}
        ref={ref}
        className={expanded ? "expanded" : ""}
        tabIndex={expanded ? 0 : undefined}
      >
        {text}
      </p>
      {overflows && (
        <button
          className="text-button"
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => {
            ref.current.scrollTop = 0;
            setExpanded((value) => !value);
          }}
        >
          {expanded ? "Show less" : "Show more…"}
        </button>
      )}
    </div>
  );
}
function Board({ route, navigate }) {
  const archived = route.archived;
  const [projectOrder, setProjectOrder] = useState(() => {
    try {
      return localStorage.getItem("correspondence:project-order") === "activity"
        ? "activity"
        : "alphabetical";
    } catch {
      return "alphabetical";
    }
  });
  const [projects, setProjects] = useState([]),
    [projectId, setProjectId] = useState(route.projectId);
  const [threads, setThreads] = useState([]);
  const threadId = route.threadId;
  const [showTurns, setShowTurns] = useState(Boolean(route.projectId) && !archived);
  const [liveOnly, setLiveOnly] = useState(() => {
    try { return localStorage.getItem("correspondence:live-only") === "true"; }
    catch { return false; }
  });
  const [sectionVisit, setSectionVisit] = useState(0);
  const seenEntry = useRef(null);
  const [searchScope, setSearchScope] = useState(null);
  const [modal, setModal] = useState(null),
    [error, setError] = useState("");
  const [renameDialog, setRenameDialog] = useState(null);
  const [archiveDialog, setArchiveDialog] = useState(null),
    [restoring, setRestoring] = useState(false),
    [projectNotice, setProjectNotice] = useState(null);
  const restoringRef = useRef(false);
  const currentProject = useRef(projectId),
    listRequest = useRef(0),
    projectRequest = useRef(0);
  const projectsRef = useRef(projects),
    navigateRef = useRef(navigate),
    mounted = useRef(true);
  projectsRef.current = projects;
  navigateRef.current = navigate;
  currentProject.current = projectId;
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      projectRequest.current += 1;
      listRequest.current += 1;
    };
  }, []);
  const project = projects.find((p) => p.id === projectId);
  const projectNames = Object.fromEntries(projects.map((p) => [p.id, p.name]));
  const listedThread = threads.find((thread) => thread.id === threadId);
  const readerThread = listedThread;
  const refreshProjects = useCallback(async () => {
    const sequence = ++projectRequest.current;
    const list = await api(`/projects?archived=${archived}`);
    if (sequence !== projectRequest.current) return list;
    // Another client may archive/restore the open project. Drop its cached
    // reader and search context as soon as its removal is observed.
    const selected = currentProject.current;
    const previous = projectsRef.current.find((p) => p.id === selected);
    const fresh = list.find((p) => p.id === selected);
    if (previous && previous.state_token !== fresh?.state_token) {
      listRequest.current += 1;
      setThreads([]);
      setSearchScope(null);
      setModal(null);
      setArchiveDialog(null);
      navigateRef.current(null);
    }
    setProjects(list);
    setProjectId((previous) =>
      list.some((p) => p.id === previous) ? previous : null,
    );
    return list;
  }, [archived]);
  const refreshList = useCallback(async () => {
    const selected = currentProject.current,
      request = ++listRequest.current;
    const scope = selected
      ? `project_id=${encodeURIComponent(selected)}`
      : "limit=60";
    const list = await api(
      `/threads?${scope}&archived=${archived}`,
    );
    if (
      request === listRequest.current &&
      currentProject.current === selected
    )
      setThreads(list);
  }, [archived]);
  const refreshCounts = useCallback(async () => {
    await Promise.all([refreshProjects(), refreshList()]);
  }, [refreshProjects, refreshList]);
  useEffect(() => {
    if (archived || !projectId || !project) {
      if (!projectId) seenEntry.current = null;
      return;
    }
    const entry = `${projectId}:${project.state_token}:${sectionVisit}`;
    if (seenEntry.current === entry) return;
    seenEntry.current = entry;
    const selected = projectId;
    post(`/projects/${encodeURIComponent(selected)}/seen`, {})
      .then(() => {
        if (!mounted.current) return;
        refreshProjects().catch((e) => setError(e.message));
        if (currentProject.current === selected)
          refreshList().catch((e) => setError(e.message));
      })
      .catch((e) => mounted.current && setError(e.message));
  }, [archived, projectId, project?.state_token, sectionVisit, refreshProjects, refreshList]);
  useEffect(() => {
    refreshProjects().catch((e) => setError(e.message));
  }, [refreshProjects]);
  useEffect(() => {
    refreshList().catch((e) => setError(e.message));
  }, [projectId, project?.state_token, refreshList]);
  useEffect(() => {
    const poll = setInterval(() => {
      if (document.visibilityState === "visible") {
        refreshProjects().catch(() => {});
        refreshList().catch(() => {});
      }
    }, 20000);
    return () => clearInterval(poll);
  }, [refreshProjects, refreshList]);
  useEffect(() => {
    if (!route.threadId) {
      setProjectId(route.projectId);
      setShowTurns(Boolean(route.projectId) && !archived);
    }
  }, [route.projectId, route.threadId, archived]);
  function selectThread(id) {
    setShowTurns(false);
    navigate(id);
  }
  function selectProject(id) {
    setShowTurns(Boolean(id) && !archived);
    setSectionVisit((value) => value + 1);
    if (currentProject.current !== id) {
      listRequest.current += 1;
      setThreads([]);
    }
    currentProject.current = id;
    setProjectId(id);
    setError("");
    navigate(null, { projectId: id });
  }
  async function projectMoved(result, original, restored = false) {
    if (!mounted.current) return;
    setArchiveDialog(null);
    setProjectNotice({
      text: restored
        ? `Restored as ${result.name} (${result.id}).`
        : `${original.name} is archived. Its original ID is available for a new project.`,
      restored: restored ? result : null,
    });
    setProjects((previous) => previous.filter((p) => p.id !== original.id));
    if (currentProject.current === original.id) selectProject(null);
    await refreshProjects();
  }
  async function restoreProject() {
    if (!project || restoringRef.current) return;
    const original = project;
    restoringRef.current = true;
    setRestoring(true);
    setError("");
    try {
      const result = await post(
        `/projects/${encodeURIComponent(original.id)}/restore`,
        { expected_state: original.state_token },
      );
      await projectMoved(result, original, true);
    } catch (e) {
      setError(e.message);
    } finally {
      restoringRef.current = false;
      setRestoring(false);
    }
  }
  function created(result) {
    if (!mounted.current) return;
    const type = modal;
    setModal(null);
    if (type === "project") {
      refreshProjects().catch((e) => setError(e.message));
      selectProject(result.id);
    } else {
      refreshList().catch((e) => setError(e.message));
      selectThread(result.id);
    }
  }
  const visibleProjects = orderProjects(projects, projectOrder).filter(
    (p) => archived || !liveOnly || p.live || p.id === projectId,
  );
  const controlsView = !archived && projectId && showTurns && !threadId;
  const emptyReader = (
    <main className="reader">
      <div className="empty-reader">
        <h2>
          {archived
            ? "The conversation is preserved."
            : "Room for another perspective."}
        </h2>
        <p>
          {archived
            ? "Select an archived project to read or search its discussions. Archived content stays outside the active board."
            : "Choose a discussion, or start one. The reasoning stays here for the next time you return."}
        </p>
        {!archived && !projects.length && (
          <button onClick={() => setModal("project")}>
            <Plus size={17} /> Create your first project
          </button>
        )}
      </div>
    </main>
  );
  return (
    <div className={`app ${threadId || (showTurns && !archived) ? "reading" : ""}${controlsView ? " controls-view" : ""}`}>
      <aside className="sidebar">
        <a className="brand" href="/">
          Correspondence
        </a>
        <div className="board-views" role="group" aria-label="Board view">
          <button
            className={!archived ? "active" : ""}
            aria-pressed={!archived}
            onClick={() => archived && navigate(null, { archived: false })}
          >
            <FolderOpen size={16} /> Board
          </button>
          <button
            className={archived ? "active" : ""}
            aria-pressed={archived}
            onClick={() => !archived && navigate(null, { archived: true })}
          >
            <Archive size={16} /> Archive
          </button>
        </div>
        <label className="project-order">
          Project order
          <select
            value={projectOrder}
            onChange={(event) => {
              const order = event.target.value;
              setProjectOrder(order);
              try {
                localStorage.setItem("correspondence:project-order", order);
              } catch {
                /* best effort */
              }
            }}
          >
            <option value="alphabetical">Alphabetical</option>
            <option value="activity">Latest activity</option>
          </select>
        </label>
        {!archived && (
          <div className="project-filter">
            <label title="Show new activity, active Auto runs, pending turns and outstanding decisions. The selected section stays visible.">
              <input type="checkbox" checked={liveOnly} onChange={(event) => {
                const checked = event.target.checked;
                setLiveOnly(checked);
                try { localStorage.setItem("correspondence:live-only", String(checked)); }
                catch { /* Filtering works without browser storage. */ }
              }} />
              Live only
            </label>
            <details className="status-legend">
              <summary>Status colours</summary>
              <span><i className="project-status decision" /> Needs your decision</span>
              <span><i className="project-status running" /> Auto active</span>
              <span><i className="project-status activity" /> New activity, paused</span>
            </details>
          </div>
        )}
        <nav aria-label={archived ? "Archived projects" : "Projects"}>
          <h2>{archived ? "Archived projects" : "Projects"}</h2>
          <button
            className="text-button board-search"
            onClick={() => setSearchScope("all")}
          >
            <Search size={17} /> {archived ? "Search archive" : "Search board"}
          </button>
          <button
            className={`project activity-view ${projectId ? "" : "selected"}`}
            aria-current={projectId ? undefined : "page"}
            onClick={() => selectProject(null)}
          >
            <span className="project-label">
              <Activity size={15} />{" "}
              {archived ? "All archived discussions" : "Recent activity"}
            </span>
          </button>
          {visibleProjects.map((p) => (
            <button
              key={p.id}
              className={`project ${p.id === projectId ? "selected" : ""}`}
              aria-current={p.id === projectId ? "page" : undefined}
              title={archived
                ? (p.last_activity_at ? `Last activity ${stamp(p.last_activity_at)}` : "No discussions yet")
                : projectStatusTitle(p)}
              onClick={() => selectProject(p.id)}
            >
              <span className="project-label">
                {p.name}
                {archived && (
                  <small className="archive-date">
                    Archived {new Date(p.archived_at).toLocaleDateString()}
                  </small>
                )}
              </span>
              {!archived && p.status?.kind !== "quiet" && p.status?.kind && (
                <span className={`project-status ${p.status.kind}`} role="img"
                  aria-label={p.status.label} />
              )}
            </button>
          ))}
          {!archived && liveOnly && !visibleProjects.length && (
            <p className="quiet-projects">No live projects. Clear the filter to browse all projects.</p>
          )}
          {!archived && (
            <button
              className="text-button new-project"
              onClick={() => setModal("project")}
            >
              <Plus size={17} /> New project
            </button>
          )}
        </nav>
        <div className="sidebar-footer">
          <span>
            <UserRound size={18} /> {getUserName()}
          </span>
          <span>
            <Monitor size={18} /> Local board
          </span>
        </div>
      </aside>
      <section className="discussion-list" aria-label="Discussions">
        <div className="list-header">
          <h1>
            {projectId
              ? project?.name
              : archived
                ? "Archived discussions"
                : "Recent activity"}
          </h1>
          {archived && (
            <p className="archive-hint">
              Preserved discussions. Restore a project to continue writing.
            </p>
          )}
          {project ? (
            project.description && (
              <ProjectDescription key={projectId} text={project.description} />
            )
          ) : (
            !archived && (
              <p className="activity-hint">
                The latest discussions across every project.
              </p>
            )
          )}
          <button
            className="outline search-launch"
            onClick={() => setSearchScope(project ? "project" : "all")}
          >
            <Search size={18} />{" "}
            {project
              ? "Search this project"
              : archived
                ? "Search archive"
                : "Search board"}
          </button>
          {!archived && (
            <button className="outline section-turns" onClick={() => setShowTurns(true)}>
              Agent turns &amp; hand-offs
            </button>
          )}
          <div className="tabs">
            <span>Discussions</span>
            <button
              className="icon refresh-list"
              aria-label="Refresh discussions"
              onClick={() => {
                refreshList().catch((e) => setError(e.message));
                refreshProjects().catch((e) => setError(e.message));
              }}
            >
              <RefreshCw size={16} />
            </button>
          </div>
          {!archived && project && (
            <button
              className="outline new-discussion"
              onClick={() => setModal("thread")}
            >
              <Plus size={17} /> New discussion
            </button>
          )}
          {!archived && project && (
            <button
              className="text-button project-archive-action"
              onClick={() => setRenameDialog(project)}
            >
              Rename project…
            </button>
          )}
          {project && (
            <button
              className="text-button project-archive-action"
              disabled={restoring}
              onClick={
                archived ? restoreProject : () => setArchiveDialog(project)
              }
            >
              {archived ? <ArchiveRestore size={16} /> : <Archive size={16} />}
              {archived
                ? restoring
                  ? "Restoring…"
                  : "Restore project"
                : "Archive project…"}
            </button>
          )}
        </div>
        {projectNotice && (
          <div className="project-notice" role="status">
            <p>{projectNotice.text}</p>
            {projectNotice.restored && (
              <button
                className="text-button"
                onClick={() =>
                  navigate(null, {
                    archived: false,
                    projectId: projectNotice.restored.id,
                  })
                }
              >
                Open restored project
              </button>
            )}
          </div>
        )}
        {error && (
          <p className="error-banner" role="alert">
            {error}
          </p>
        )}
        <div className="thread-list">
          {threads.length ? (
            threads.map((t) => (
              <button
                className={`thread-row ${t.id === threadId ? "selected" : ""}`}
                key={t.id}
                onClick={() => selectThread(t.id)}
              >
                {!projectId && (
                  <span className="thread-project">
                    {projectNames[t.project_id] || t.project_id}
                  </span>
                )}
                <h3>
                  {t.title}
                </h3>
                <p className="thread-meta">
                  {authorLabel(t.last_author) || "Discussion"} ·{" "}
                  <time dateTime={t.updated_at} title={stamp(t.updated_at)}>
                    {shortTime(t.updated_at)}
                  </time>{" "}
                  · {t.message_count}{" "}
                  {t.message_count === 1 ? "message" : "messages"}
                  {t.status === "resolved" ? " · Resolved" : ""}
                </p>
                {t.last_excerpt && (
                  <p className="thread-excerpt">
                    {plainExcerpt(t.last_excerpt)}
                  </p>
                )}
                {!archived && <UnreadCounts counts={t.unread_counts} />}
              </button>
            ))
          ) : (
            <div className="empty-list">
              {archived
                ? "No archived discussions here."
                : project
                  ? "Start a discussion to exchange ideas, questions, and evidence."
                  : "No discussions yet. Create a project to begin."}
            </div>
          )}
        </div>
        {project?.repo_path && (
          <div className="repo-path" title={project.repo_path}>
            <FolderOpen size={15} />
            <span>{project.repo_path}</span>
          </div>
        )}
      </section>
      {threadId ? (
        <ThreadReader
          key={threadId}
          route={route}
          listed={readerThread}
          onNavigate={(options) => navigate(threadId, options)}
          onBack={() => navigate(null, { projectId })}
          onRefresh={refreshCounts}
          onSearch={() => setSearchScope("thread")}
          onProject={(id) => {
            currentProject.current = id;
            setProjectId(id);
          }}
        />
      ) : !archived ? (
        <TurnsPanel
          projectId={projectId}
          projectNames={projectNames}
          onOpenProject={(id) => { selectProject(id); setShowTurns(true); }}
          onBack={() => setShowTurns(false)}
          onStatusChanged={() => refreshProjects().catch((e) => setError(e.message))}
          fallback={emptyReader}
        />
      ) : (
        emptyReader
      )}
      {searchScope && (
        <SearchDialog
          initialScope={searchScope}
          archived={archived}
          project={project}
          threadId={threadId}
          close={() => setSearchScope(null)}
          onSelect={(message) => {
            setSearchScope(null);
            currentProject.current = message.project_id;
            setProjectId(message.project_id);
            setShowTurns(false);
            navigate(message.thread_id, { messageId: message.id });
          }}
        />
      )}
      {modal && (
        <CreateForm
          type={modal}
          projectId={projectId}
          close={() => setModal(null)}
          onCreated={created}
        />
      )}
      {renameDialog && (
        <ProjectRenameDialog
          project={renameDialog}
          close={() => setRenameDialog(null)}
          onRenamed={async () => {
            setRenameDialog(null);
            await refreshProjects();
          }}
        />
      )}
      {archiveDialog && (
        <ProjectArchiveDialog
          project={archiveDialog}
          close={() => setArchiveDialog(null)}
          onArchived={(result) => projectMoved(result, archiveDialog)}
        />
      )}
    </div>
  );
}
function App() {
  const [route, setRoute] = useState(routeFromHash);
  useEffect(() => {
    const handler = () => setRoute(routeFromHash());
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);
  function navigate(
    id,
    {
      messageId = null,
      mode = "oldest",
      archived = route.archived,
      projectId = null,
    } = {},
  ) {
    const params = new URLSearchParams();
    if (archived) params.set("archive", "1");
    if (id) {
      params.set("thread", id);
      if (messageId) params.set("message", messageId);
      else if (mode !== "oldest") params.set("view", mode);
    } else if (projectId) params.set("project", projectId);
    const hash = params.size ? `#${params}` : "";
    if (location.hash !== hash)
      history.pushState(null, "", `${location.pathname}${hash}`);
    setRoute({ threadId: id, messageId, mode, archived, projectId });
  }
  // A view change drops cached results, in-flight readers and open search forms.
  return (
    <Board key={String(route.archived)} route={route} navigate={navigate} />
  );
}
api("/health")
  .then((health) => setUserName(health.user_name))
  .catch(() => {})
  .finally(() =>
    createRoot(document.getElementById("root")).render(<App />),
  );
