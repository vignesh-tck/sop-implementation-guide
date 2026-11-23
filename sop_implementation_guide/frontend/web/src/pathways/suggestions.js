// The assistant's suggestions, one decision each.
//
// Whatever the assistant does (a chat turn, a drafted playbook) comes back as
// a target: the playbooks as it would have them. The difference between the
// base (the planner's draft when it was asked) and the target is split into
// small operations: add this step, change these fields of that step, link
// these two steps, move this recommendation type. Each has a stable id, so the
// planner's accept / reject decisions and the assistant's reasons survive the
// next turn.
//
// An operation is pending while the draft doesn't already reflect it and the
// planner hasn't rejected it. Accepting applies it to the draft (with any
// operation it needs first, such as the new step a link points at); the
// preview the canvas draws is the draft with every pending operation applied.

import { nextId, numbers, topoOrder, wouldLoop } from "../blocks/plan/graph.js";

export const STEP_FIELDS = [
  ["title", "Activity"], ["how", "Description"], ["condition", "Only if"], ["depends_mode", "Waits for"],
  ["actor", "Primary owner"], ["owner", "Primary owner, exactly"], ["secondary_owner", "Secondary owner"],
  ["time", "Duration"], ["filled_by", "Filled in by"], ["source", "Source"],
];
const PLAYBOOK_FIELDS = [["name", "Name"], ["who", "Who does the work"], ["branch", "Deciding question"]];

const val = (v) => (v == null ? "" : String(v));
const mode = (s) => (s?.depends_mode === "any" && (s.depends_on || []).length > 1 ? "any" : "all");
const fieldVal = (s, f) => (f === "depends_mode" ? mode(s) : val(s?.[f]));
const byKey = (doc) => Object.fromEntries((doc?.pathways || []).map((p) => [p.key, p]));
const stepOf = (p, id) => p?.steps.find((s) => s.id === id);

// The operations from base to target, in the order they can be applied.
export function suggestionOps(base, target, reasons = {}) {
  if (!base || !target) return [];
  const B = byKey(base), T = byKey(target);
  const why = (...keys) => keys.map((k) => reasons[k]).find(Boolean) || null;
  const ops = [];
  const newPlaybooks = new Set(Object.keys(T).filter((k) => !B[k]));

  for (const [key, t] of Object.entries(T)) {
    const b = B[key];
    if (!b) {
      ops.push({ id: `pb+:${key}`, kind: "add_playbook", playbook: key, name: t.name,
                 data: { key, name: t.name, who: t.who || "", branch: t.branch || "" }, reason: why(`playbook:${key}`) });
    } else {
      const fields = PLAYBOOK_FIELDS.filter(([f]) => val(b[f]) !== val(t[f]))
        .map(([f, label]) => ({ field: f, label, before: val(b[f]), after: val(t[f]) }));
      if (fields.length) ops.push({ id: `pb~:${key}`, kind: "edit_playbook", playbook: key, name: t.name, fields, reason: why(`playbook:${key}`) });
    }
    const bSteps = b?.steps || [];
    const bIds = new Set(bSteps.map((s) => s.id));
    const newIds = new Set(t.steps.filter((s) => !bIds.has(s.id)).map((s) => s.id));
    t.steps.forEach((s, i) => {
      const old = stepOf(b, s.id);
      const requires = [...(newPlaybooks.has(key) ? [`pb+:${key}`] : []),
                        ...(s.depends_on || []).filter((d) => newIds.has(d)).map((d) => `st+:${key}:${d}`)];
      if (!old) {
        ops.push({ id: `st+:${key}:${s.id}`, kind: "add_step", playbook: key, step_id: s.id, step: s,
                   after: i > 0 ? t.steps[i - 1].id : null, requires,
                   reason: why(`step:${key}:${s.id}`, `playbook:${key}`) });
        return;
      }
      const fields = STEP_FIELDS.filter(([f]) => fieldVal(old, f) !== fieldVal(s, f))
        .map(([f, label]) => ({ field: f, label, before: fieldVal(old, f), after: fieldVal(s, f) }));
      if (fields.length) {
        ops.push({ id: `st~:${key}:${s.id}`, kind: "edit_step", playbook: key, step_id: s.id, title: s.title, fields,
                   reason: why(`step:${key}:${s.id}`, `playbook:${key}`) });
      }
      const was = new Set(old.depends_on || []), now = new Set(s.depends_on || []);
      for (const d of now) {
        if (was.has(d)) continue;
        ops.push({ id: `ln+:${key}:${d}>${s.id}`, kind: "link", playbook: key, from: d, to: s.id,
                   requires: newIds.has(d) ? [`st+:${key}:${d}`] : [], reason: why(`step:${key}:${s.id}`, `playbook:${key}`) });
      }
      for (const d of was) {
        if (now.has(d) || !t.steps.some((x) => x.id === d)) continue;   // a removed step's links go with it
        ops.push({ id: `ln-:${key}:${d}>${s.id}`, kind: "unlink", playbook: key, from: d, to: s.id,
                   reason: why(`step:${key}:${s.id}`, `playbook:${key}`) });
      }
    });
    for (const s of bSteps) {
      if (!t.steps.some((x) => x.id === s.id)) {
        ops.push({ id: `st-:${key}:${s.id}`, kind: "remove_step", playbook: key, step_id: s.id, step: s,
                   reason: why(`step:${key}:${s.id}`, `playbook:${key}`) });
      }
    }
  }
  const labels = new Set([...Object.keys(base.rec_types || {}), ...Object.keys(target.rec_types || {})]);
  for (const label of [...labels].sort()) {
    const from = base.rec_types?.[label] || null, to = target.rec_types?.[label] || null;
    if (from === to) continue;
    ops.push({ id: `rec:${label}`, kind: "map_rec", label, from, to, playbook: to || from,
               requires: to && newPlaybooks.has(to) ? [`pb+:${to}`] : [], reason: why(`rec:${label}`) });
  }
  for (const key of Object.keys(B)) {
    if (!T[key]) ops.push({ id: `pb-:${key}`, kind: "remove_playbook", playbook: key, name: B[key].name, reason: why(`playbook:${key}`) });
  }
  return ops;
}

// The part of an operation the draft doesn't reflect yet, or null when it does.
function remaining(op, draft) {
  const D = byKey(draft);
  const p = D[op.playbook];
  switch (op.kind) {
    case "add_playbook": return p ? null : op;
    case "remove_playbook": return p ? op : null;
    case "edit_playbook": {
      if (!p) return null;
      const fields = op.fields.filter((f) => val(p[f.field]) !== f.after);
      return fields.length ? { ...op, fields } : null;
    }
    case "add_step": {
      const s = stepOf(p, op.step_id);
      return s && s.title === op.step.title ? null : op;
    }
    case "remove_step": return stepOf(p, op.step_id) ? op : null;
    case "edit_step": {
      const s = stepOf(p, op.step_id);
      if (!s && !(op.requires || []).length) return null;
      const fields = s ? op.fields.filter((f) => fieldVal(s, f.field) !== f.after) : op.fields;
      return fields.length ? { ...op, fields } : null;
    }
    case "link": {
      const s = stepOf(p, op.to);
      return s && (s.depends_on || []).includes(op.from) ? null : op;
    }
    case "unlink": {
      const s = stepOf(p, op.to);
      return s && (s.depends_on || []).includes(op.from) ? op : null;
    }
    case "map_rec": return (draft.rec_types?.[op.label] || null) === op.to ? null : op;
    default: return null;
  }
}

export function pendingOps(ops, draft, rejected) {
  const out = [];
  for (const op of ops) {
    if (rejected.has(op.id)) continue;
    const r = remaining(op, draft);
    if (r) out.push(r);
  }
  // An operation that needs one the planner rejected can't happen either.
  const ids = new Set(out.map((o) => o.id));
  return out.filter((o) => (o.requires || []).every((r) => ids.has(r) || !ops.some((x) => x.id === r) || !rejected.has(r)));
}

function clone(doc) {
  return { ...doc, rec_types: { ...doc.rec_types }, pathways: doc.pathways.map((p) => ({ ...p, steps: p.steps.map((s) => ({ ...s })) })) };
}

function applyOne(doc, op) {
  const d = clone(doc);
  const p = d.pathways.find((x) => x.key === op.playbook);
  switch (op.kind) {
    case "add_playbook":
      if (!p) d.pathways.push({ ...op.data, steps: [] });
      break;
    case "remove_playbook":
      d.pathways = d.pathways.filter((x) => x.key !== op.playbook);
      for (const [l, k] of Object.entries(d.rec_types)) if (k === op.playbook) delete d.rec_types[l];
      break;
    case "edit_playbook":
      if (p) for (const f of op.fields) p[f.field] = f.after;
      break;
    case "add_step": {
      if (!p) break;
      const ids = new Set(p.steps.map((s) => s.id));
      const step = { ...op.step, id: ids.has(op.step.id) ? nextId(p.steps) : op.step.id };
      step.depends_on = (step.depends_on || []).filter((x) => ids.has(x));
      const at = op.after ? p.steps.findIndex((s) => s.id === op.after) : -1;
      p.steps.splice(at >= 0 ? at + 1 : p.steps.length, 0, step);
      p.steps = topoOrder(p.steps);
      break;
    }
    case "remove_step": {
      if (!p) break;
      const gone = stepOf(p, op.step_id);
      if (!gone) break;
      p.steps = topoOrder(p.steps.filter((s) => s.id !== op.step_id).map((s) => (
        (s.depends_on || []).includes(op.step_id)
          ? { ...s, depends_on: [...new Set([...s.depends_on.filter((x) => x !== op.step_id), ...(gone.depends_on || [])])] }
          : s)));
      break;
    }
    case "edit_step": {
      const s = stepOf(p, op.step_id);
      if (!s) break;
      for (const f of op.fields) {
        if (f.after === "" && f.field !== "title") delete s[f.field];
        else s[f.field] = f.after;
      }
      break;
    }
    case "link": {
      const s = stepOf(p, op.to);
      if (!s || !stepOf(p, op.from) || (s.depends_on || []).includes(op.from) || wouldLoop(p.steps, op.from, op.to)) break;
      s.depends_on = [...(s.depends_on || []), op.from];
      p.steps = topoOrder(p.steps);
      break;
    }
    case "unlink": {
      const s = stepOf(p, op.to);
      if (!s) break;
      s.depends_on = (s.depends_on || []).filter((x) => x !== op.from);
      if (s.depends_on.length < 2) delete s.depends_mode;
      break;
    }
    case "map_rec":
      if (op.to) d.rec_types[op.label] = op.to;
      else delete d.rec_types[op.label];
      break;
    default:
  }
  return d;
}

const ORDER = ["add_playbook", "edit_playbook", "add_step", "edit_step", "link", "unlink", "remove_step", "map_rec", "remove_playbook"];

export function applyOps(doc, ops) {
  const sorted = [...ops].sort((a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind));
  return sorted.reduce(applyOne, doc);
}

// An operation with everything it needs first, from what is still pending.
export function withRequired(op, pending) {
  const byId = Object.fromEntries(pending.map((o) => [o.id, o]));
  const out = new Map();
  const visit = (o) => {
    if (!o || out.has(o.id)) return;
    (o.requires || []).forEach((r) => visit(byId[r]));
    out.set(o.id, o);
  };
  visit(op);
  return [...out.values()];
}

// Pending operations that need this one, so rejecting it rejects them too.
export function dependents(op, pending) {
  const out = new Set([op.id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const o of pending) {
      if (!out.has(o.id) && (o.requires || []).some((r) => out.has(r))) {
        out.add(o.id);
        grew = true;
      }
    }
  }
  return pending.filter((o) => out.has(o.id));
}

// What a step's card on the canvas shows for the pending operations on it.
export function canvasMarks(pending, playbook, preview) {
  const marks = {};
  const removed = [];
  const edges = {};
  const num = numbers(preview?.steps || []);
  for (const op of pending) {
    if (op.playbook !== playbook) continue;
    if (op.kind === "add_step") marks[op.step_id] = { ...(marks[op.step_id] || {}), kind: "suggested", ops: [...(marks[op.step_id]?.ops || []), op.id] };
    if (op.kind === "edit_step") {
      const m = marks[op.step_id] || {};
      marks[op.step_id] = { ...m, kind: m.kind || "edit", fields: op.fields, ops: [...(m.ops || []), op.id] };
    }
    if (op.kind === "remove_step") removed.push({ ...op.step, n: num[op.step_id] || "–", _op: op.id });
    if (op.kind === "link") {
      edges[`${op.from}>${op.to}`] = "suggested";
      const m = marks[op.to] || {};
      marks[op.to] = { ...m, kind: m.kind || "edit", fields: m.fields || [], ops: [...(m.ops || []), op.id] };
    }
  }
  for (const r of removed) marks[r.id] = { kind: "remove", ops: [r._op] };
  return { marks, removed, edges };
}

// One line describing an operation, for the cards in the assistant panel.
export function describe(op, doc) {
  const p = byKey(doc)[op.playbook];
  const num = numbers(p?.steps || []);
  const n = (id) => (num[id] ? `step ${num[id]}` : "a new step");
  const title = (id) => stepOf(p, id)?.title || "";
  switch (op.kind) {
    case "add_playbook": return { tag: "New playbook", title: op.name, detail: "" };
    case "remove_playbook": return { tag: "Remove playbook", title: op.name, detail: "Its recommendation types become unmapped" };
    case "edit_playbook": return { tag: "Edit playbook", title: op.name, detail: op.fields.map((f) => f.label).join(", ") };
    case "add_step": {
      const s = op.step;
      return { tag: "Add step", title: s.title,
               detail: [`New ${n(op.step_id)}`, s.owner, s.time, (s.depends_on || []).length ? `waits on ${(s.depends_on || []).map(n).join(", ")}` : null,
                        s.condition ? `only if ${s.condition.toLowerCase()}` : null].filter(Boolean).join(" · ") };
    }
    case "remove_step": return { tag: "Remove step", title: op.step.title, detail: "Steps after it wait on what it waited on" };
    case "edit_step": {
      const f = op.fields.length === 1 ? op.fields[0] : null;
      return { tag: `Edit ${n(op.step_id)}`, title: f && f.field === "time" ? `Duration ${f.before || "—"} → ${f.after || "—"}` : op.title || title(op.step_id),
               detail: f && f.field === "time" ? title(op.step_id) : op.fields.map((x) => x.label).join(", ") };
    }
    case "link": return { tag: "Link steps", title: `${n(op.from)} → ${n(op.to)}`,
                          detail: [title(op.to), stepOf(p, op.to)?.condition ? `when “${stepOf(p, op.to).condition}”` : null].filter(Boolean).join(" · ") };
    case "unlink": return { tag: "Unlink steps", title: `${n(op.from)} ✕ ${n(op.to)}`, detail: title(op.to) };
    case "map_rec": return { tag: "Move recommendation type", title: op.label,
                             detail: op.to ? `into ${byKey(doc)[op.to]?.name || op.to}` : "out of every playbook" };
    default: return { tag: op.kind, title: "", detail: "" };
  }
}

// Gaps a rule can find, no model needed: shown as "Also noticed".
export function findGaps(p) {
  if (!p) return [];
  const num = numbers(p.steps);
  const out = [];
  for (const s of p.steps) {
    if (s.actor === "agent" && !s.secondary_owner) out.push({ id: `sec:${s.id}`, step: s.id, text: `Step ${num[s.id]} is done by an agent but has no secondary owner.`, action: "Suggest one", ask: `Suggest a secondary owner for @step ${num[s.id]} (${s.title}).` });
    if (!(s.time || "").trim()) out.push({ id: `time:${s.id}`, step: s.id, text: `Step ${num[s.id]} has no duration.`, action: "Estimate", ask: `Estimate a duration for @step ${num[s.id]} (${s.title}).` });
    if (!(s.how || "").trim()) out.push({ id: `how:${s.id}`, step: s.id, text: `Step ${num[s.id]} has no description.`, action: "Write one", ask: `Write a description for @step ${num[s.id]} (${s.title}).` });
  }
  // A step waiting on all of two branches that never both happen would never start.
  const byId = Object.fromEntries(p.steps.map((s) => [s.id, s]));
  const branches = {};
  const branchOf = (id, seen = new Set()) => {
    if (branches[id]) return branches[id];
    const s = byId[id];
    if (!s || seen.has(id)) return new Set();
    seen.add(id);
    const out = new Set(s.condition ? [s.condition] : []);
    for (const d of s.depends_on || []) for (const c of branchOf(d, seen)) out.add(c);
    branches[id] = out;
    return out;
  };
  for (const s of p.steps) {
    const deps = s.depends_on || [];
    if (deps.length < 2 || s.depends_mode === "any") continue;
    const sets = deps.map((d) => [d, branchOf(d)]).filter(([, b]) => b.size);
    const clash = sets.find(([a, x], i) => sets.slice(i + 1).some(([, y]) => ![...x].some((c) => y.has(c))));
    if (clash) {
      const nums = deps.map((d) => num[d]).filter(Boolean).join(" and ");
      out.push({ id: `branches:${s.id}`, step: s.id, text: `Step ${num[s.id]} waits on all of steps ${nums}, which are on different branches.`,
                 action: "Fix", ask: `@step ${num[s.id]} (${s.title}) waits on all of steps ${nums}, but they are on different branches that don't both happen. Make it wait on whichever is first, unless both really must finish.` });
    }
  }
  const starts = p.steps.filter((s) => !(s.depends_on || []).length);
  if (starts.length > 1) {
    out.push({ id: "starts", text: `${starts.length} steps wait on nothing (steps ${starts.map((s) => num[s.id]).join(", ")}).`, action: "Check", ask: `Steps ${starts.map((s) => num[s.id]).join(", ")} wait on nothing. Check whether they should wait on an earlier step.` });
  }
  return out;
}

// Steps in other playbooks that look like this one, for the duration hint.
const STOP = new Set(["the", "a", "an", "and", "or", "of", "to", "for", "on", "in", "with", "by", "from", "at", "if", "it", "its", "each", "any"]);
const words = (t) => new Set(String(t || "").toLowerCase().split(/[^a-z]+/).filter((w) => w.length > 2 && !STOP.has(w)));

export function similarSteps(doc, playbook, step) {
  const mine = words(step?.title);
  if (!mine.size) return [];
  const out = [];
  for (const p of doc?.pathways || []) {
    if (p.key === playbook) continue;
    let best = null;
    for (const s of p.steps) {
      const theirs = words(s.title);
      const common = [...mine].filter((w) => theirs.has(w)).length;
      const score = common / new Set([...mine, ...theirs]).size;
      if (score >= 0.3 && s.time && (!best || score > best.score)) best = { score, playbook: p.name, step: s };
    }
    if (best) out.push(best);
  }
  return out;
}
