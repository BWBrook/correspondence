export async function api(path, options = {}) {
  const { body, ...rest } = options;
  const response = await fetch(`/api${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      "X-Correspondence-Client": "browser",
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    let detail;
    try {
      detail = (await response.json()).detail;
    } catch {
      /* plain HTTP error */
    }
    const error = new Error(
      typeof detail === "string"
        ? detail
        : `Request failed (${response.status}). Please try again.`,
    );
    error.status = response.status;
    throw error;
  }
  return response.json();
}
export const post = (path, body) => api(path, { method: "POST", body });
export const kinds = [
  "note",
  "question",
  "proposal",
  "evidence",
  "counterexample",
  "decision",
];
export const titleCase = (s) => s[0].toUpperCase() + s.slice(1);
export const participantName = (author) =>
  ({ Fable: "Claude", Astra: "Codex" })[author] || author;
export const authorLabel = (author) =>
  ({ Fable: "Fable (Claude)", Astra: "Astra (Codex)" })[author] || author;
export function stamp(value) {
  return new Date(value).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}
