import React, { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { X, CornerDownRight } from "lucide-react";
import { kinds, titleCase, stamp, post } from "./api";

export function Modal({ title, close, children }) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    el.showModal();
    return () => el.close();
  }, []);
  return (
    <dialog
      ref={ref}
      aria-label={title}
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      onClick={(e) => {
        if (e.target === ref.current) close();
      }}
    >
      <div className="modal-heading">
        <h2>{title}</h2>
        <button className="icon" aria-label="Close dialog" onClick={close}>
          <X size={20} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function KindSelect({ value, onChange }) {
  return (
    <select
      aria-label="Message type"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      {kinds.map((k) => (
        <option key={k} value={k}>
          {titleCase(k)}
        </option>
      ))}
    </select>
  );
}
export function CreateForm({ type, projectId, onCreated, close }) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [kind, setKind] = useState("question");
  const request = useRef({});
  async function submit(e) {
    e.preventDefault();
    if (busy) return;
    const values = Object.fromEntries(new FormData(e.currentTarget));
    const body =
      type === "project" ? values : { ...values, project_id: projectId, kind };
    if (type === "project" && !body.repo_path?.trim()) body.repo_path = null;
    const signature = JSON.stringify(body);
    if (request.current.signature !== signature)
      request.current = { signature, id: crypto.randomUUID() };
    if (type !== "project") body.request_id = request.current.id;
    setBusy(true);
    setError("");
    try {
      const result = await post(
        type === "project" ? "/projects" : "/threads",
        body,
      );
      onCreated(result);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={type === "project" ? "New project" : "New discussion"}
      close={close}
    >
      <form onSubmit={submit} className="creation-form">
        <label>
          {type === "project" ? "Project name" : "Title"}
          <input
            autoFocus
            required
            name={type === "project" ? "name" : "title"}
            maxLength={200}
          />
        </label>
        {type === "project" ? (
          <>
            <label>
              Project ID <span>(short, stable name)</span>
              <input
                name="id"
                required
                pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
                maxLength={80}
                placeholder="e.g. diprotodon"
              />
            </label>
            <label>
              Description
              <textarea name="description" rows={3} maxLength={4000} />
            </label>
            <label>
              Repository path <span>(optional)</span>
              <input
                name="repo_path"
                placeholder="/Users/bwbrook/papers/project"
              />
            </label>
          </>
        ) : (
          <>
            <KindSelect value={kind} onChange={setKind} />
            <label>
              Opening message
              <textarea
                name="body"
                rows={8}
                required
                maxLength={100000}
                placeholder="What would you like to discuss? Markdown supported."
              />
            </label>
          </>
        )}
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="form-actions">
          <button type="button" className="secondary" onClick={close}>
            Cancel
          </button>
          <button disabled={busy}>
            {busy
              ? "Saving…"
              : type === "project"
                ? "Create project"
                : "Start discussion"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
export function Message({
  message,
  onReply,
  messages,
  onNavigate,
  highlighted,
  onMarkRead,
  readBusy,
  onQuickReply,
}) {
  const parent = messages.find((m) => m.id === message.parent_id);
  function follow(event, id) {
    if (
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    event.preventDefault();
    onNavigate(id);
  }
  return (
    <article
      className={`message${highlighted ? " highlighted" : ""}`}
      id={`message-${message.id}`}
      tabIndex={-1}
      aria-label={`Message ${message.id} by ${message.author}`}
    >
      <div className={`avatar ${message.author.toLowerCase()}`}>
        {message.author[0]}
      </div>
      <div className="message-content">
        <header className="message-heading">
          <strong>{message.author}</strong>
          {message.model && <span className="model">{message.model}</span>}
          <a
            className="message-number"
            href={`#thread=${message.thread_id}&message=${message.id}`}
            aria-label={`Link to message ${message.id}`}
            onClick={(event) => follow(event, message.id)}
          >
            #{message.id}
          </a>
        </header>
        <div className="message-date">
          {stamp(message.created_at)} <span>· {titleCase(message.kind)}</span>
        </div>
        {message.parent_id && (
          <a
            className="reply-reference"
            href={`#thread=${message.thread_id}&message=${message.parent_id}`}
            onClick={(event) => follow(event, message.parent_id)}
          >
            <CornerDownRight size={14} /> Reply to {parent?.author || "message"}{" "}
            #{message.parent_id}
          </a>
        )}
        <div className="markdown">
          <Markdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: ({ children, href }) => (
                <a href={href} target="_blank" rel="noreferrer noopener">
                  {children}
                </a>
              ),
              img: ({ alt }) => (
                <span className="image-reference">
                  [Image: {alt || "attached reference"}]
                </span>
              ),
            }}
          >
            {message.body}
          </Markdown>
        </div>
        <div className="message-actions">
          <button
            className="text-button reply-button"
            onClick={() => onReply(message)}
          >
            <CornerDownRight size={14} /> Reply
          </button>
          {message.author !== "Barry" &&
            ["proposal", "question"].includes(message.kind) && (
              <>
                <button
                  className="text-button"
                  onClick={() => onQuickReply(message, "accept")}
                >
                  Accept…
                </button>
                <button
                  className="text-button"
                  onClick={() => onQuickReply(message, "seen")}
                >
                  Seen, no action…
                </button>
              </>
            )}
          {onMarkRead && (
            <button
              className="text-button mark-through"
              disabled={readBusy}
              title="Includes all earlier messages in this discussion"
              onClick={onMarkRead}
            >
              Mark read through #{message.id}
            </button>
          )}
        </div>
      </div>
    </article>
  );
}

export function UnreadCounts({ counts }) {
  if (!counts) return null;
  return (
    <span
      className="participant-counts"
      title="Messages from others beyond each participant's explicit read acknowledgement. This does not prove whether they have read them."
    >
      Awaiting acknowledgement:{" "}
      {Object.entries(counts)
        .map(([name, count]) => `${name} ${count}`)
        .join(" · ")}
    </span>
  );
}

// Keep retry identities across closing/reopening a quick reply in this page.
const quickRequests = new Map();
export function QuickReply({ message, action, close, onPosted }) {
  const [body, setBody] = useState(
    action === "accept" ? "Accepted." : "Seen; no action needed from me.",
  );
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const kind = action === "accept" ? "decision" : "note";
  async function submit(event) {
    event.preventDefault();
    if (busy || !body.trim()) return;
    const payload = { body, kind, parent_id: message.id };
    const signature = JSON.stringify([message.thread_id, payload]);
    let attempt = quickRequests.get(signature);
    if (!attempt) {
      attempt = { id: crypto.randomUUID() };
      quickRequests.set(signature, attempt);
    }
    setBusy(true);
    setError("");
    try {
      if (!attempt.promise) {
        attempt.promise = post(`/threads/${message.thread_id}/messages`, {
          ...payload,
          request_id: attempt.id,
        });
        attempt.promise.catch(() => {
          attempt.promise = null;
        });
      }
      const result = await attempt.promise;
      onPosted(result);
    } catch (error) {
      setError(error.message);
    } finally {
      setBusy(false);
    }
  }
  function dismiss() {
    if (!busy) close();
  }
  return (
    <Modal
      title={action === "accept" ? "Accept this message" : "Seen, no action"}
      close={dismiss}
    >
      <form className="creation-form" onSubmit={submit}>
        <p>
          Reply to {message.author} #{message.id} as Barry. This posts a {kind};
          read markers and discussion status stay unchanged.
        </p>
        <blockquote className="quick-context">
          {message.body.slice(0, 300)}
          {message.body.length > 300 ? "…" : ""}
        </blockquote>
        <label>
          Your reply
          <textarea
            autoFocus
            required
            maxLength={100000}
            rows={3}
            value={body}
            disabled={busy}
            onChange={(event) => setBody(event.target.value)}
          />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="form-actions">
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={dismiss}
          >
            Cancel
          </button>
          <button disabled={busy || !body.trim()}>
            {busy ? "Posting…" : `Post ${kind}`}
          </button>
        </div>
      </form>
    </Modal>
  );
}
export function Composer({
  threadId,
  replyTo,
  clearReply,
  onPosting,
  onPosted,
}) {
  const key = `correspondence:draft:${threadId}`;
  const [body, setBody] = useState(() => {
    try {
      return localStorage.getItem(key) || "";
    } catch {
      return "";
    }
  });
  const [kind, setKind] = useState("note"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const request = useRef({}),
    textarea = useRef(null);
  useEffect(() => {
    try {
      localStorage.setItem(key, body);
    } catch {
      /* drafts are best effort */
    }
  }, [body, key]);
  useEffect(() => {
    if (replyTo) textarea.current?.focus();
  }, [replyTo]);
  async function submit(e) {
    e.preventDefault();
    if (busy || !body.trim()) return;
    const payload = { body, kind, parent_id: replyTo?.id ?? null };
    const signature = JSON.stringify(payload);
    if (request.current.signature !== signature)
      request.current = { signature, id: crypto.randomUUID() };
    setBusy(true);
    setError("");
    const postingContext = onPosting();
    try {
      const result = await post(`/threads/${threadId}/messages`, {
        ...payload,
        request_id: request.current.id,
      });
      // Clear the saved submitted draft even if navigation unmounted this form.
      // A newer draft must survive a response from an older composer instance.
      try {
        if (localStorage.getItem(key) === body) localStorage.removeItem(key);
      } catch {
        /* best effort */
      }
      setBody("");
      clearReply();
      request.current = {};
      onPosted(result, postingContext);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="composer" onSubmit={submit}>
      <div className="composer-toolbar">
        <KindSelect value={kind} onChange={setKind} />
        {replyTo && (
          <span className="reply-target">
            Replying to {replyTo.author} #{replyTo.id}
            <button
              type="button"
              className="icon"
              aria-label="Cancel targeted reply"
              onClick={clearReply}
            >
              <X size={16} />
            </button>
          </span>
        )}
      </div>
      <textarea
        ref={textarea}
        aria-label="Reply message"
        placeholder="Add to the discussion…"
        required
        disabled={busy}
        maxLength={100000}
        rows={6}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit(e);
        }}
      />
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <footer>
        <span>
          Posting as <strong>Barry</strong> · Markdown supported
        </span>
        <button disabled={busy || !body.trim()}>
          {busy ? "Posting…" : "Post reply"}
        </button>
      </footer>
    </form>
  );
}
