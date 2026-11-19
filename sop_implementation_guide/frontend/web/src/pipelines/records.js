// Reading a record's values and evidence, shared by the output tables, the
// Records tab and the full records view.

export const KIND_TEXT = {
  extracted: "read from the source",
  derived: "computed by a step",
  inferred: "model judgement (approved step)",
  assumed: "value you stated",
  manual: "your override",
};

export function short(v, n = 80) {
  if (v == null) return "";
  const s = typeof v === "object" ? JSON.stringify(v) : String(v);
  return s.length > n ? s.slice(0, n) + "…" : s;
}

export const isBlank = (v) => v == null || v === "";

export function isEdited(r) {
  return r.added_by_you || Object.values(r.prov || {}).some((p) => p.kind === "manual");
}

// What needs a look on a record, in words.
export function attention(r, fields) {
  const why = [];
  if (r.excerpt_verified === false) why.push("excerpt not found in source");
  const unq = fields.filter((f) => r.prov?.[f.name]?.verified === false).map((f) => f.name);
  if (unq.length) why.push(`no confirmed quote: ${unq.join(", ")}`);
  if (r.conflicts?.length) why.push(`conflict: ${r.conflicts.map((c) => c.field).join(", ")}`);
  const blank = fields.filter((f) => f.required && !f.accept_blank && isBlank(r.values?.[f.name])).map((f) => f.name);
  if (blank.length) why.push(`required blank: ${blank.join(", ")}`);
  return why;
}

// One field's evidence state: what the Records tab shows in its cell.
export function cellState(r, f) {
  const v = r.values?.[f.name];
  const p = r.prov?.[f.name];
  if (isBlank(v)) return "blank";
  if ((r.conflicts || []).some((c) => c.field === f.name)) return "conflict";
  if (p?.verified === false) return "unquoted";
  if (p?.kind === "extracted" && p?.quote) return "quoted";
  return p?.kind || "set";
}

// Fields a reviewer still has to look at: blank, unquoted or contradicted, and
// not accepted as they are. A field the planner accepts blanks for is fine blank.
export function toReview(r, fields, mark) {
  const ok = new Set(mark?.accepted || []);
  return fields.filter((f) => {
    if (ok.has(f.name)) return false;
    const st = cellState(r, f);
    if (st === "blank") return !f.accept_blank;
    return st === "unquoted" || st === "conflict";
  }).map((f) => f.name);
}

export function recordStatus(r, fields, mark) {
  if (mark?.flag) return { kind: "flagged", text: "Flagged" };
  const n = toReview(r, fields, mark).length;
  return n ? { kind: "review", text: `${n} to review` } : { kind: "complete", text: "Complete" };
}

// The record's name: its first field.
export function recordName(r, fields) {
  return short(r.values?.[fields[0]?.name], 60) || r.id;
}

// Model steps that can run again for one record.
export const RERUN_TYPES = ["research", "llm_fill", "geo_tie"];

// The Records filters: search text, then All / To review / Unquoted / Flagged.
export function matchesFilter(r, fields, mark, filt, q) {
  if (q.trim() && !Object.values(r.values || {}).some((v) => String(v ?? "").toLowerCase().includes(q.trim().toLowerCase()))) return false;
  if (filt === "review") return recordStatus(r, fields, mark).kind === "review";
  if (filt === "unquoted") return fields.some((f) => cellState(r, f) === "unquoted");
  if (filt === "flagged") return !!mark?.flag;
  return true;
}
