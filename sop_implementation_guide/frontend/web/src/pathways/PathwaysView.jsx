import { useEffect, useMemo, useRef, useState } from "react";
import { call } from "../api.js";
import PlanCanvas, { showStep } from "../blocks/plan/PlanCanvas.jsx";
import StepInspector from "../blocks/plan/StepInspector.jsx";
import { ACTORS } from "../blocks/plan/actors.jsx";
import { numbers } from "../blocks/plan/graph.js";
import { useStepEdits } from "../blocks/plan/useStepEdits.js";
import AssistantPanel, { EMPTY_CHAT } from "./AssistantPanel.jsx";
import NewPlaybookDialog from "./NewPlaybookDialog.jsx";
import RecTypes from "./RecTypes.jsx";
import { FLOW_PROMPTS, StepToolbar, TextSuggestion, ValueChip } from "./StepAI.jsx";
import {
  applyOps, canvasMarks, dependents, describe, findGaps, pendingOps, similarSteps, suggestionOps, withRequired,
} from "./suggestions.js";

// Edit the playbooks every implementation plan starts from: each playbook's
// steps on a canvas (click a step to edit it in the panel beside it; drag
// between steps to link them), and which recommendation types belong to it. A
// save adds a new version; older versions stay restorable. Plans nobody has
// approved follow the new version at once; approved plans keep their own steps.
//
// The assistant works alongside: the chat in the panel (Ctrl J), Ask AI on a
// selected step, and New playbook > Draft with AI. Everything it does arrives
// as separate suggestions (suggestions.js) drawn on the canvas: accept or
// reject each on its card, in the panel, or all at once. Accepted suggestions
// join the draft, which is saved as usual. The chat and the pending
// suggestions are kept in this browser.

const NEW_STEP = { title: "", how: "", actor: "city", owner: "", time: "", depends_on: [], filled_by: "template" };
const NO_STEPS = [];

const REVIEWER_KEY = "sop.pathways.reviewer";
const CHAT_KEY = "sop.playbooks.agent.chat";
const SUGG_KEY = "sop.playbooks.assistant.suggestions";
const OLD_PROPOSAL_KEY = "sop.playbooks.agent.proposal";

const editable = (d) => ({
  status: d.status || "",
  pathways: d.pathways.map((p) => ({ ...p, steps: p.steps.map((s) => ({ ...s })) })),
  rec_types: { ...d.rec_types },
});

function slug(name, taken) {
  const base = name.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "pathway";
  let key = base;
  for (let n = 2; taken.includes(key); n++) key = `${base}_${n}`;
  return key;
}

function readStored(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v ? JSON.parse(v) : fallback;
  } catch {
    return fallback;
  }
}

function store(key, value) {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch { /* a per-viewer convenience only */ }
}

// The pending suggestions on one step, in its panel: accept or reject each.
function StepSuggestions({ ops, doc, onAccept, onReject }) {
  if (!ops.length) return null;
  return (
    <div className="si-sugg">
      {ops.map((op) => {
        const d = describe(op, doc);
        return (
          <div key={op.id} className="si-sugg-row">
            <div>
              <b>✦ {d.tag}</b> {op.kind === "edit_step"
                ? op.fields.map((f) => <div key={f.field} className="si-sugg-f">{f.label}: <del>{f.before || "—"}</del> → <ins>{f.after || "—"}</ins></div>)
                : <span>{d.title}</span>}
              {op.reason && <div className="sg-why">{op.reason}</div>}
            </div>
            <span className="si-sugg-act">
              <button type="button" className="primary" onClick={() => onAccept(op)}>Accept</button>
              <button type="button" onClick={() => onReject(op)}>Reject</button>
            </span>
          </div>
        );
      })}
    </div>
  );
}

export default function PathwaysView() {
  const [data, setData] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [draft, setDraft] = useState(null);
  const [selectedKey, setSelectedKey] = useState(null);
  // Plain text, not JSON: the implementation plan reads the same name.
  const [reviewer, setReviewer] = useState(() => { try { return localStorage.getItem(REVIEWER_KEY) || ""; } catch { return ""; } });
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [saved, setSaved] = useState(null);
  const [stepError, setStepError] = useState(null);
  const [panel, setPanel] = useState("step");
  const [chat, setChat] = useState(() => readStored(CHAT_KEY, EMPTY_CHAT));
  const [mode, setMode] = useState("propose");
  const [agentBusy, setAgentBusy] = useState(false);
  // The assistant's suggestions: {base, target, reasons, rejected}.
  const [sugg, setSugg] = useState(() => readStored(SUGG_KEY, null));
  // A quick action's answer for one step: {stepId, action, values, busy}.
  const [quick, setQuick] = useState(null);
  const [multi, setMulti] = useState(() => new Set());
  const [multiAsk, setMultiAsk] = useState("");
  const [dialog, setDialog] = useState(false);
  const [focus, setFocus] = useState(null);
  // The last Check fit of a playbook's recommendation types: {key, busy, checked, moves, error}.
  const [fit, setFit] = useState(null);
  const assistant = useRef(null);

  const stepEdits = useStepEdits(
    draft?.pathways.find((p) => p.key === selectedKey)?.steps || NO_STEPS,
    (fn) => {
      setSaved(null);
      setDraft((d) => {
        const e = editable(d);
        return { ...e, pathways: e.pathways.map((p) => (p.key === selectedKey ? { ...p, steps: fn(p.steps) } : p)) };
      });
    },
    { onError: setStepError, markEdited: false },
  );

  // Another playbook (or a reload) starts with no step open.
  const { setSelected: setSelectedStep } = stepEdits;
  useEffect(() => {
    setSelectedStep(null);
    setStepError(null);
    setQuick(null);
    setMulti(new Set());
  }, [selectedKey, data, setSelectedStep]);

  useEffect(() => { try { localStorage.setItem(REVIEWER_KEY, reviewer); } catch { /* a per-viewer convenience only */ } }, [reviewer]);
  useEffect(() => store(CHAT_KEY, chat), [chat]);
  useEffect(() => store(SUGG_KEY, sugg), [sugg]);
  useEffect(() => store(OLD_PROPOSAL_KEY, null), []);

  // Ctrl J (⌘J) opens the assistant.
  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "j") {
        e.preventDefault();
        setPanel("agent");
        setTimeout(() => assistant.current?.focus(), 0);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const ops = useMemo(() => (sugg ? suggestionOps(sugg.base, sugg.target, sugg.reasons) : []), [sugg]);
  const pending = useMemo(() => (draft ? pendingOps(ops, draft, new Set(sugg?.rejected || [])) : []), [ops, draft, sugg]);
  // What the canvas draws: the draft with every pending suggestion in it.
  const preview = useMemo(() => (draft ? applyOps(draft, pending) : null), [draft, pending]);

  const keys = (preview?.pathways || []).map((p) => p.key).join("|");
  useEffect(() => {
    const k = keys ? keys.split("|") : [];
    if (k.length && !k.includes(selectedKey)) setSelectedKey(k[0]);
  }, [keys, selectedKey]);

  // "Show on canvas": select the step once its playbook is on screen, and scroll to it.
  useEffect(() => {
    if (!focus || focus.key !== selectedKey) return;
    setSelectedStep(focus.id);
    setPanel("step");
    showStep(focus.id);
    setFocus(null);
  }, [focus, selectedKey, setSelectedStep]);

  function apply(d) {
    setData(d);
    setDraft(editable(d));
    setSelectedKey((k) => (d.pathways.some((p) => p.key === k) ? k : d.pathways[0]?.key ?? null));
  }

  function load() {
    setLoadError(null);
    call("GET", "/pathways").then(apply).catch((e) => setLoadError(e.message));
  }

  useEffect(load, []);

  if (loadError) return <main className="view"><div className="pathways-wrap"><div className="banner err">Could not load playbooks — {loadError}</div></div></main>;
  if (!data || !draft) return <main className="view"><div className="pathways-wrap"><div className="card spin">Loading playbooks…</div></div></main>;

  const dirty = JSON.stringify(draft) !== JSON.stringify(editable(data));
  const pathway = preview.pathways.find((p) => p.key === selectedKey) || null;
  const draftPathway = draft.pathways.find((p) => p.key === selectedKey) || null;
  const { marks, removed, edges } = pathway ? canvasMarks(pending, selectedKey, pathway) : { marks: {}, removed: [], edges: {} };
  const num = numbers(pathway?.steps || []);
  const selId = stepEdits.selected;
  const selPreview = pathway?.steps.find((s) => s.id === selId) || removed.find((s) => s.id === selId) || null;
  const selDraft = draftPathway?.steps.find((s) => s.id === selId) || null;
  const selOps = pending.filter((o) => o.playbook === selectedKey && (o.step_id === selId || o.to === selId));
  const recsFor = (key) => Object.keys(preview.rec_types).filter((l) => preview.rec_types[l] === key).sort();
  // Types in the data with no pathway, plus any this edit has just unmapped.
  const allLabels = [...new Set([...data.unmapped_rec_labels, ...Object.keys(data.rec_types), ...Object.keys(draft.rec_types)])].sort();
  const unmapped = allLabels.filter((l) => !draft.rec_types[l]);
  // A suggested move of a recommendation type counts for the playbook it leaves too.
  const pendingFor = (key) => pending.filter((o) => o.playbook === key || o.from === key);
  const here = pendingFor(selectedKey);
  const gaps = findGaps(pathway);

  function edit(fn) {
    setSaved(null);
    setDraft((d) => fn(editable(d)));
  }

  function changePathway(field, value) {
    edit((d) => ({ ...d, pathways: d.pathways.map((p) => (p.key === selectedKey ? { ...p, [field]: value } : p)) }));
  }

  function mapRec(label, key) {
    edit((d) => {
      const rec_types = { ...d.rec_types };
      if (key) rec_types[label] = key;
      else delete rec_types[label];
      return { ...d, rec_types };
    });
  }

  function addBlank(name) {
    const key = slug(name, preview.pathways.map((p) => p.key));
    edit((d) => ({ ...d, pathways: [...d.pathways, { key, name, who: "", branch: "", steps: [{ ...NEW_STEP, id: "s1" }] }] }));
    setSelectedKey(key);
    setDialog(false);
  }

  function addCopy(fromKey, name) {
    const src = draft.pathways.find((p) => p.key === fromKey);
    if (!src) return;
    const key = slug(name, preview.pathways.map((p) => p.key));
    edit((d) => ({ ...d, pathways: [...d.pathways, { ...src, key, name, steps: src.steps.map((s) => ({ ...s })) }] }));
    setSelectedKey(key);
    setDialog(false);
  }

  function removePathway() {
    const n = recsFor(selectedKey).length;
    const msg = `Delete the "${pathway.name}" playbook?` + (n ? ` Its ${n} recommendation type(s) will become unmapped.` : "");
    if (!window.confirm(msg)) return;
    edit((d) => ({
      ...d,
      pathways: d.pathways.filter((p) => p.key !== selectedKey),
      rec_types: Object.fromEntries(Object.entries(d.rec_types).filter(([, k]) => k !== selectedKey)),
    }));
  }

  // ── suggestions ──
  function addSuggestions(target, reasons, base = draft) {
    setSugg((s) => ({ base, target, reasons: { ...(s?.reasons || {}), ...reasons }, rejected: [] }));
    const touched = [...new Set(suggestionOps(base, target, {}).flatMap((o) => [o.playbook, o.from]).filter(Boolean))];
    if (touched.length && !touched.includes(selectedKey)) setSelectedKey(touched[0]);
  }

  function accept(op) {
    const list = withRequired(op, pending);
    setSaved(null);
    setDraft((d) => applyOps(d, list));
  }

  function reject(op) {
    const deps = dependents(op, pending);
    if (deps.length > 1 && !window.confirm(`Rejecting this also rejects ${deps.length - 1} other suggestion(s) that need it. Continue?`)) return;
    setSugg((s) => ({ ...s, rejected: [...(s?.rejected || []), ...deps.map((d) => d.id)] }));
  }

  function acceptAll(list) {
    const all = [...new Map(list.flatMap((o) => withRequired(o, pending)).map((o) => [o.id, o])).values()];
    setSaved(null);
    setDraft((d) => applyOps(d, all));
  }

  function discardAll(list) {
    const ids = new Set(list.flatMap((o) => dependents(o, pending)).map((o) => o.id));
    setSugg((s) => ({ ...s, rejected: [...(s?.rejected || []), ...ids] }));
  }

  function show(op) {
    if (op.kind === "map_rec") {
      // The type's row, under the canvas of the playbook it is in now.
      setSelectedKey(op.from && preview.pathways.some((p) => p.key === op.from) ? op.from : op.to);
      setTimeout(() => document.querySelector(`[data-rec="${CSS.escape(op.label)}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" }), 50);
      return;
    }
    const id = op.step_id || op.to || null;
    setSelectedKey(op.playbook);
    if (id) setFocus({ key: op.playbook, id });
  }

  function decide(stepId, yes) {
    const list = here.filter((o) => o.step_id === stepId || o.to === stepId);
    if (yes) acceptAll(list);
    else discardAll(list);
  }

  // Check fit: the assistant's moves come back as suggestions like any other.
  async function checkFit() {
    const key = selectedKey;
    setFit({ key, busy: true });
    try {
      const r = await call("POST", "/pathways/fit", { working: preview, playbook: key });
      const target = { ...preview, rec_types: { ...preview.rec_types } };
      const reasons = {};
      for (const t of r.types) {
        if (t.fits) continue;
        target.rec_types[t.label] = t.move_to;
        reasons[`rec:${t.label}`] = t.reason;
      }
      for (const a of r.add) {
        target.rec_types[a.label] = key;
        reasons[`rec:${a.label}`] = a.reason;
      }
      const moves = Object.keys(reasons).length;
      if (moves) addSuggestions(target, reasons);
      setFit({ key, busy: false, checked: r.types.length, moves });
    } catch (e) {
      setFit({ key, busy: false, error: e.message });
    }
  }

  // ── the assistant ──
  function ask(text, asMode = "propose") {
    setPanel("agent");
    setTimeout(() => assistant.current?.send(text, asMode), 0);
  }

  async function runQuick(action, id, variant) {
    const prev = quick?.stepId === id && quick.action === action ? quick : null;
    const previous = variant && prev ? JSON.stringify(prev.values) : null;
    setQuick({ stepId: id, action, values: prev?.values || null, busy: true });
    setPanel("step");
    stepEdits.setSelected(id);
    try {
      const r = await call("POST", "/pathways/assist", {
        action, working: preview, playbook: selectedKey, step_id: id, variant: variant || null, previous,
      });
      setQuick({ stepId: id, action, values: r, busy: false });
    } catch (e) {
      setQuick(null);
      setStepError(e.message);
    }
  }

  function onSelect(id, e) {
    if (e?.shiftKey && id) {
      setMulti((m) => {
        const next = new Set(m.size ? m : selId ? [selId] : []);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
      return;
    }
    setMulti(new Set());
    stepEdits.setSelected(id);
    if (id) setPanel("step");
    if (quick && quick.stepId !== id) setQuick(null);
  }

  const toolbar = (id) => {
    const s = draftPathway?.steps.find((x) => x.id === id);
    if (!s || multi.size > 1) return null;
    const n = num[id];
    return (
      <StepToolbar
        onEdit={() => setPanel("step")}
        onDuplicate={() => stepEdits.duplicate(id)}
        onDelete={() => stepEdits.remove(id)}
        onQuick={(k) => runQuick(k, id)}
        onFlow={(k) => ask(FLOW_PROMPTS[k](n, s.title))}
        onAsk={(t) => ask(`@step ${n} (${s.title}): ${t}`)}
      />
    );
  };

  // ── the step panel's inline suggestions ──
  const slots = {};
  if (selDraft && quick?.stepId === selDraft.id) {
    const v = quick.values || {};
    if (quick.action === "rewrite" && (v.how || quick.busy)) {
      slots.how = (
        <TextSuggestion before={selDraft.how} after={v.how} note={v.note} busy={quick.busy}
          onKeep={() => { stepEdits.patch(selDraft.id, { how: v.how }); setQuick(null); }}
          onDiscard={() => setQuick(null)}
          onVariant={(variant) => runQuick("rewrite", selDraft.id, variant)} />
      );
    }
    if (quick.action === "estimate") {
      slots.time = quick.busy ? <span className="ai-note"><span className="pl-spin" /> Estimating…</span>
        : <ValueChip text={v.time} note={v.note} onUse={() => { stepEdits.patch(selDraft.id, { time: v.time }); setQuick(null); }} onDismiss={() => setQuick(null)} />;
    }
    if (quick.action === "owners") {
      if (quick.busy) slots.owner = <span className="ai-note"><span className="pl-spin" /> Suggesting owners…</span>;
      else {
        slots.owner = <ValueChip text={`${ACTORS[v.actor]?.label || v.actor} · ${v.owner}`} note={v.note}
                                 onUse={() => stepEdits.patch(selDraft.id, { actor: v.actor, owner: v.owner })} />;
        if (v.secondary_owner) {
          slots.secondary = <ValueChip text={`${v.secondary_owner}?`} onUse={() => { stepEdits.patch(selDraft.id, { secondary_owner: v.secondary_owner }); setQuick(null); }}
                                       onDismiss={() => setQuick(null)} />;
        }
      }
    }
  }
  if (selDraft && !slots.time) {
    const like = similarSteps(preview, selectedKey, selDraft);
    if (like.length) {
      const times = [...new Set(like.map((l) => l.step.time))];
      slots.time = (
        <span className="ai-note" title={like.map((l) => `${l.playbook}: ${l.step.title} (${l.step.time})`).join("\n")}>
          Similar steps in {like.length} other playbook{like.length > 1 ? "s" : ""} take {times.slice(0, 2).join(" or ")}.
        </span>
      );
    }
  }
  if (selOps.length) {
    slots.top = <StepSuggestions ops={selOps} doc={preview} onAccept={accept} onReject={reject} />;
  }

  // ── saving ──
  async function run(fn, done) {
    setSaving(true);
    setSaveError(null);
    setSaved(null);
    try {
      apply(await fn());
      setSaved(done);
      setNote("");
    } catch (e) {
      setSaveError(e.message);
    } finally {
      setSaving(false);
    }
  }

  const save = () => run(
    () => call("PUT", "/pathways", { ...draft, updated_by: reviewer, note, base_version: data.version }),
    "Saved. Implementation plans nobody has saved yet now follow this version.",
  );

  const restore = (v) => {
    const label = v === 0 ? "the original file (v0)" : `v${v}`;
    if (!window.confirm(`Make ${label} the current template? This saves it as a new version; nothing is deleted.`)) return;
    run(() => call("POST", `/pathways/versions/${v}/restore`, { updated_by: reviewer }), `Restored ${label}.`);
  };

  const canSave = data.storage_ready && reviewer.trim() && !saving;
  const isSuggested = (id) => marks[id]?.kind === "suggested" || marks[id]?.kind === "remove";

  return (
    <main className="view">
      <div className="pathways-wrap">
        <div className="pathways-top">
          <div>
            <h3>Playbooks</h3>
            <div className="sub">
              The templates every implementation plan starts from, one playbook per kind of recommendation. {data.version}
              {data.source === "app" ? `, saved by ${data.updated_by} on ${data.updated}` : ", from backend/data/pathways.json"}.
            </div>
          </div>
          <span className="spacer" />
          <button type="button" className="ask-agent" onClick={() => { setPanel("agent"); setTimeout(() => assistant.current?.focus(), 0); }}>
            ✦ Assistant{agentBusy ? <span className="pl-spin" /> : <kbd>Ctrl J</kbd>}
          </button>
        </div>

        {!data.storage_ready && (
          <div className="banner warn">
            Saving is off until the <code>pathway_templates</code> table exists. Run the PATHWAY TEMPLATES section
            of <code>backend/db/schema.sql</code> in the Supabase SQL editor. You can still view and try edits.
          </div>
        )}
        {unmapped.length > 0 && (
          <div className="banner warn">
            {unmapped.length} recommendation type(s) have no playbook, so they get no implementation plan: {unmapped.join(", ")}.
            Add them to a playbook below.
          </div>
        )}

        <div className="pathways-grid">
          <aside className="card pathway-list">
            <h4>Playbooks</h4>
            <ul>
              {preview.pathways.map((p) => {
                const n = pendingFor(p.key).length;
                const isNew = !draft.pathways.some((x) => x.key === p.key);
                return (
                  <li key={p.key} aria-selected={p.key === selectedKey} onClick={() => setSelectedKey(p.key)}>
                    <div className="name">
                      {p.name || <i>Unnamed</i>}
                      {isNew && <span className="pc-flag sugg">✦ Suggested</span>}
                    </div>
                    <div className="meta">
                      {p.steps.length} steps · {recsFor(p.key).length} rec types
                      {n > 0 && <span className="pb-pending">{n} pending</span>}
                    </div>
                  </li>
                );
              })}
            </ul>
            <div className="pathway-new">
              <button type="button" onClick={() => setDialog(true)}>+ New</button>
              <button type="button" className="ai" onClick={() => setDialog(true)}>✦ Draft with AI</button>
            </div>

            <h4 className="history-head">History</h4>
            <ul className="pathway-history">
              {(dirty || pending.length > 0) && (
                <li className="current">
                  <b>Draft</b>
                  {pending.length > 0 ? ` · ${pending.length} suggestion${pending.length > 1 ? "s" : ""} pending` : " · unsaved changes"}
                </li>
              )}
              {data.history.map((h, i) => (
                <li key={h.version}>
                  <b>v{h.version}</b> · {h.updated_by} · {(h.created_at || "").slice(0, 10)}
                  {h.note && <div className="assess">{h.note}</div>}
                  {i > 0 && <button onClick={() => restore(h.version)} disabled={!canSave || dirty}>Restore</button>}
                </li>
              ))}
              <li>
                <b>v0</b> · original file
                {data.source === "app" && <button onClick={() => restore(0)} disabled={!canSave || dirty}>Restore</button>}
              </li>
            </ul>
          </aside>

          <div>
            {!pathway ? (
              <div className="card empty">No playbook selected.</div>
            ) : (
              <div className="card">
                <h4>Playbook <code className="assess">{pathway.key}</code></h4>
                {draftPathway ? (
                  <div className="pathway-fields">
                    <label>Name<input value={draftPathway.name} onChange={(e) => changePathway("name", e.target.value)} /></label>
                    <label>Who does the work<input value={draftPathway.who} onChange={(e) => changePathway("who", e.target.value)} /></label>
                    <label className="wide">
                      The deciding question
                      <textarea rows={2} value={draftPathway.branch} onChange={(e) => changePathway("branch", e.target.value)} />
                    </label>
                  </div>
                ) : (
                  <div className="banner sugg-banner">
                    ✦ This whole playbook is the assistant's suggestion: <b>{pathway.name}</b>. Accept its steps on the
                    canvas one by one, or all at once below.
                  </div>
                )}

                <div className="plan-canvas-wrap side editing">
                  <PlanCanvas
                    steps={pathway.steps} marks={marks} removed={removed} edgeMarks={edges}
                    selectedId={selId} onSelect={onSelect} multi={multi}
                    editable onConnect={stepEdits.connect} onDisconnect={stepEdits.disconnect}
                    onDecide={decide} toolbar={toolbar}
                  />
                  <div className="canvas-tools">
                    <button type="button" onClick={() => stepEdits.addAfter(null)} disabled={!draftPathway}>+ Add step</button>
                    <button type="button" className="ai" onClick={() => ask("Find gaps in this playbook: missing steps, owners or durations, and branches that lead nowhere. Suggest fixes.")}>
                      ✦ Find gaps
                    </button>
                  </div>
                  {multi.size > 1 && (
                    <form className="canvas-bar multi" onSubmit={(e) => {
                      e.preventDefault();
                      const list = [...multi].map((id) => `@step ${num[id]} (${pathway.steps.find((s) => s.id === id)?.title})`).join(", ");
                      ask(`${list}: ${multiAsk.trim() || "Review these steps together and suggest improvements."}`);
                      setMultiAsk("");
                      setMulti(new Set());
                    }}>
                      <span>{multi.size} steps selected</span>
                      <input value={multiAsk} onChange={(e) => setMultiAsk(e.target.value)} placeholder="✦ Ask AI about them…" />
                      <button type="submit" className="primary">Ask</button>
                      <button type="button" className="link" onClick={() => setMulti(new Set())}>Clear</button>
                    </form>
                  )}
                  {multi.size <= 1 && here.length > 0 && (
                    <div className="canvas-bar">
                      <span>✦ {here.length} suggested change{here.length > 1 ? "s" : ""} from the assistant</span>
                      <button type="button" onClick={() => discardAll(here)}>Discard all</button>
                      <button type="button" className="primary" onClick={() => acceptAll(here)}>Accept all</button>
                    </div>
                  )}
                  <div className="side-panel">
                    <div className="side-tabs" role="tablist">
                      <button type="button" role="tab" aria-selected={panel === "step"} onClick={() => setPanel("step")}>Step</button>
                      <button type="button" role="tab" aria-selected={panel === "agent"} onClick={() => setPanel("agent")}>
                        ✦ Assistant{agentBusy && <span className="pl-spin" />}
                        {pending.length > 0 && <span className="pb-pending">{pending.length}</span>}
                      </button>
                    </div>
                    {panel === "step" && (selPreview ? (
                      <StepInspector
                        s={selDraft || selPreview}
                        n={isSuggested(selPreview.id) && !selDraft ? "✦ suggested" : num[selPreview.id] || selPreview.n}
                        steps={selDraft ? draftPathway.steps : pathway.steps}
                        removed={!pathway.steps.includes(selPreview)}
                        editable={!!selDraft && !isSuggested(selPreview.id)}
                        sources={data.filled_by} slots={slots}
                        onChange={(fields) => stepEdits.patch(selPreview.id, fields)}
                        onDelete={() => stepEdits.remove(selPreview.id)}
                        onAddAfter={() => stepEdits.addAfter(selPreview.id)}
                        onSelect={(id) => onSelect(id)}
                        onClose={() => stepEdits.setSelected(null)}
                      />
                    ) : (
                      <div className="si-empty">
                        <b>Click a step to edit it</b>
                        <p className="assess">
                          Each step has an activity, a description, what it depends on, a primary and a secondary owner,
                          and a potential duration.
                        </p>
                        <p className="assess">
                          Drag from the dot under a step to the step that waits on it; click a line to remove the link.
                          Shift-click several steps to ask the assistant about them together.
                        </p>
                        <button type="button" onClick={() => stepEdits.addAfter(null)} disabled={!draftPathway}>+ Add step</button>
                      </div>
                    ))}
                    <AssistantPanel
                      ref={assistant} hidden={panel !== "agent"} chat={chat} setChat={setChat} mode={mode} setMode={setMode}
                      draft={draft} working={preview} playbook={pathway} version={data.version}
                      others={preview.pathways.filter((p) => p.key !== selectedKey)}
                      pending={pending} gaps={gaps}
                      onResult={(out, sentDraft) => addSuggestions(out.target, out.reasons || {}, sentDraft)}
                      onAccept={accept} onReject={reject} onShow={show}
                      onAcceptAll={() => acceptAll(pending)} onDiscardAll={() => discardAll(pending)}
                      onBusy={setAgentBusy}
                    />
                  </div>
                </div>
                {stepError && <div className="banner err" style={{ marginTop: ".6rem" }}>{stepError}</div>}

                <div className="btn-row">
                  <button onClick={() => stepEdits.addAfter(null)} disabled={!draftPathway}>+ Add step</button>
                  <span className="spacer" />
                  <button onClick={removePathway} disabled={!draftPathway}>Delete playbook</button>
                </div>

                <RecTypes
                  pathway={pathway} draft={draft} preview={preview} pending={pending} unmapped={unmapped}
                  fit={fit} onCheck={checkFit} onMap={mapRec} onAccept={accept} onReject={reject}
                />
              </div>
            )}

            <div className="card">
              <h4>Save</h4>
              <div className="plan-edit-row">
                <label className="grow">
                  Template status, shown on every implementation plan
                  <input value={draft.status} onChange={(e) => edit((d) => ({ ...d, status: e.target.value }))} />
                </label>
              </div>
              <div className="plan-edit-row">
                <label>
                  Your name
                  <input value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="Required to save" />
                </label>
                <label className="grow">
                  What changed
                  <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. SME timelines for parks" />
                </label>
              </div>
              {pending.length > 0 && (
                <div className="assess" style={{ marginTop: ".5rem" }}>
                  Save keeps what you accepted. The {pending.length} pending suggestion{pending.length > 1 ? "s stay" : " stays"} here until you accept or reject {pending.length > 1 ? "them" : "it"}.
                </div>
              )}
              {saveError && <div className="banner err" style={{ marginTop: ".75rem" }}>Save failed — {saveError}</div>}
              {saved && <div className="banner ok" style={{ marginTop: ".75rem" }}>{saved}</div>}
              <div className="btn-row">
                <button className="primary" onClick={save} disabled={!canSave || !dirty}>
                  {saving ? "Saving…" : `Save as v${data.version_number + 1}`}
                </button>
                <button onClick={() => { setDraft(editable(data)); setSaveError(null); }} disabled={!dirty || saving}>
                  Discard changes
                </button>
                {saveError?.startsWith("409") && <button onClick={load}>Reload</button>}
              </div>
            </div>
          </div>
        </div>
      </div>

      {dialog && (
        <NewPlaybookDialog
          working={preview} labels={allLabels} onClose={() => setDialog(false)}
          onBlank={addBlank} onCopy={addCopy}
          onDrafted={(result, recTypes) => {
            const p = result.playbook;
            const target = applyOps(draft, pending);
            target.pathways = [...target.pathways.filter((x) => x.key !== p.key), p];
            target.rec_types = { ...target.rec_types };
            for (const l of recTypes) target.rec_types[l] = p.key;
            const reasons = { [`playbook:${p.key}`]: "Drafted by the assistant" };
            for (const s of p.steps) if (s.source) reasons[`step:${p.key}:${s.id}`] = `From ${s.source}`;
            for (const l of recTypes) reasons[`rec:${l}`] = "Chosen when drafting the playbook";
            addSuggestions(target, reasons);
            setSelectedKey(p.key);
            setDialog(false);
          }}
        />
      )}
    </main>
  );
}
