import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Plus,
  Search,
  RefreshCw,
  UserRound,
  Monitor,
  FolderOpen,
  CheckCheck,
} from "lucide-react";
import { api, post } from "./api";
import { CreateForm, UnreadCounts } from "./components";
import { ThreadReader } from "./ThreadReader";
import { SearchDialog } from "./SearchDialog";
import "./style.css";

function routeFromHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  return {
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
function App() {
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
    [projectId, setProjectId] = useState(null);
  const [threads, setThreads] = useState([]),
    [route, setRoute] = useState(routeFromHash);
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
  const currentProject = useRef(projectId),
    currentUnread = useRef(unreadOnly),
    listRequest = useRef(0),
    projectRequest = useRef(0);
  currentProject.current = projectId;
  currentUnread.current = unreadOnly;
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
    const list = await api("/projects");
    if (sequence !== projectRequest.current) return list;
    setProjects(list);
    setProjectId(
      (previous) =>
        previous || orderProjects(list, currentOrder.current)[0]?.id || null,
    );
    return list;
  }, []);
  const refreshList = useCallback(async () => {
    const selected = currentProject.current,
      unread = currentUnread.current,
      request = ++listRequest.current;
    if (!selected) {
      setThreads([]);
      return;
    }
    const list = await api(
      `/threads?project_id=${encodeURIComponent(selected)}&unread_only=${unread}`,
    );
    if (
      request === listRequest.current &&
      currentProject.current === selected &&
      currentUnread.current === unread
    )
      setThreads(list);
  }, []);
  const refreshCounts = useCallback(async () => {
    await Promise.all([refreshProjects(), refreshList()]);
  }, [refreshProjects, refreshList]);
  async function markAllRead() {
    if (markingAllRef.current) return;
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
  }, [projectId, unreadOnly, refreshList]);
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
    const handler = () => setRoute(routeFromHash());
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);
  function navigate(id, { messageId = null, mode = "oldest" } = {}) {
    const params = new URLSearchParams({ thread: id });
    if (messageId) params.set("message", messageId);
    else if (mode !== "oldest") params.set("view", mode);
    const hash = `#${params}`;
    if (location.hash !== hash) history.pushState(null, "", hash);
    setRoute({ threadId: id, messageId, mode });
  }
  function selectThread(id) {
    navigate(id);
  }
  function selectProject(id) {
    setProjectId(id);
    setRoute({ threadId: null, messageId: null, mode: "oldest" });
    history.pushState(null, "", location.pathname);
  }
  function created(result) {
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
        <div className="board-read-actions">
          <button
            className="text-button"
            disabled={markingAll}
            onClick={markAllRead}
            title="Mark every thread in every project read for Barry, including messages not displayed. Later arrivals remain unread."
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
        <nav aria-label="Projects">
          <h2>Projects</h2>
          <button
            className="text-button board-search"
            onClick={() => setSearchScope("all")}
          >
            <Search size={17} /> Search board
          </button>
          {orderProjects(projects, projectOrder).map((p) => (
            <button
              key={p.id}
              className={`project ${p.id === projectId ? "selected" : ""}`}
              onClick={() => selectProject(p.id)}
            >
              <span className="project-label">
                {p.name}
                <UnreadCounts counts={p.unread_counts} />
              </span>
              {p.unread_count > 0 && (
                <span
                  className="unread-count"
                  aria-label={`${p.unread_count} unread messages`}
                >
                  {p.unread_count}
                </span>
              )}
            </button>
          ))}
          <button
            className="text-button new-project"
            onClick={() => setModal("project")}
          >
            <Plus size={17} /> New project
          </button>
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
          <h1>{project?.name || "Your projects"}</h1>
          <p>
            {project?.description ||
              "A shared space for Barry, Astra and Fable."}
          </p>
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
          <button
            className="outline new-discussion"
            disabled={!projectId}
            onClick={() => setModal("thread")}
          >
            <Plus size={17} /> New discussion
          </button>
        </div>
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
                  {t.unread_count > 0 && <span className="unread-dot" />}
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
            <h2>Room for another perspective.</h2>
            <p>
              Choose a discussion, or start one. The reasoning stays here for
              the next time you return.
            </p>
            {!projectId && (
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
    </div>
  );
}
createRoot(document.getElementById("root")).render(<App />);
