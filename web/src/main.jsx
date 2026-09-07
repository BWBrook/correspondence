import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Plus,
  Search,
  RefreshCw,
  Download,
  UserRound,
  Monitor,
  ArrowLeft,
  FolderOpen,
} from "lucide-react";
import { api, post } from "./api";
import { Composer, CreateForm, Message } from "./components";
import "./style.css";

function selectedFromHash() {
  return (
    Number(new URLSearchParams(location.hash.slice(1)).get("thread")) || null
  );
}
function App() {
  const [projects, setProjects] = useState([]),
    [projectId, setProjectId] = useState(null);
  const [threads, setThreads] = useState([]),
    [threadId, setThreadId] = useState(selectedFromHash);
  const [detail, setDetail] = useState(null),
    [unreadOnly, setUnreadOnly] = useState(false);
  const [query, setQuery] = useState(""),
    [results, setResults] = useState(null),
    [searching, setSearching] = useState(false);
  const [modal, setModal] = useState(null),
    [error, setError] = useState(""),
    [replyTo, setReplyTo] = useState(null);
  const [loading, setLoading] = useState(false),
    [statusBusy, setStatusBusy] = useState(false);
  const currentThread = useRef(threadId),
    currentProject = useRef(projectId),
    currentUnread = useRef(unreadOnly),
    listRequest = useRef(0),
    readPane = useRef(null);
  currentThread.current = threadId;
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
  const loadThread = useCallback(
    async (id, append = false, after = 0) => {
      if (!id) return;
      if (currentThread.current !== id) return;
      setLoading(true);
      try {
        const result = await api(`/threads/${id}?after=${after}&limit=100`);
        if (currentThread.current !== id) return;
        setDetail((prev) =>
          append && prev?.thread.id === id
            ? { ...result, messages: [...prev.messages, ...result.messages] }
            : result,
        );
        setProjectId(result.thread.project_id);
        const last = result.messages.at(-1);
        if (last && document.visibilityState === "visible") {
          await post(`/threads/${id}/read`, { message_id: last.id });
          await refreshProjects();
          await refreshList();
        }
      } catch (e) {
        if (currentThread.current === id) setError(e.message);
      } finally {
        if (currentThread.current === id) setLoading(false);
      }
    },
    [refreshProjects, refreshList],
  );
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
    setDetail(null);
    setReplyTo(null);
    setError("");
    setLoading(false);
    if (threadId) loadThread(threadId);
    // Fetch on selection only; refreshing counts must not reload a reader's discussion.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId]);
  useEffect(() => {
    const handler = () => {
      const id = selectedFromHash();
      if (id) setThreadId(id);
      const message = Number(
        new URLSearchParams(location.hash.slice(1)).get("message"),
      );
      if (message)
        document
          .getElementById(`message-${message}`)
          ?.scrollIntoView({ block: "start" });
    };
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);
  useEffect(() => {
    const message = Number(
      new URLSearchParams(location.hash.slice(1)).get("message"),
    );
    if (message)
      document
        .getElementById(`message-${message}`)
        ?.scrollIntoView({ block: "start" });
  }, [detail]);
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
  function selectThread(id) {
    setThreadId(id);
    location.hash = `thread=${id}`;
  }
  function selectProject(id) {
    setProjectId(id);
    setThreadId(null);
    setDetail(null);
    setQuery("");
    history.replaceState(null, "", location.pathname);
  }
  async function changeStatus(status) {
    setStatusBusy(true);
    try {
      await post(`/threads/${threadId}/status`, { status });
      await loadThread(threadId);
      await refreshList();
    } catch (e) {
      setError(e.message);
    } finally {
      setStatusBusy(false);
    }
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
  const listed = threads.find((t) => t.id === threadId);
  const newMessages =
    detail && listed && listed.message_count > detail.thread.message_count;
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
              <span>{p.name}</span>
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
                    setQuery("");
                    selectThread(m.thread_id);
                  }}
                >
                  <h3>{m.thread_title}</h3>
                  <p>
                    {m.author} · {m.kind}
                  </p>
                  <div>{m.body.slice(0, 180)}</div>
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
      <main className="reader" ref={readPane}>
        {error && (
          <div className="error-banner" role="alert">
            {error}
            <button className="text-button" onClick={() => setError("")}>
              Dismiss
            </button>
          </div>
        )}
        {detail ? (
          <>
            <header className="reader-header">
              <button
                className="icon mobile-back"
                aria-label="Back to discussions"
                onClick={() => selectProject(projectId)}
              >
                <ArrowLeft size={20} />
              </button>
              <h2>{detail.thread.title}</h2>
              <div className="reader-actions">
                <select
                  aria-label="Discussion status"
                  value={detail.thread.status}
                  disabled={statusBusy}
                  onChange={(e) => changeStatus(e.target.value)}
                >
                  <option value="open">Open</option>
                  <option value="resolved">Resolved</option>
                </select>
                <a
                  href={`/api/threads/${threadId}/export`}
                  download={`discussion-${threadId}.md`}
                >
                  <Download size={17} /> Export
                </a>
              </div>
            </header>
            {newMessages && (
              <button
                className="new-messages"
                onClick={() => loadThread(threadId)}
              >
                New messages available · Refresh discussion
              </button>
            )}
            <div className="messages">
              {detail.messages.map((m) => (
                <Message
                  key={m.id}
                  message={m}
                  messages={detail.messages}
                  onReply={setReplyTo}
                />
              ))}
            </div>
            {detail.has_more && (
              <button
                className="outline load-more"
                disabled={loading}
                onClick={() => loadThread(threadId, true, detail.next_after)}
              >
                {loading ? "Loading…" : "Load more messages"}
              </button>
            )}
            <Composer
              key={threadId}
              threadId={threadId}
              replyTo={replyTo}
              clearReply={() => {
                if (currentThread.current === threadId) setReplyTo(null);
              }}
              onPosted={() => {
                loadThread(threadId);
                refreshList().catch((e) => setError(e.message));
              }}
            />
          </>
        ) : (
          <div className="empty-reader">
            <h2>
              {loading
                ? "Loading discussion…"
                : "Room for another perspective."}
            </h2>
            <p>
              {loading
                ? "Retrieving the correspondence."
                : "Choose a discussion, or start one. The reasoning stays here for the next time you return."}
            </p>
            {!projectId && (
              <button onClick={() => setModal("project")}>
                <Plus size={17} /> Create your first project
              </button>
            )}
          </div>
        )}
      </main>
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
