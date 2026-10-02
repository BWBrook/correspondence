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
  CheckCheck,
  Archive,
  ArchiveRestore,
} from "lucide-react";
import { api, post } from "./api";
import { CreateForm, UnreadCounts } from "./components";
import { ProjectArchiveDialog } from "./ProjectArchiveDialog";
import { ThreadReader } from "./ThreadReader";
import { SearchDialog } from "./SearchDialog";
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
  const currentOrder = useRef(projectOrder);
  currentOrder.current = projectOrder;
  const [projects, setProjects] = useState([]),
    [projectId, setProjectId] = useState(route.projectId);
  const [threads, setThreads] = useState([]);
  const threadId = route.threadId;
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [searchScope, setSearchScope] = useState(null);
  const [markingAll, setMarkingAll] = useState(false),
    [readAllNotice, setReadAllNotice] = useState(""),
    [readAllError, setReadAllError] = useState(""),
    [readAllThread, setReadAllThread] = useState(null);
  const markingAllRef = useRef(false);
  const [modal, setModal] = useState(null),
    [error, setError] = useState("");
  const [archiveDialog, setArchiveDialog] = useState(null),
    [restoring, setRestoring] = useState(false),
    [projectNotice, setProjectNotice] = useState(null);
  const restoringRef = useRef(false);
  const currentProject = useRef(projectId),
    currentUnread = useRef(unreadOnly),
    listRequest = useRef(0),
    projectRequest = useRef(0);
  const projectsRef = useRef(projects),
    navigateRef = useRef(navigate),
    mounted = useRef(true);
  projectsRef.current = projects;
  navigateRef.current = navigate;
  currentProject.current = projectId;
  currentUnread.current = unreadOnly;
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      projectRequest.current += 1;
      listRequest.current += 1;
    };
  }, []);
  const project = projects.find((p) => p.id === projectId);
  const listedThread = threads.find((thread) => thread.id === threadId);
  // An acknowledged thread disappears from an Unread-filtered list. Retain its
  // fresh metadata so the open reader also reflects the explicit bulk action.
  const readerThread =
    readAllThread?.id === threadId &&
    (!listedThread ||
      readAllThread.last_read_message_id > listedThread.last_read_message_id ||
      readAllThread.last_message_id > listedThread.last_message_id)
      ? readAllThread
      : listedThread;
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
      list.some((p) => p.id === previous)
        ? previous
        : orderProjects(list, currentOrder.current)[0]?.id || null,
    );
    return list;
  }, [archived]);
  const refreshList = useCallback(async () => {
    const selected = currentProject.current,
      unread = currentUnread.current,
      request = ++listRequest.current;
    if (!selected) {
      setThreads([]);
      return;
    }
    const list = await api(
      `/threads?project_id=${encodeURIComponent(selected)}&unread_only=${unread}&archived=${archived}`,
    );
    if (
      request === listRequest.current &&
      currentProject.current === selected &&
      currentUnread.current === unread
    )
      setThreads(list);
  }, [archived]);
  const refreshCounts = useCallback(async () => {
    await Promise.all([refreshProjects(), refreshList()]);
  }, [refreshProjects, refreshList]);
  async function markAllRead() {
    if (archived || markingAllRef.current) return;
    markingAllRef.current = true;
    setMarkingAll(true);
    setReadAllNotice("");
    setReadAllError("");
    try {
      const result = await post("/read-all", {});
      setReadAllNotice(
        `${result.messages_marked} unread ${result.messages_marked === 1 ? "message" : "messages"} acknowledged across ${result.threads_marked} ${result.threads_marked === 1 ? "thread" : "threads"}.`,
      );
      await Promise.all([
        refreshCounts(),
        threadId
          ? api(`/threads/${threadId}?mode=latest&limit=1`).then((result) =>
              setReadAllThread(result.thread),
            )
          : Promise.resolve(),
      ]);
    } catch (error) {
      setReadAllError(error.message);
    } finally {
      markingAllRef.current = false;
      setMarkingAll(false);
    }
  }
  useEffect(() => {
    refreshProjects().catch((e) => setError(e.message));
  }, [refreshProjects]);
  useEffect(() => {
    refreshList().catch((e) => setError(e.message));
  }, [projectId, project?.state_token, unreadOnly, refreshList]);
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
    if (!route.threadId && route.projectId) setProjectId(route.projectId);
  }, [route.projectId, route.threadId]);
  function selectThread(id) {
    navigate(id);
  }
  function selectProject(id) {
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
    setUnreadOnly(false);
    if (type === "project") {
      refreshProjects().catch((e) => setError(e.message));
      selectProject(result.id);
    } else {
      refreshList().catch((e) => setError(e.message));
      selectThread(result.id);
    }
  }
  return (
    <div className={`app ${threadId ? "reading" : ""}`}>
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
          <div className="board-read-actions">
            <button
              className="text-button"
              disabled={markingAll}
              onClick={markAllRead}
              title="Mark every thread on the active board read for Barry, including messages not displayed. Archived projects and later arrivals remain unchanged."
            >
              <CheckCheck size={17} />{" "}
              {markingAll ? "Marking all read…" : "Mark all threads read"}
            </button>
            {readAllNotice && <p role="status">{readAllNotice}</p>}
            {readAllError && (
              <p role="alert" className="error">
                {readAllError}
              </p>
            )}
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
          {orderProjects(projects, projectOrder).map((p) => (
            <button
              key={p.id}
              className={`project ${p.id === projectId ? "selected" : ""}`}
              onClick={() => selectProject(p.id)}
            >
              <span className="project-label">
                {p.name}
                {archived ? (
                  <small className="archive-date">
                    Archived {new Date(p.archived_at).toLocaleDateString()}
                  </small>
                ) : (
                  <UnreadCounts counts={p.unread_counts} />
                )}
              </span>
              {!archived && p.unread_count > 0 && (
                <span
                  className="unread-count"
                  aria-label={`${p.unread_count} unread messages`}
                >
                  {p.unread_count}
                </span>
              )}
            </button>
          ))}
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
            <UserRound size={18} /> Barry
          </span>
          <span>
            <Monitor size={18} /> Local board
          </span>
        </div>
      </aside>
      <section className="discussion-list" aria-label="Discussions">
        <div className="list-header">
          <h1>{project?.name || (archived ? "Archive" : "Your projects")}</h1>
          {archived && (
            <p className="archive-hint">
              Preserved discussions. Restore a project to continue writing.
            </p>
          )}
          <ProjectDescription
            key={projectId}
            text={
              project?.description ||
              "A shared space for Barry, Astra and Fable."
            }
          />
          <button
            className="outline search-launch"
            onClick={() => setSearchScope("project")}
            disabled={!project}
          >
            <Search size={18} /> Search this project
          </button>
          <div className="tabs">
            <button
              className={!unreadOnly ? "active" : ""}
              onClick={() => setUnreadOnly(false)}
            >
              All
            </button>
            <button
              className={unreadOnly ? "active" : ""}
              onClick={() => setUnreadOnly(true)}
            >
              Unread
            </button>
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
          {!archived && (
            <button
              className="outline new-discussion"
              disabled={!projectId}
              onClick={() => setModal("thread")}
            >
              <Plus size={17} /> New discussion
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
                <h3>
                  {!archived && t.unread_count > 0 && (
                    <span className="unread-dot" />
                  )}
                  {t.title}
                </h3>
                <p>
                  {t.last_author || "Discussion"} · {t.message_count}{" "}
                  {t.message_count === 1 ? "message" : "messages"}
                  {t.status === "resolved" ? " · Resolved" : ""}
                </p>
                <UnreadCounts counts={t.unread_counts} />
              </button>
            ))
          ) : (
            <div className="empty-list">
              {unreadOnly
                ? "You’re all caught up."
                : archived
                  ? "No archived discussions here."
                  : "Start a discussion to exchange ideas, questions, and evidence."}
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
          onProject={setProjectId}
          onBack={() => selectProject(projectId)}
          onRefresh={refreshCounts}
          onSearch={() => setSearchScope("thread")}
        />
      ) : (
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
            {!archived && !projectId && (
              <button onClick={() => setModal("project")}>
                <Plus size={17} /> Create your first project
              </button>
            )}
          </div>
        </main>
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
            setProjectId(message.project_id);
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
createRoot(document.getElementById("root")).render(<App />);
