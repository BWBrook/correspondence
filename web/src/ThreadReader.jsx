import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowUp,
  ArrowDown,
  Download,
  RefreshCw,
  Check,
  Search,
} from "lucide-react";
import { api, post } from "./api";
import { Composer, Message, QuickReply } from "./components";

// A reader owns one contiguous window of a discussion. Navigation can replace
// that window; paging extends it. Neither operation acknowledges any messages.
export function ThreadReader({
  route,
  listed,
  onNavigate,
  onProject,
  onBack,
  onSearch,
  onRefresh,
}) {
  const { threadId, archived } = route;
  const [detail, setDetail] = useState(null);
  const [quickReply, setQuickReply] = useState(null);
  const [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [replyTo, setReplyTo] = useState(null),
    [highlight, setHighlight] = useState(null);
  const pane = useRef(null),
    toolbar = useRef(null),
    detailRef = useRef(null);
  const routeRef = useRef(route);
  const pendingScroll = useRef(null),
    request = useRef(0),
    controller = useRef(null);
  const mounted = useRef(true);
  detailRef.current = detail;
  routeRef.current = route;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.abort();
    };
  }, []);

  function readingAnchor() {
    const main = pane.current;
    if (!main) return null;
    const top = main.getBoundingClientRect().top;
    const boundary = Math.max(
      top,
      toolbar.current?.getBoundingClientRect().bottom || top,
    );
    const element = [...main.querySelectorAll(".message")].find(
      (el) => el.getBoundingClientRect().bottom > boundary,
    );
    return element
      ? {
          type: "restore",
          id: element.id,
          offset: element.getBoundingClientRect().top - top,
        }
      : null;
  }

  function scrollToTarget(target) {
    const main = pane.current;
    if (!main || !target) return;
    if (target.type === "top") {
      main.scrollTop = 0;
      main.querySelector("h2")?.focus({ preventScroll: true });
    } else if (target.type === "reply") {
      main.scrollTop = main.scrollHeight;
      main.querySelector(".composer textarea")?.focus({ preventScroll: true });
    } else {
      const element = document.getElementById(target.id);
      if (!element) return;
      const offset =
        target.type === "restore"
          ? target.offset
          : (toolbar.current?.offsetHeight || 0) +
            parseFloat(getComputedStyle(main).paddingTop) +
            (target.unread ? 64 : 14);
      main.scrollTop +=
        element.getBoundingClientRect().top -
        main.getBoundingClientRect().top -
        offset;
      if (target.type !== "restore") element.focus({ preventScroll: true });
    }
  }

  useLayoutEffect(() => {
    scrollToTarget(pendingScroll.current);
    pendingScroll.current = null;
  }, [detail]);

  async function loadPage({
    mode = "oldest",
    after = 0,
    before,
    messageId,
    merge = "replace",
  } = {}) {
    const sequence = ++request.current;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setLoading(true);
    setError("");
    try {
      if (messageId) {
        const exact = await api(`/messages/${messageId}?archived=${archived}`, {
          signal: abort.signal,
        });
        if (exact.message.thread_id !== threadId)
          throw new Error(
            `Message #${messageId} belongs to another discussion.`,
          );
        after = messageId - 1;
        mode = "oldest";
      }
      const query = new URLSearchParams({ mode, after, limit: 50, archived });
      if (before) query.set("before", before);
      const result = await api(`/threads/${threadId}?${query}`, {
        signal: abort.signal,
      });
      if (!mounted.current || sequence !== request.current) return;
      const previous = detailRef.current;
      let next = result;
      if (previous && merge !== "replace") {
        const messages =
          merge === "prepend"
            ? [...result.messages, ...previous.messages]
            : [...previous.messages, ...result.messages];
        next = {
          ...result,
          messages,
          has_before:
            merge === "prepend" ? result.has_before : previous.has_before,
          previous_before: messages[0]?.id ?? null,
          next_after: messages.at(-1)?.id ?? 0,
          has_more: (messages.at(-1)?.id ?? 0) < result.thread.last_message_id,
        };
      }
      pendingScroll.current =
        merge !== "replace"
          ? readingAnchor()
          : messageId
            ? { type: "message", id: `message-${messageId}` }
            : mode === "latest" ||
                (mode === "unread" && !result.thread.first_unread_message_id)
              ? { type: "reply" }
              : mode === "unread"
                ? {
                    type: "message",
                    id: `message-${result.thread.first_unread_message_id}`,
                    unread: true,
                  }
                : { type: "top" };
      setHighlight(
        messageId ||
          (mode === "unread" ? result.thread.first_unread_message_id : null),
      );
      setNotice(
        mode === "unread" && !result.thread.first_unread_message_id
          ? "No unread messages. Showing the latest replies."
          : "",
      );
      setDetail(next);
      onProject(result.thread.project_id);
    } catch (e) {
      if (
        mounted.current &&
        sequence === request.current &&
        e.name !== "AbortError"
      ) {
        if (e.status === 404) setDetail(null);
        setError(e.message);
      }
    } finally {
      if (mounted.current && sequence === request.current) setLoading(false);
    }
  }

  useEffect(() => {
    const present =
      route.messageId &&
      detailRef.current?.messages.some((m) => m.id === route.messageId);
    if (present) {
      // A second navigation supersedes any in-flight page request, even when
      // its target is already in the current window.
      request.current += 1;
      controller.current?.abort();
      setLoading(false);
      setError("");
      setHighlight(route.messageId);
      scrollToTarget({ type: "message", id: `message-${route.messageId}` });
    } else {
      loadPage({ mode: route.mode, messageId: route.messageId });
    }
    // Only deliberate navigation replaces the window; polling leaves it intact.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route]);

  async function acknowledge(messageId, wholeThread = false) {
    if (archived || busy || !messageId) return;
    setBusy(true);
    setError("");
    try {
      // Capture the clicked snapshot's ID. Never resolve "latest" on the server
      // during this write: arrivals after the click must remain unread.
      await post(`/threads/${threadId}/read`, { message_id: messageId });
      const fresh = await api(
        `/threads/${threadId}?after=${messageId}&limit=1`,
      );
      if (!mounted.current) return;
      pendingScroll.current = readingAnchor();
      setDetail(
        (prev) =>
          prev && {
            ...prev,
            thread: fresh.thread,
            has_more: prev.next_after < fresh.thread.last_message_id,
          },
      );
      setNotice(
        `${wholeThread ? "Thread marked read" : "Marked read"} through #${messageId}. ${fresh.thread.unread_count} unread ${fresh.thread.unread_count === 1 ? "message remains" : "messages remain"}.`,
      );
      await onRefresh();
    } catch (e) {
      if (mounted.current) setError(e.message);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function changeStatus() {
    if (archived) return;
    const next = detail.thread.status === "resolved" ? "open" : "resolved";
    if (
      !confirm(
        `Mark this discussion ${next}? This appends a permanent status note.`,
      )
    )
      return;
    setBusy(true);
    try {
      const thread = await post(`/threads/${threadId}/status`, {
        status: next,
      });
      if (!mounted.current) return;
      setDetail((prev) => ({
        ...prev,
        thread,
        has_more: prev.next_after < thread.last_message_id,
      }));
      await onRefresh();
    } catch (e) {
      if (mounted.current) setError(e.message);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  // List polling can advertise newer arrivals without moving the reader or
  // extending the snapshot that an in-flight acknowledgement already captured.
  const snapshot = detail?.thread;
  const thread =
    listed &&
    snapshot &&
    listed.last_message_id >= snapshot.last_message_id &&
    listed.last_read_message_id >= snapshot.last_read_message_id
      ? listed
      : snapshot;
  const latestId = Math.max(
    thread?.last_message_id || 0,
    listed?.last_message_id || 0,
  );
  const newMessages = snapshot && latestId > snapshot.last_message_id;
  const unreadCount = thread?.unread_count;
  const locked = loading || busy;

  return (
    <main className="reader" ref={pane}>
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
              onClick={onBack}
            >
              <ArrowLeft size={20} />
            </button>
            <h2 tabIndex={-1}>{thread.title}</h2>
            <div className="reader-actions">
              <button className="text-button" onClick={onSearch}>
                <Search size={16} /> Search
              </button>
              {!archived && (
                <button
                  className="text-button status-toggle"
                  disabled={locked}
                  onClick={changeStatus}
                >
                  {thread.status === "resolved"
                    ? "Resolved · Reopen"
                    : "Open · Resolve"}
                </button>
              )}
              <a
                href={`/api/threads/${threadId}/export?archived=${archived}`}
                download={`discussion-${threadId}.md`}
              >
                <Download size={17} /> Export
              </a>
            </div>
          </header>
          {archived && (
            <p className="archive-banner">
              Archived · Read-only. Restore this project from its discussion
              list to continue writing.
            </p>
          )}
          <details className="read-state">
            <summary>Participant acknowledgements</summary>
            <p>
              Explicit read markers only; these do not prove attention or
              agreement. Counts exclude each participant’s own messages.
            </p>
            <ul>
              {Object.entries(thread.read_markers || {}).map(([name, id]) => (
                <li key={name}>
                  <strong>{name}</strong>:{" "}
                  {id
                    ? `acknowledged through #${id}`
                    : "no read acknowledgement"}{" "}
                  · {thread.unread_counts[name]} remaining
                </li>
              ))}
            </ul>
          </details>
          <nav
            className="thread-toolbar"
            ref={toolbar}
            aria-label="Discussion navigation"
          >
            <div className="thread-navigation">
              <button
                className="text-button"
                disabled={locked}
                onClick={() => onNavigate({ mode: "oldest" })}
              >
                <ArrowUp size={16} /> Top
              </button>
              <button
                className="text-button"
                disabled={locked || !unreadCount}
                onClick={() => onNavigate({ mode: "unread" })}
              >
                First unread
              </button>
              <button
                className="outline"
                disabled={locked}
                onClick={() => onNavigate({ mode: "latest" })}
              >
                <ArrowDown size={16} /> {archived ? "Latest" : "Latest / Reply"}
              </button>
              <button
                className="icon"
                aria-label="Refresh messages without moving reading position"
                disabled={locked}
                onClick={() =>
                  loadPage({ after: detail.next_after, merge: "append" })
                }
              >
                <RefreshCw size={16} />
              </button>
            </div>
            <div className="thread-read-actions">
              <span>
                {unreadCount} unread · {thread.message_count} messages
              </span>
              {!archived && (
                <button
                  className="text-button"
                  disabled={locked || !unreadCount}
                  title={`Includes all messages through #${latestId}, even those not displayed`}
                  onClick={() => acknowledge(latestId, true)}
                >
                  <Check size={16} /> Mark thread read
                </button>
              )}
            </div>
            <div className="reader-notice" role="status">
              {loading ? "Loading messages…" : busy ? "Saving…" : notice}
            </div>
          </nav>
          {newMessages && (
            <div className="new-messages">
              New replies available{" "}
              <button
                className="text-button"
                disabled={locked}
                onClick={() => onNavigate({ mode: "latest" })}
              >
                Jump to latest
              </button>
            </div>
          )}
          {detail.has_before && (
            <button
              className="outline load-more"
              disabled={locked}
              onClick={() =>
                loadPage({ before: detail.previous_before, merge: "prepend" })
              }
            >
              Load older messages
            </button>
          )}
          <div className="messages">
            {detail.messages.map((m) => (
              <React.Fragment key={m.id}>
                {m.id === thread.first_unread_message_id && (
                  <div className="unread-divider">
                    New since you last marked read
                  </div>
                )}
                <Message
                  message={m}
                  archived={archived}
                  messages={detail.messages}
                  onReply={setReplyTo}
                  onQuickReply={(message, action) =>
                    setQuickReply({ message, action })
                  }
                  highlighted={highlight === m.id}
                  onNavigate={(id) => onNavigate({ messageId: id })}
                  onMarkRead={
                    !archived && m.id > thread.last_read_message_id
                      ? () => acknowledge(m.id)
                      : null
                  }
                  readBusy={locked}
                />
              </React.Fragment>
            ))}
          </div>
          {(detail.has_more || newMessages) && (
            <button
              className="outline load-more"
              disabled={locked}
              onClick={() =>
                loadPage({ after: detail.next_after, merge: "append" })
              }
            >
              Load newer messages
            </button>
          )}
          {!archived && (
            <Composer
              threadId={threadId}
              replyTo={replyTo}
              clearReply={() => setReplyTo(null)}
              onPosting={() => ({
                route: routeRef.current,
                request: request.current,
              })}
              onPosted={(message, context) => {
                if (!mounted.current) return;
                if (
                  routeRef.current === context.route &&
                  request.current === context.request
                )
                  onNavigate({ messageId: message.id });
                onRefresh().catch((e) => {
                  if (mounted.current) setError(e.message);
                });
              }}
            />
          )}
          {!archived && quickReply && (
            <QuickReply
              {...quickReply}
              close={() => setQuickReply(null)}
              onPosted={(message) => {
                if (!mounted.current) return;
                setQuickReply(null);
                onNavigate({ messageId: message.id });
                onRefresh().catch((e) => setError(e.message));
              }}
            />
          )}
        </>
      ) : (
        <div className="empty-reader">
          <h2>{loading ? "Loading discussion…" : "Discussion unavailable"}</h2>
          <p>
            {loading
              ? "Retrieving the correspondence."
              : "Choose another discussion or try again."}
          </p>
          <button
            className="outline"
            onClick={() => onNavigate({ mode: "oldest" })}
            disabled={loading}
          >
            Try again
          </button>
          <button className="text-button" onClick={onBack}>
            Back to discussions
          </button>
        </div>
      )}
    </main>
  );
}
