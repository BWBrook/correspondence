import React, { useEffect, useRef, useState } from "react";
import { api, stamp, titleCase, authorLabel } from "./api";
import { Modal } from "./components";

const PAGE_SIZE = 100;

export function SearchDialog({
  initialScope,
  archived = false,
  project,
  threadId,
  close,
  onSelect,
}) {
  const [scope, setScope] = useState(initialScope);
  const activeScope =
    (scope === "project" && !project) || (scope === "thread" && !threadId)
      ? "all"
      : scope;
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const request = useRef(0),
    controller = useRef(null),
    pending = useRef(false),
    input = useRef(null);

  useEffect(() => {
    input.current?.focus();
  }, []);

  async function search(before, sequence, abort) {
    const params = new URLSearchParams({
      q: query.trim(),
      limit: PAGE_SIZE,
      archived,
    });
    if (activeScope === "project") params.set("project_id", project.id);
    if (activeScope === "thread") params.set("thread_id", threadId);
    if (before) params.set("before", before);
    try {
      const page = await api(`/search?${params}`, { signal: abort.signal });
      if (sequence !== request.current || abort.signal.aborted) return;
      setResults((previous) => (before ? [...previous, ...page] : page));
      setHasMore(page.length === PAGE_SIZE);
    } catch (error) {
      if (sequence === request.current && !abort.signal.aborted)
        setError(error.message);
    } finally {
      if (sequence === request.current && !abort.signal.aborted) {
        setLoading(false);
        pending.current = false;
      }
    }
  }

  useEffect(() => {
    const sequence = ++request.current;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setResults([]);
    setHasMore(false);
    setError("");
    pending.current = Boolean(query.trim());
    setLoading(pending.current);
    const timer = query.trim()
      ? setTimeout(() => search(null, sequence, abort), 250)
      : null;
    return () => {
      clearTimeout(timer);
      abort.abort();
    };
  }, [query, activeScope, project?.id, threadId, archived]);

  // Load-more creates its own controller, which must also be cancelled on close.
  useEffect(
    () => () => {
      controller.current?.abort();
    },
    [],
  );

  function loadMore() {
    if (pending.current || !hasMore) return;
    pending.current = true;
    setLoading(true);
    setError("");
    const sequence = ++request.current;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    search(results.at(-1).id, sequence, abort);
  }

  return (
    <Modal
      title={archived ? "Search archive" : "Search correspondence"}
      close={close}
    >
      <div className="search-dialog">
        <label>
          Search in
          <select
            aria-label="Search scope"
            value={activeScope}
            onChange={(event) => setScope(event.target.value)}
          >
            <option value="all">
              {archived ? "All archived projects" : "Whole board"}
            </option>
            <option value="project" disabled={!project}>
              This project{project ? ` — ${project.name}` : ""}
            </option>
            <option value="thread" disabled={!threadId}>
              This discussion
            </option>
          </select>
        </label>
        <label>
          Words or phrase
          <input
            ref={input}
            autoFocus
            type="search"
            aria-label="Search words or phrase"
            value={query}
            maxLength={500}
            placeholder="Search message text and discussion titles"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <p className="search-hint">
          {archived
            ? "Searches archived projects only. Read acknowledgements remain preserved."
            : "Matches the text you enter. Results open at the matching message."}
        </p>
        <p role="status" className="search-status">
          {loading
            ? "Searching…"
            : !query.trim()
              ? "Enter text to search."
              : error
                ? "Search could not finish."
                : results.length
                  ? `${results.length} matching ${results.length === 1 ? "message" : "messages"} loaded · newest first`
                  : "No matching messages in this scope."}
        </p>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="search-results">
          {results.map((message) => (
            <button
              className="thread-row search-result"
              key={message.id}
              onClick={() => onSelect(message)}
            >
              <span className="search-project">{message.project_name}</span>
              <h3>{message.thread_title}</h3>
              <p>
                {authorLabel(message.author)} · {titleCase(message.kind)} · #
                {message.id} · {stamp(message.created_at)}
              </p>
              <div>{message.snippet}</div>
            </button>
          ))}
        </div>
        {hasMore && (
          <button className="outline" disabled={loading} onClick={loadMore}>
            Load more matches
          </button>
        )}
      </div>
    </Modal>
  );
}
