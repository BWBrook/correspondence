import React, { useRef, useState } from "react";
import { Modal } from "./components";
import { post } from "./api";

export function ProjectArchiveDialog({ project, close, onArchived }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef(false);
  async function archive() {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await post(
        `/projects/${encodeURIComponent(project.id)}/archive`,
        { expected_state: project.state_token },
      );
      await onArchived(result);
    } catch (e) {
      setError(e.message);
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }
  return (
    <Modal title={`Archive ${project.name}?`} close={close}>
      <div className="archive-confirmation">
        <p>
          Its discussions and read acknowledgements will be preserved. They will
          appear only when you select Archive, including in agent searches.
        </p>
        <p>
          You can start a fresh project using <strong>{project.id}</strong>.
          Restore the archived project at any time; name clashes are resolved
          automatically.
        </p>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="form-actions">
          <button className="secondary" onClick={close}>
            Cancel
          </button>
          <button disabled={busy} onClick={archive}>
            {busy ? "Archiving…" : "Archive project"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
