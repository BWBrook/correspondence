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
export const USER = "User";
let userName = USER;
export const setUserName = (name) => {
  if (name) userName = name;
};
export const getUserName = () => userName;
export const participantName = (author) =>
  ({ Fable: "Claude", Astra: "Codex" })[author] || author;
export const authorLabel = (author) =>
  author === USER
    ? userName
    : ({ Fable: "Fable (Claude)", Astra: "Astra (Codex)" })[author] || author;
export function stamp(value) {
  return new Date(value).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}
export function shortTime(value) {
  const date = new Date(value);
  const now = new Date();
  const days = Math.round(
    (new Date(now.toDateString()) - new Date(date.toDateString())) / 86400000,
  );
  if (days === 0)
    return date.toLocaleTimeString(undefined, { timeStyle: "short" });
  if (days === 1) return "Yesterday";
  if (days < 7) return date.toLocaleDateString(undefined, { weekday: "short" });
  return date.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    ...(date.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
  });
}
export function plainExcerpt(markdown) {
  return (markdown || "")
    .replace(/```[\s\S]*?(```|$)/g, " ")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[`*_>#|]+/g, "")
    .replace(/\s+/g, " ")
    .trim();
}
