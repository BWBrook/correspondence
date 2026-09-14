import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Plus,
  Search,
  RefreshCw,
  UserRound,
  Monitor,
  FolderOpen,
} from "lucide-react";
import { api } from "./api";
import { CreateForm, UnreadCounts } from "./components";
import { ThreadReader } from "./ThreadReader";
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
function App() {
  const [projects, setProjects] = useState([]),
    [projectId, setProjectId] = useState(null);
  const [threads, setThreads] = useState([]),
    [route, setRoute] = useState(routeFromHash);
  const threadId = route.threadId;
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [query, setQuery] = useState(""),
    [results, setResults] = useState(null),
    [searching, setSearching] = useState(false);
  const [modal, setModal] = useState(null),
    [error, setError] = useState("");
  const currentProject = useRef(projectId),
    currentUnread = useRef(unreadOnly),
    listRequest = useRef(0);
  currentProject.current = projectId;
  currentUnread.current = unreadOnly;
  const project = projects.find((p) => p.id === projectId);
  const refreshProjects = useCallback(async () => {
    const list = await api("/projects");
    setProjects(list);
    setProjectId((previous) => previous || list[0]?.id || null);
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
  useEffect(() => {
    const controller = new AbortController();
    setResults(null);
    if (!query.trim()) {
      setSearching(false);
      return;
    }
    setSearching(true);
    const timer = setTimeout(() => {
      api(
        `/search?q=${encodeURIComponent(query)}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`,
        { signal: controller.signal },
      )
        .then(setResults)
        .catch((e) => {
          if (e.name !== "AbortError") setError(e.message);
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearching(false);
        });
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, projectId]);
  function navigate(id, { messageId = null, mode = "oldest" } = {}) {
    const params = new URLSearchParams({ thread: id });
    if (messageId) params.set("message", messageId);
    else if (mode !== "oldest") params.set("view", mode);
    const hash = `#${params}`;
    if (location.hash !== hash) history.pushState(null, "", hash);
    setRoute({ threadId: id, messageId, mode });
    setQuery("");
  }
  function selectThread(id) {
    navigate(id);
  }
  function selectProject(id) {
    setProjectId(id);
    setRoute({ threadId: null, messageId: null, mode: "oldest" });
    setQuery("");
    history.pushState(null, "", location.pathname);
  }
  function created(result) {
    const type = modal;
    setModal(null);
    setQuery("");
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
        <nav aria-label="Projects">
          <h2>Projects</h2>
          {projects.map((p) => (
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
          <label className="search">
            <Search size={18} />
            <input
              aria-label="Search correspondence"
              placeholder="Search correspondence"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
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
          {query.trim() ? (
            <>
              <p className="list-note">
                {searching
                  ? "Searching…"
                  : `${results?.length || 0} matching ${results?.length === 1 ? "message" : "messages"}`}
              </p>
              {results?.map((m) => (
                <button
                  className="thread-row search-result"
                  key={m.id}
                  onClick={() => {
                    navigate(m.thread_id, { messageId: m.id });
                  }}
                >
                  <h3>{m.thread_title}</h3>
                  <p>
                    {m.author} · {m.kind}
                  </p>
                  <div>{m.snippet}</div>
                </button>
              ))}
            </>
          ) : threads.length ? (
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
          listed={threads.find((t) => t.id === threadId)}
          onNavigate={(options) => navigate(threadId, options)}
          onProject={setProjectId}
          onBack={() => selectProject(projectId)}
          onRefresh={refreshCounts}
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
