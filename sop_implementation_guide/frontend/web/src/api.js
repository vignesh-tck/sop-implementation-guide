const API_BASE = (import.meta.env.VITE_API_BASE || "http://localhost:8000").replace(/\/+$/, "");

export async function call(method, path, body) {
  const res = await fetch(API_BASE + path, {
    method,
    headers: { "Content-Type": "application/json" },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : typeof data === "string" ? data : res.statusText;
    throw new Error(`${res.status} — ${detail}`);
  }
  return data;
}

export const money = (n) => (n == null ? "—" : "$" + Number(n).toLocaleString());
export const num = (n, d = 1) => (n == null ? "—" : Number(n).toFixed(d));

export function scoreColour(v) {
  if (v == null) return "var(--muted)";
  return v >= 0.6 ? "var(--teal)" : v >= 0.35 ? "var(--amber)" : "var(--red)";
}

// Same thresholds, 0–100 scale — used for sop_index_norm and feasibility_score on the map.
export function score100Colour(v) {
  if (v == null) return "var(--muted)";
  return v >= 60 ? "var(--teal)" : v >= 35 ? "var(--amber)" : "var(--red)";
}

export function deadlineLabel(iso) {
  if (!iso) return { text: "—", soon: false };
  const days = Math.round((new Date(iso) - new Date()) / 86400000);
  const soon = days >= 0 && days <= 45;
  const suffix = days < 0 ? " (passed)" : days <= 45 ? ` (${days}d)` : "";
  return { text: iso + suffix, soon };
}

export { API_BASE };
