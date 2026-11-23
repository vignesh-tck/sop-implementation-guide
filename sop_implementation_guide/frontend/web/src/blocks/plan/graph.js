// Plan steps as a graph, on the client: numbering, "after" text, a live diff
// while an SME edits, and keeping the order readable as links change. The
// server (backend/tools/plan_graph.py) does the same diff for the audit trail
// and the time estimate; this side only needs it for drawing.

export const DIFF_FIELDS = [
  ["title", "Activity"], ["how", "Description"], ["after", "Depends on"], ["condition", "Only if"],
  ["actor", "Primary owner"], ["owner", "Primary owner, exactly"], ["secondary_owner", "Secondary owner"],
  ["time", "Duration"],
];

export const numbers = (steps) => Object.fromEntries(steps.map((s, i) => [s.id, i + 1]));

// "After 2, 4", "After 3 or 5", or "Start".
export function afterText(s, num) {
  const deps = (s.depends_on || []).map((d) => num[d]).filter(Boolean).sort((a, b) => a - b);
  if (!deps.length) return "Start";
  return "After " + deps.join(s.depends_mode === "any" ? " or " : ", ");
}

// The step numbers as an editable string, and back: "2, 4" / "3 or 5".
export const afterInput = (s, num) => {
  const deps = (s.depends_on || []).map((d) => num[d]).filter(Boolean).sort((a, b) => a - b);
  return deps.join(s.depends_mode === "any" ? " or " : ", ");
};

export function parseAfter(text, steps, selfId) {
  const ids = [];
  for (const m of String(text || "").matchAll(/\d+/g)) {
    const s = steps[Number(m[0]) - 1];
    if (s && s.id !== selfId && !ids.includes(s.id)) ids.push(s.id);
  }
  return { depends_on: ids, depends_mode: /\bor\b/i.test(text) && ids.length > 1 ? "any" : "all" };
}

export function nextId(steps) {
  const taken = new Set(steps.map((s) => s.id));
  let k = steps.length + 1;
  while (taken.has(`s${k}`)) k++;
  return `s${k}`;
}

// Whether making `to` wait on `from` would close a loop.
export function wouldLoop(steps, from, to) {
  if (from === to) return true;
  const deps = Object.fromEntries(steps.map((s) => [s.id, s.depends_on || []]));
  const seen = new Set();
  const stack = [from];
  while (stack.length) {
    const id = stack.pop();
    if (id === to) return true;
    if (seen.has(id)) continue;
    seen.add(id);
    stack.push(...(deps[id] || []));
  }
  return false;
}

// Reorder so every step comes after the steps it waits on, keeping the
// current order wherever the links allow, so numbers still read top to bottom.
export function topoOrder(steps) {
  const left = new Map(steps.map((s) => [s.id, new Set((s.depends_on || []).filter((d) => steps.some((x) => x.id === d)))]));
  const out = [];
  const rest = [...steps];
  while (rest.length) {
    const i = rest.findIndex((s) => left.get(s.id).size === 0);
    if (i < 0) return [...out, ...rest]; // a loop: leave the rest as it was
    const [s] = rest.splice(i, 1);
    out.push(s);
    for (const set of left.values()) set.delete(s.id);
  }
  return out;
}

function field(s, key, num, actors) {
  if (key === "after") return afterText(s, num);
  if (key === "actor") return actors?.[s.actor]?.label || s.actor || "";
  return String(s[key] || "").trim();
}

// What changed from base to steps, by id: the same shape as the server's diff.
export function diffSteps(base, steps, actors) {
  const bNum = numbers(base), nNum = numbers(steps);
  const byId = Object.fromEntries(base.map((s) => [s.id, s]));
  const added = [], changed = [];
  for (const s of steps) {
    const old = byId[s.id];
    if (!old) { added.push({ id: s.id, n: nNum[s.id], title: s.title, actor: s.actor }); continue; }
    const fields = DIFF_FIELDS
      .map(([key, label]) => ({ field: key, label, before: field(old, key, bNum, actors), after: field(s, key, nNum, actors) }))
      .filter((f) => f.before !== f.after);
    if (fields.length) changed.push({ id: s.id, n: nNum[s.id], title: s.title, fields });
  }
  const removed = base.filter((s) => !nNum[s.id]).map((s) => ({ ...s, n: bNum[s.id] }));
  return { added, changed, removed, count: added.length + changed.length + removed.length };
}

// The canvas's view of a diff: a mark per step id, and the removed steps.
export function marksFrom(changes) {
  if (!changes) return { marks: {}, removed: [] };
  const marks = {};
  for (const a of changes.added || []) marks[a.id] = { kind: "new" };
  for (const c of changes.changed || []) marks[c.id] = { kind: "changed", fields: c.fields };
  return { marks, removed: changes.removed || [] };
}

export function changeSummary(changes) {
  if (!changes || !changes.count) return "No changes";
  const parts = [];
  if (changes.added?.length) parts.push(`${changes.added.length} added`);
  if (changes.changed?.length) parts.push(`${changes.changed.length} changed`);
  if (changes.removed?.length) parts.push(`${changes.removed.length} removed`);
  return parts.join(" · ");
}

// Weeks as words: "3–8 wk", "4–13 mo", "1.6–4.8 yr".
export function weeksRange(lo, hi) {
  if (lo == null || hi == null) return null;
  if (hi < 10) return `${Math.max(1, Math.round(lo))}–${Math.max(1, Math.round(hi))} wk`.replace(/^(\d+)–\1 /, "$1 ");
  const mo = (w) => w / 4.345;
  if (mo(hi) > 36) return `${(lo / 52).toFixed(1)}–${(hi / 52).toFixed(1)} yr`;
  const a = Math.max(1, Math.round(mo(lo))), b = Math.max(1, Math.round(mo(hi)));
  return a === b ? `${a} mo` : `${a}–${b} mo`;
}

export function estimateText(est) {
  if (!est) return null;
  return weeksRange(est.fastest_weeks, est.slowest_weeks);
}
