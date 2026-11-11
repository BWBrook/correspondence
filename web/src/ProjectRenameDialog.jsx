import React, { useState } from "react";
import { Modal } from "./components";
import { post } from "./api";

export function ProjectRenameDialog({ project, close, onRenamed }) {
  const [name, setName] = useState(project.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function rename(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await onRenamed(
        await post(`/projects/${encodeURIComponent(project.id)}/rename`, {
          name,
          expected_state: project.state_token,
        }),
      );
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  }
  return (
    <Modal title={`Rename ${project.name}`} close={close}>
      <form onSubmit={rename}>
        <label>
          Name
          <input value={name} maxLength={200} autoFocus onChange={(e) => setName(e.target.value)} />
        </label>
        <p className="hint">
          The id <strong>{project.id}</strong> stays, so turn hooks and main
          sessions keep working.
        </p>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="form-actions">
          <button type="button" className="secondary" onClick={close}>
            Cancel
          </button>
          <button disabled={busy || !name.trim() || name.trim() === project.name}>
            {busy ? "Renaming…" : "Rename"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
