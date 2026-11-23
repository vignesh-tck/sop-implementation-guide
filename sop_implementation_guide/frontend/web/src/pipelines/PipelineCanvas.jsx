import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dagre from "@dagrejs/dagre";
import {
  Background, ConnectionLineType, Controls, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider,
  useNodesState, useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { draftPortText, isMulti, nodeOutput, outputList, outputNode, portText, stepsModel, unapprovedOutputs } from "./ports.js";

// The pipeline as a node map: sources → steps → output. It leads the page:
// clicking a node opens it in the inspector beside it.
//
// Two modes. Outside a draft the canvas shows the applied steps: inspect,
// preview and approve them, and drag nodes around. "Edit pipeline" opens a
// draft, where steps are added, wired, merged and removed freely; nothing runs
// or resets until Check passes and the planner presses Apply. The agent's
// proposal, when there is one, is drawn dashed on top: new nodes and wires,
// and badges on the steps it would change or remove.
//
// Display only: the sources are listed in one box (each row still wires on its
// own), and a run of Parse steps is drawn as one box with a row per field.
//
// Wires: step → step feeds the target (several wires into one step merge
// their records); step → Output feeds the final output; source → Extract
// makes that Extract step read the source. Wires from sources are drawn only
// while their step or source is selected or hovered (the step shows the sources
// it reads as chips), since they would otherwise cross the whole canvas.
//
// Layout: dagre lays the graph out left to right, ordering each column to keep
// wires from crossing. A node the planner drags keeps its place, saved in the
// definition's layout (display only); Tidy drops those and lays it all out again.
//
// A pipeline can have several Output nodes (output, output:o2 ...), each with its
// own schema; every branch ends in one of them. A source wired to a step that
// reads one source itself (a map lookup, the use tables) sets that step's source.

const COL = 230;    // spacing for proposed nodes, placed beside the laid-out graph
const ROW = 90;
const NODE_W = 164; // a node's size for the layout (.pl-node is 10.25rem wide); generous in height
const NODE_H = 96;
const SOURCE_WIRES = new Set(["source", "table", "reads"]);   // drawn on demand
const WIRE_PATH = { borderRadius: 10, offset: 18 };           // right-angled wires
const ROW_H = 28;   // a row in the sources box or a Parse box
const HEAD_H = 32;

function cut(v, n = 28) {
  if (v == null) return "";
  const s = typeof v === "object" ? JSON.stringify(v) : String(v);
  return s.length > n ? s.slice(0, n) + "…" : s;
}

// One line saying what a step does, from its config. linked: the sources it
// reads show as chips on the node, so the line leaves them out.
function stepSummary(s, linked = false) {
  const c = s.config || {};
  switch (s.type) {
    case "extract": return linked ? "" : c.source_ids?.length ? `from ${c.source_ids.join(", ")}` : "from all sources";
    case "parse": return `${c.field} as ${c.as}`;
    case "map_values": return `${c.field}: ${Object.keys(c.mapping || {}).length} mappings`;
    case "set_value": return `${c.field} = ${cut(c.value, 16)}`;
    case "filter": return `${c.field} ${c.op}${c.value != null ? " " + cut(c.value, 14) : ""}`;
    case "dedupe": return `by ${(c.keys || []).join(", ")}`;
    case "research": return c.fields?.length ? `fills ${c.fields.join(", ")}` : "fills blank fields";
    case "llm_fill": return `fills ${c.field}`;
    case "geo_tie": return `fills ${c.field || "geo_scope"}`;
    case "sql": return c.tables?.length && !linked ? `joins ${c.tables.join(", ")}` : cut((c.query || "").replace(/\s+/g, " "), 30);
    case "from_table": return `from ${c.table || "?"}`;
    case "read_table": return c.source_id ? (linked ? "CSV as a table" : `CSV ${c.source_id}`) : `table ${c.table || "?"}`;
    case "spatial_lookup": return `${c.field || "?"} → ${c.writes || "?"}${c.layers?.length ? ` · layer${c.layers.length > 1 ? "s" : ""} ${c.layers.join(", ")}` : ""}`;
    case "use_table": return c.source_id && !linked ? `from ${c.source_id}` : "use tables";
    default: return "";
  }
}

function stepStatus(s, out) {
  if (out?.error) return "bad";
  if (s?.status === "approved") return "ok";
  if (s?.status === "proposed") return "pending";
  return "idle";
}

const STATUS_TEXT = { ok: "approved", pending: "needs approval", bad: "failed", idle: "not set" };
const DRAFT_TEXT = { new: "new in the draft", changed: "changed in the draft", unchanged: "unchanged" };
const PROP_TEXT = { new: "Proposed by agent", changed: "Proposed change", removed: "Proposed removal" };
const TEAL = "#18B4A5", FAINT = "#8FA8BF";

// ── Nodes ────────────────────────────────────────────────────────────────────

function Box({ data, children, inputs, outputs }) {
  const tip = data.errs?.length ? data.errs.join("\n")
    : data.draft ? `${data.title}: ${DRAFT_TEXT[data.draft]}` : STATUS_TEXT[data.status] ? `${data.title}: ${STATUS_TEXT[data.status]}` : data.title;
  return (
    <div className={`pl-node ${data.status}${data.sel ? " sel" : ""}${data.running ? " running" : ""}${data.ghost ? " ghost" : ""}`
                    + `${data.draft ? " draft-" + data.draft : ""}${data.trace ? " trace" : ""}${data.prop ? " prop-" + data.prop : ""}`}
         title={tip}>
      {inputs && <Handle type="target" position={Position.Left} className="pl-handle" isConnectable={!!data.connectable} title={data.inTip} />}
      {data.prop && <span className="pl-node-prop">{PROP_TEXT[data.prop]}</span>}
      <span className="pl-node-kind">
        <span className="pl-node-dot" />{data.kind}
        {data.stepKind && <span className={"pl-node-badge " + data.stepKind} title={data.stepKind === "model" ? "Calls the model" : "A fixed rule: no model call"}>
          {data.stepKind === "model" ? "Model" : "Rule"}</span>}
        {data.draft && data.draft !== "unchanged" && <span className={"pl-node-draft " + data.draft}>{data.draft}</span>}
      </span>
      <span className="pl-node-title">{data.title}</span>
      {data.sub && <span className="pl-node-sub">{data.sub}</span>}
      {data.reads?.map((r) => (
        <span key={r.how} className="pl-node-reads" title={`${r.how} ${r.ids.length ? r.ids.join(", ") : "all sources"}: select the step to see the wires`}>
          {r.how} {r.ids.length ? r.ids.map((id) => <code key={id}>{id}</code>) : "all sources"}
        </span>
      ))}
      {data.meta && <span className="pl-node-meta">{data.meta}</span>}
      {data.errs?.length > 0 && <span className="pl-node-err" aria-label={`${data.errs.length} problem(s)`}>!</span>}
      {children}
      {outputs && <Handle type="source" position={Position.Right} className="pl-handle" isConnectable={!!data.connectable} title={data.outTip} />}
    </div>
  );
}

// A box holding rows: the sources, or a run of Parse steps.
function GroupBox({ data }) {
  return (
    <div className={"pl-group" + (data.trace ? " trace" : "") + (data.sel ? " sel" : "")} style={{ width: data.w, height: data.h }}>
      <div className="pl-group-head">
        <b>{data.title}</b><span className="pl-group-n">{data.count}</span>
        {data.sub && <span className="pl-node-sub">{data.sub}</span>}
      </div>
    </div>
  );
}

// One row in a box: a source (wires out to the steps that read it) or a Parse step.
function RowNode({ data }) {
  return (
    <div className={`pl-rownode ${data.status}${data.sel ? " sel" : ""}${data.trace ? " trace" : ""}${data.running ? " running" : ""}`
                    + `${data.draft ? " draft-" + data.draft : ""}${data.prop ? " prop-" + data.prop : ""}`}
         title={data.errs?.length ? data.errs.join("\n") : data.tip}>
      {data.inputs && <Handle type="target" position={Position.Left} className={"pl-handle" + (data.inner === "in" || data.inner === "both" ? " inner" : "")}
                              isConnectable={!!data.connectable} />}
      <span className="pl-node-dot" />
      <code className="pl-row-id">{data.kind}</code>
      <span className="pl-row-title">{data.title}</span>
      {data.draft && data.draft !== "unchanged" && <span className={"pl-node-draft " + data.draft}>{data.draft}</span>}
      {data.prop && <span className="pl-node-draft prop">{data.prop === "removed" ? "remove?" : "change?"}</span>}
      {data.errs?.length > 0 && <span className="pl-node-err">!</span>}
      {data.outputs && <Handle type="source" position={Position.Right} className={"pl-handle" + (data.inner === "out" || data.inner === "both" ? " inner" : "")}
                               isConnectable={!!data.connectable} />}
    </div>
  );
}

const NODE_TYPES = {
  source: (p) => <Box {...p} outputs />,
  step: (p) => <Box {...p} inputs outputs />,
  output: (p) => <Box {...p} inputs />,
  marker: (p) => <Box {...p} inputs outputs />,   // ghosts and the gaps node: wired, never connectable
  group: GroupBox,
  row: RowNode,
};

// ── Menus and details ────────────────────────────────────────────────────────

function Menu({ menu, items, onClose }) {
  const ref = useRef(null);
  useEffect(() => {
    const away = (e) => { if (!ref.current?.contains(e.target)) onClose(); };
    const key = (e) => e.key === "Escape" && onClose();
    window.addEventListener("pointerdown", away, true);
    window.addEventListener("keydown", key);
    return () => { window.removeEventListener("pointerdown", away, true); window.removeEventListener("keydown", key); };
  }, [onClose]);
  // Keep the menu inside the canvas.
  const [pos, setPos] = useState({ left: menu.x, top: menu.y });
  useEffect(() => {
    const el = ref.current, box = el?.parentElement;
    if (!el || !box) return;
    const w = box.clientWidth, h = box.clientHeight;
    setPos({ left: Math.max(4, Math.min(menu.x, w - el.offsetWidth - 4)), top: Math.max(4, Math.min(menu.y, h - el.offsetHeight - 4)) });
  }, [menu.x, menu.y]);
  return (
    <div ref={ref} className="pl-menu" style={pos} role="menu" onContextMenu={(e) => e.preventDefault()}>
      {items.filter(Boolean).map((it, i) => it === "-" ? <div key={i} className="pl-menu-sep" /> : (
        <button key={i} type="button" role="menuitem" disabled={it.disabled} title={it.why || ""}
                className={it.danger ? "danger" : ""} onClick={() => { onClose(); it.run(); }}>
          <span>{it.label}</span>{it.key && <kbd>{it.key}</kbd>}
        </button>
      ))}
    </div>
  );
}

// ── The canvas ───────────────────────────────────────────────────────────────

// Runs of Parse steps drawn as one box: each step after the first takes only the
// one before it, which feeds nothing else.
function parseRuns(model) {
  const used = {};
  for (const s of model.steps) for (const i of model.inputs[s.id] || []) (used[i] ||= []).push(s.id);
  for (const i of model.outputFrom) (used[i] ||= []).push("output");
  const byId = Object.fromEntries(model.steps.map((s) => [s.id, s]));
  const runs = [], seen = new Set();
  for (const sid of model.order) {
    const s = byId[sid];
    if (!s || s.type !== "parse" || seen.has(sid)) continue;
    const prev = (model.inputs[sid] || [])[0];
    if ((model.inputs[sid] || []).length === 1 && byId[prev]?.type === "parse" && (used[prev] || []).length === 1) continue;
    const run = [sid];
    let cur = sid;
    for (;;) {
      const next = used[cur] || [];
      if (next.length !== 1 || next[0] === "output" || byId[next[0]]?.type !== "parse" || (model.inputs[next[0]] || []).length !== 1) break;
      cur = next[0];
      run.push(cur);
    }
    run.forEach((x) => seen.add(x));
    if (run.length > 1) runs.push(run);
  }
  return runs;
}

// "+ Output": add an Output node loading into a target no output uses yet,
// or, for a kind whose outputs each write a table of their own, another one.
function AddOutput({ view, disabled, send }) {
  if (view.many_outputs) {
    return (
      <button className="pl-toolbar-select" disabled={disabled} title="Add an output with its own schema, written to a table of its own"
              onClick={() => send("POST", "/outputs", { target: view.targets?.[0]?.id })}>+ Output</button>
    );
  }
  const used = new Set(outputList(view).map((o) => o.target));
  const free = (view.targets || []).filter((t) => !used.has(t.id));
  if (!free.length) return null;
  return (
    <select className="pl-toolbar-select" value="" disabled={disabled} aria-label="Add an output"
            title="Add an output with its own schema, loading into another part of the dataset"
            onChange={(e) => e.target.value && send("POST", "/outputs", { target: e.target.value })}>
      <option value="">+ Output</option>
      {free.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
    </select>
  );
}

function Flow({ view, sel, onSelect, act, disabled, onAddStep, onAddSource, onEditStep, onAskAgent, onOpenSource,
                fill, highlight, focus, proposal, trace, onClearTrace, onShowCheck }) {
  const d = view.definition;
  const model = useMemo(() => stepsModel(view), [view]);
  const inDraft = model.draft;
  const sources = model.sources;
  const steps = model.steps;
  const liveSteps = useMemo(() => Object.fromEntries((d.steps || []).map((s) => [s.id, s])), [d.steps]);
  const gaps = useMemo(() => (!inDraft && steps.length ? view.design_gaps || [] : []), [inDraft, steps.length, view.design_gaps]);
  const busy = !!view.busy;
  const run = view.last_run;
  const multi = isMulti(view);
  const extraOuts = useMemo(() => outputList(view).filter((o) => o.id !== "output"), [view]);
  const mainOut = outputList(view)[0];
  const named = multi || (view.targets || []).length > 1;   // a kind with several targets names its outputs
  const canDesign = !unapprovedOutputs(view).length && d.sources_status === "final";
  const designWhy = unapprovedOutputs(view).length ? `Approve the target schema first${multi ? " (every output's)" : ""}` : "Mark the sources final first";
  const editable = inDraft && !disabled;
  const flow = useReactFlow();
  const wrap = useRef(null);

  const [menu, setMenu] = useState(null);
  const [notice, setNotice] = useState(null);
  const flash = useCallback((text) => setNotice({ text, at: Date.now() }), []);
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 6000);
    return () => clearTimeout(t);
  }, [notice]);

  // Problems from the draft's check, by node id on the canvas.
  const problems = useMemo(() => {
    const errors = {}, warnings = {};
    const outs = new Set(extraOuts.map((o) => o.id));
    const key = (n) => (n === "output" ? "output" : outs.has(n) ? outputNode(n) : n ? `step:${n}` : null);
    for (const e of view.draft?.check?.errors || []) if (key(e.node)) (errors[key(e.node)] ||= []).push(e.text);
    for (const w of view.draft?.check?.warnings || []) if (key(w.node)) (warnings[key(w.node)] ||= []).push(w.text);
    return { errors, warnings };
  }, [view.draft, extraOuts]);

  // Which step the server is on, from progress lines like "Running s2 (parse)".
  const [running, setRunning] = useState(null);
  const progress = busy ? view.progress || "" : null;
  const [seenProgress, setSeenProgress] = useState(undefined);
  if (progress !== seenProgress) {
    setSeenProgress(progress);
    const m = /(?:^|: )Running (\S+) /.exec(progress || "");   // "Use permissions: Running t5 (use_table)" with several outputs
    setRunning(progress === null ? null : m ? m[1] : /(?:^|: )Reading \d+ source/.test(progress) ? "sources" : running);
  }

  // Every change goes through act; errors show on the canvas.
  const send = useCallback(async (method, path, body) => {
    try { return await act(method, path, body, { throwErrors: true }); }
    catch (e) { flash(e.message.replace(/^\d+ — /, "")); return null; }
  }, [act, flash]);

  // ── layout: saved positions, else by depth in the graph ───────────────────
  const [local, setLocal] = useState(null);       // the layout as last changed here; null = the server's
  const layout = useMemo(() => local ?? d.layout ?? {}, [local, d.layout]);
  const queued = useRef(null);
  const saveLayout = useCallback((next) => { queued.current = next; setLocal(next); }, []);
  useEffect(() => {
    // Sent between other actions, never during one: the server saves the definition whole.
    if (!queued.current || disabled) return;
    const next = queued.current;
    queued.current = null;
    send("PUT", "/layout", { positions: next });
  });

  // Display grouping: sources in one box, runs of Parse steps in one box each.
  const runs = useMemo(() => parseRuns(model), [model]);
  const runOf = useMemo(() => Object.fromEntries(runs.flatMap((r) => r.map((sid, k) => [sid, { head: r[0], k, n: r.length }]))), [runs]);
  const boxed = sources.length > 0;
  const srcH = HEAD_H + sources.length * ROW_H + 6;

  // The agent's proposal, over what is on the canvas now.
  const prop = useMemo(() => {
    if (!proposal) return null;
    const have = new Set(steps.map((s) => s.id));
    const haveSrc = new Set(sources.map((s) => s.id));
    return {
      steps: proposal.steps.filter((s) => s.prop_state === "new" && !have.has(s.id)),
      changed: new Set(proposal.steps.filter((s) => s.prop_state === "changed").map((s) => s.id)),
      removed: new Set(proposal.removed || []),
      sources: proposal.sources.filter((x) => x.prop_state === "new" && !haveSrc.has(x.id)),
      all: proposal.steps, outputFrom: proposal.output_from, outputsFrom: proposal.outputs_from || {}, allSources: proposal.sources,
    };
  }, [proposal, steps, sources]);

  const nodeIds = useMemo(() => [
    ...(boxed ? ["srcbox", ...sources.map((s) => `src:${s.id}`)] : ["sources"]),
    ...(steps.length ? steps.map((s) => `step:${s.id}`) : ["steps"]),
    ...runs.map((r) => `parse:${r[0]}`),
    ...(gaps.length ? ["gaps"] : []), "output", ...extraOuts.map((o) => outputNode(o.id)),
  ], [boxed, sources, steps, runs, gaps.length, extraOuts]);

  // The node a step is drawn in: its own, or its Parse box.
  const boxOf = useCallback((sid) => (runOf[sid] ? `parse:${runOf[sid].head}` : `step:${sid}`), [runOf]);

  // Where dagre puts every node, top-left corners. Only the step → step and
  // step → Output wires shape it: the source wires are drawn on demand, so the
  // sources box is just kept left of the steps that read no other step.
  const auto = useMemo(() => {
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: "LR", nodesep: 28, ranksep: 66 });
    g.setDefaultEdgeLabel(() => ({}));
    const size = {};
    const put = (id, w, h) => { size[id] = { w, h }; g.setNode(id, { width: w, height: h }); };
    const start = boxed ? "srcbox" : "sources";
    put(start, boxed ? 196 : NODE_W, boxed ? srcH : NODE_H);
    for (const r of runs) put(`parse:${r[0]}`, 190, HEAD_H + r.length * ROW_H + 6);
    for (const s of steps) if (!runOf[s.id]) put(`step:${s.id}`, NODE_W, NODE_H);
    if (!steps.length) put("steps", NODE_W, NODE_H);
    if (gaps.length) put("gaps", NODE_W, NODE_H);
    const outs = ["output", ...extraOuts.map((o) => outputNode(o.id))];
    outs.forEach((id) => put(id, NODE_W, NODE_H));
    const wire = (a, b) => { if (a !== b && g.hasNode(a) && g.hasNode(b)) g.setEdge(a, b); };
    for (const s of steps) {
      const ins = model.inputs[s.id] || [];
      ins.forEach((i) => wire(boxOf(i), boxOf(s.id)));
      if (!ins.length) wire(start, boxOf(s.id));
    }
    if (!steps.length) { wire(start, "steps"); wire("steps", "output"); }
    const ends = model.outputFrom.filter((i) => steps.some((s) => s.id === i)).map(boxOf);
    if (gaps.length) { ends.forEach((e) => wire(e, "gaps")); wire("gaps", "output"); }
    else ends.forEach((e) => wire(e, "output"));
    for (const o of extraOuts) for (const i of model.outputsFrom[o.id] || []) if (steps.some((s) => s.id === i)) wire(boxOf(i), outputNode(o.id));
    dagre.layout(g);
    const pos = {};
    for (const id of g.nodes()) { const n = g.node(id); pos[id] = { x: Math.round(n.x - size[id].w / 2), y: Math.round(n.y - size[id].h / 2) }; }
    // An output nothing feeds yet waits in the last column, under the rest.
    const unfed = outs.filter((id) => !g.inEdges(id)?.length);
    const placed = Object.keys(pos).filter((id) => !unfed.includes(id));
    if (unfed.length && placed.length) {
      const right = Math.max(...placed.map((id) => pos[id].x));
      let below = Math.max(...placed.map((id) => pos[id].y + size[id].h)) + 40;
      for (const id of unfed) { pos[id] = { x: right, y: below }; below += NODE_H + 28; }
    }
    return pos;
  }, [boxed, srcH, runs, runOf, boxOf, steps, model, gaps.length, extraOuts]);

  const positions = useMemo(() => {
    const pos = {};
    const at = (id) => layout[id] || auto[id] || { x: 0, y: 0 };
    if (boxed) {
      pos.srcbox = at("srcbox");
      sources.forEach((s, i) => { pos[`src:${s.id}`] = { x: 6, y: HEAD_H + i * ROW_H }; });   // inside the box
    } else pos.sources = at("sources");
    for (const s of steps) if (!runOf[s.id]) pos[`step:${s.id}`] = at(`step:${s.id}`);
    for (const r of runs) {
      pos[`parse:${r[0]}`] = at(`parse:${r[0]}`);
      r.forEach((sid, k) => { pos[`step:${sid}`] = { x: 6, y: HEAD_H + k * ROW_H }; });   // inside the box
    }
    if (!steps.length) pos.steps = at("steps");
    if (gaps.length) pos.gaps = at("gaps");
    pos.output = at("output");
    extraOuts.forEach((o) => { pos[outputNode(o.id)] = at(outputNode(o.id)); });
    // Proposed nodes: new sources under the sources box, new steps in a row below
    // the graph, each a column right of the steps it takes.
    if (prop) {
      const bottom = Math.max(ROW, ...Object.entries(pos).filter(([k]) => !k.startsWith("src:") && !runOf[k.slice(5)])
        .map(([k, v]) => v.y + (k === "srcbox" ? srcH : k.startsWith("parse:") ? HEAD_H + runOf[k.slice(6)].n * ROW_H : NODE_H)));
      const base = boxed ? pos.srcbox : pos.sources;
      prop.sources.forEach((x, i) => { pos[`prop:src:${x.id}`] = { x: base.x, y: (boxed ? base.y + srcH : base.y + 70) + 24 + i * 64 }; });
      const px = {};
      const isNew = new Set(prop.steps.map((x) => x.id));
      const pIn = Object.fromEntries(prop.all.map((s) => [s.id, s.inputs || []]));
      const xOf = (sid, guard = 0) => {
        if (!isNew.has(sid)) return pos[boxOf(sid)]?.x ?? base.x;
        if (px[sid] == null) px[sid] = guard > 50 ? base.x : Math.max(base.x, ...(pIn[sid] || []).map((i) => xOf(i, guard + 1))) + COL;
        return px[sid];
      };
      prop.steps.forEach((x, i) => { pos[`prop:step:${x.id}`] = { x: xOf(x.id), y: bottom + 40 + (i % 2) * 10 }; });
    }
    return pos;
  }, [boxed, srcH, sources, layout, auto, steps, runs, runOf, boxOf, gaps.length, prop, extraOuts]);

  // ── what to draw ───────────────────────────────────────────────────────────
  const status = useMemo(() => {
    const st = {};
    const srcState = d.sources_status === "final" ? "ok" : sources.length ? "pending" : "idle";
    sources.forEach((s) => { st[`src:${s.id}`] = view.sources[s.id]?.error ? "bad" : srcState; });
    st.sources = srcState;
    steps.forEach((s) => {
      const id = `step:${s.id}`;
      if (inDraft) st[id] = problems.errors[id] ? "bad" : s.draft_state === "unchanged" ? stepStatus(liveSteps[s.id], view.outputs[s.id]) : "pending";
      else st[id] = stepStatus(s, view.outputs[s.id]);
    });
    st.steps = "idle";
    st.gaps = "bad";
    st.output = problems.errors.output ? "bad" : d.schema?.status !== "approved" ? "pending" : run ? (run.upto ? "pending" : "ok") : "idle";
    for (const o of extraOuts) {
      const id = outputNode(o.id);
      st[id] = problems.errors[id] ? "bad" : o.schema?.status !== "approved" ? "pending" : o.last_run ? (o.last_run.upto ? "pending" : "ok") : "idle";
    }
    return st;
  }, [d.schema?.status, d.sources_status, sources, steps, view.sources, view.outputs, run, inDraft, problems, liveSteps, extraOuts]);

  const reads = useCallback((s) => (s.config?.source_ids?.length ? s.config.source_ids : sources.map((x) => x.id)), [sources]);

  const base = useMemo(() => {
    const ns = [];
    const lit = (id) => !!highlight?.has(id);
    const add = (id, type, data, extra = {}) => ns.push({
      id, type, position: positions[id], deletable: false, connectable: false, ...extra,
      data: { status: status[id], sel: sel === id, connectable: false, trace: lit(id), ...data },
    });
    if (!sources.length) add("sources", "marker", { kind: "Sources", title: "No sources yet", sub: "add URLs or files", ghost: true });
    else add("srcbox", "group", { title: "Sources", count: sources.length, w: 196, h: srcH, trace: sources.some((s) => lit(`src:${s.id}`)) },
             { selectable: false, style: { width: 196, height: srcH } });
    sources.forEach((s) => {
      const meta = view.sources[s.id] || {};
      add(`src:${s.id}`, "row", {
        kind: s.id, title: cut(s.label, 26), outputs: true, connectable: editable, running: running === "sources",
        draft: inDraft && s.draft_state === "new" ? "new" : null,
        prop: prop?.removed && (proposal?.removed_sources || []).includes(s.id) ? "removed" : null,
        tip: `${s.label}\n${s.kind === "grants_gov" ? `Grants.gov: ${s.query}` : s.url}\n${meta.error ? "unreadable" : meta.chars != null ? `${meta.chars.toLocaleString()} characters` : ""}`,
      }, { parentId: "srcbox", extent: "parent", draggable: false, deletable: !disabled, connectable: editable, style: { width: 184 } });
    });
    if (!steps.length) add("steps", "marker", {
      kind: "Steps", ghost: true, title: busy && d.sources_status === "final" ? "Outlining…" : "No steps yet",
      sub: canDesign ? (inDraft ? "add a step, or ask the agent" : "Edit pipeline to add steps") : designWhy.toLowerCase(),
    });
    for (const r of runs) {
      const h = HEAD_H + r.length * ROW_H + 6;
      add(`parse:${r[0]}`, "group", { title: "Parse", count: `${r.length} fields`, w: 190, h, trace: r.some((x) => lit(`step:${x}`)) },
          { selectable: false, style: { width: 190, height: h } });
    }
    steps.forEach((s) => {
      const cat = view.catalog[s.type] || {};
      const id = `step:${s.id}`;
      const ins = model.inputs[s.id] || [];
      let meta, inTip, outTip;
      if (inDraft) {
        const port = draftPortText(view, s.id);
        meta = s.draft_state === "unchanged" ? "unchanged" : `${s.draft_state} in draft`;
        inTip = `Input: ${port.input}`;
        outTip = `Output: ${port.output}\n${port.writes.length ? `Writes: ${port.writes.join(", ")}` : "Writes no fields"}`;
      } else {
        const out = view.outputs[s.id];
        const before = ins.map((i) => view.outputs[i]);
        const inCount = before.length && before.every((o) => o && !o.error) && s.type !== "extract" ? before.reduce((a, o) => a + o.count, 0) : null;
        meta = out?.error ? "error" : out && !out.error ? `${inCount != null ? `${inCount} → ` : ""}${out.count} record${out.count === 1 ? "" : "s"}` : "not previewed";
        const port = portText(view, s.id);
        inTip = `Input: ${port.input}`;
        outTip = `Output: ${port.output}\n${port.writes.length ? `Writes: ${port.writes.join(", ")}` : "Writes no fields"}`;
      }
      const propState = prop?.removed.has(s.id) ? "removed" : prop?.changed.has(s.id) ? "changed" : null;
      const r = runOf[s.id];
      if (r) {
        add(id, "row", {
          kind: s.id, title: s.config?.as === "date" ? `${s.config?.field} → date` : s.config?.field || "", inputs: true, outputs: true,
          inner: r.n === 1 ? null : r.k === 0 ? "out" : r.k === r.n - 1 ? "in" : "both",
          running: running === s.id, connectable: editable, draft: inDraft ? s.draft_state : null, errs: problems.errors[id], prop: propState,
          tip: `${s.id} Parse ${s.config?.field} as ${s.config?.as} · ${meta}\n${inTip}\n${outTip}`,
        }, { parentId: `parse:${r.head}`, extent: "parent", draggable: false, deletable: editable, connectable: editable, style: { width: 178 } });
        return;
      }
      // The sources it reads, as chips: their wires show when it is selected or hovered.
      const linked = [];
      if (s.type === "extract") linked.push({ how: "reads", ids: s.config?.source_ids?.length ? s.config.source_ids : [] });
      const joins = s.type === "sql" ? (s.config?.tables || []).filter((x) => sources.some((y) => y.id === x)) : [];
      if (joins.length) linked.push({ how: "joins", ids: joins });
      if (s.config?.source_id && sources.some((x) => x.id === s.config.source_id)) linked.push({ how: "reads", ids: [s.config.source_id] });
      add(id, "step", {
        kind: s.id, n: model.order.indexOf(s.id) + 1, title: cat.label || s.type, sub: stepSummary(s, linked.length > 0), stepKind: cat.model ? "model" : "rule",
        reads: linked.length ? linked : null, running: running === s.id, connectable: editable, meta, inTip, outTip,
        draft: inDraft ? s.draft_state : null, errs: problems.errors[id], prop: propState,
      }, { deletable: editable, connectable: editable });
    });
    if (gaps.length) add("gaps", "marker", { kind: "Missing", title: "No step fills", sub: cut(gaps.join(", "), 40), meta: "right-click for help", ghost: true });
    add("output", "output", {
      kind: "Output", title: named ? mainOut.name : run && !run.upto && !inDraft ? "Final output" : "Output", connectable: editable,
      sub: inDraft ? (model.outputFrom.length ? `takes ${model.outputFrom.join(" + ")}` : "nothing wired in") : run ? `${run.coverage?.records ?? 0} records` : "no run yet",
      meta: inDraft ? null : run?.upto ? `preview to ${run.upto}` : run ? (run.at || "").slice(5, 16).replace("T", " ") : null,
      errs: problems.errors.output, prop: prop && JSON.stringify(prop.outputFrom) !== JSON.stringify(model.outputFrom) ? "changed" : null,
    }, { connectable: editable });
    for (const o of extraOuts) {
      const id = outputNode(o.id);
      const from = model.outputsFrom[o.id] || [];
      const r = o.last_run;
      add(id, "output", {
        kind: `Output · ${o.id}`, title: o.name, connectable: editable,
        sub: inDraft ? (from.length ? `takes ${from.join(" + ")}` : "nothing wired in")
          : o.schema?.status !== "approved" ? "schema to approve" : r ? `${r.coverage?.records ?? 0} records` : from.length ? "no run yet" : "nothing wired in",
        meta: inDraft ? null : (o.design_gaps || []).length ? `no step fills ${cut(o.design_gaps.join(", "), 30)}` : r?.upto ? `preview to ${r.upto}` : null,
        errs: problems.errors[id],
        prop: prop && JSON.stringify(prop.outputsFrom[o.id] || []) !== JSON.stringify(from) ? "changed" : null,
      }, { connectable: editable });
    }
    // The agent's proposed nodes: dashed, not editable until added to the draft.
    for (const x of prop?.sources || []) {
      add(`prop:src:${x.id}`, "source", { kind: x.id, title: cut(x.label, 26), sub: x.kind === "grants_gov" ? "Grants.gov search" : cut(x.url, 30), prop: "new", status: "pending" },
          { draggable: false });
    }
    for (const x of prop?.steps || []) {
      const cat = view.catalog[x.type] || {};
      add(`prop:step:${x.id}`, "step", { kind: x.id, title: cat.label || x.type, sub: stepSummary(x), stepKind: cat.model ? "model" : "rule", prop: "new", status: "pending" },
          { draggable: false });
    }
    return ns;
  }, [positions, status, sel, sources, steps, view, editable, disabled, running, busy, d.sources_status, canDesign, designWhy, gaps, run, model, inDraft,
      problems, runs, runOf, srcH, highlight, prop, proposal, named, mainOut, extraOuts]);

  const edges = useMemo(() => {
    const es = [];
    const edge = (source, target, kind, extra = {}) => {
      const done = !inDraft && status[source] === "ok" && status[target] !== "bad";
      const lit = highlight?.has(source) && highlight?.has(target);
      es.push({
        id: `${kind}:${source}>${target}`, source, target, data: { kind }, animated: busy, type: "smoothstep", pathOptions: WIRE_PATH,
        className: `pl-wire ${kind}${done ? " done" : ""}${lit ? " lit" : ""}`, deletable: false, reconnectable: false,
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: lit ? "#6055C9" : done ? TEAL : FAINT }, ...extra,
      });
    };
    const wired = { deletable: editable, reconnectable: editable };
    for (const s of steps) {
      for (const i of model.inputs[s.id] || []) {
        if (runOf[s.id]?.k > 0 && runOf[i]?.head === runOf[s.id].head) continue;   // inside a Parse box
        edge(`step:${i}`, `step:${s.id}`, "chain", wired);
      }
      if (s.type === "extract") reads(s).forEach((sid) => edge(`src:${sid}`, `step:${s.id}`, "source", wired));
      if (s.type === "sql") (s.config?.tables || []).filter((t) => sources.some((x) => x.id === t))
        .forEach((sid) => edge(`src:${sid}`, `step:${s.id}`, "table", { deletable: editable }));
      if (s.config?.source_id && sources.some((x) => x.id === s.config.source_id)) edge(`src:${s.config.source_id}`, `step:${s.id}`, "reads");
    }
    if (!steps.length) (sources.length ? sources.map((s) => `src:${s.id}`) : ["sources"]).forEach((n) => edge(n, "steps", "chain"));
    const ends = model.outputFrom.filter((i) => steps.some((s) => s.id === i)).map((i) => `step:${i}`);
    if (gaps.length) { ends.forEach((e) => edge(e, "gaps", "chain")); edge("gaps", "output", "chain"); }
    else ends.forEach((e) => edge(e, "output", "out", wired));
    for (const o of extraOuts) {
      for (const i of model.outputsFrom[o.id] || []) if (steps.some((s) => s.id === i)) edge(`step:${i}`, outputNode(o.id), "out", wired);
    }
    // The proposal's new wires, dashed.
    if (prop) {
      const has = new Set(es.map((e) => `${e.source}>${e.target}`));
      const isNew = new Set(prop.steps.map((x) => x.id));
      const isNewSrc = new Set(prop.sources.map((x) => x.id));
      const sNode = (sid) => (isNew.has(sid) ? `prop:step:${sid}` : `step:${sid}`);
      const srcNode = (sid) => (isNewSrc.has(sid) ? `prop:src:${sid}` : `src:${sid}`);
      const inBox = (a, b) => a.startsWith("step:") && b.startsWith("step:") && runOf[a.slice(5)] && runOf[a.slice(5)].head === runOf[b.slice(5)]?.head;
      const ghost = (a, b) => {
        if (has.has(`${a}>${b}`) || inBox(a, b)) return;
        has.add(`${a}>${b}`);
        es.push({ id: `prop:${a}>${b}`, source: a, target: b, data: { kind: "prop" }, className: "pl-wire prop", deletable: false, type: "smoothstep", pathOptions: WIRE_PATH,
                  reconnectable: false, selectable: false, markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: "#6055C9" } });
      };
      const allSrc = prop.allSources.map((x) => x.id);
      for (const x of prop.all) {
        for (const i of x.inputs || []) ghost(sNode(i), sNode(x.id));
        if (x.type === "extract") for (const sid of x.config?.source_ids?.length ? x.config.source_ids : allSrc) ghost(srcNode(sid), sNode(x.id));
      }
      for (const i of prop.outputFrom) ghost(sNode(i), "output");
      for (const o of extraOuts) for (const i of prop.outputsFrom[o.id] || []) ghost(sNode(i), outputNode(o.id));
    }
    return es;
  }, [steps, sources, gaps.length, status, busy, editable, reads, model, inDraft, runOf, highlight, prop, extraOuts]);

  // React Flow keeps measurements and selection on the node objects; refresh the rest.
  const [nodes, setNodes, onNodesChange] = useNodesState(base);
  useEffect(() => {
    setNodes((prev) => {
      const old = new Map(prev.map((n) => [n.id, n]));
      return base.map((n) => {
        const p = old.get(n.id);
        return p ? { ...p, ...n, position: p.dragging ? p.position : n.position, selected: p.selected } : n;
      });
    });
  }, [base, setNodes]);
  const [selEdges, setSelEdges] = useState([]);
  const [hover, setHover] = useState(null);
  const shownEdges = useMemo(() => edges.map((e) => {
    const selected = selEdges.includes(e.id);
    // A source wire shows while either end is selected or hovered, the sources box
    // is hovered, or a trace runs along it.
    const hidden = SOURCE_WIRES.has(e.data?.kind) && !selected && hover !== "srcbox" && !/lit/.test(e.className)
      && ![sel, hover].some((x) => x && (x === e.source || x === e.target));
    return { ...e, selected, hidden };
  }), [edges, selEdges, sel, hover]);
  const onEdgesChange = useCallback((changes) => {
    for (const c of changes) if (c.type === "select") setSelEdges((cur) => (c.selected ? [...new Set([...cur, c.id])] : cur.filter((x) => x !== c.id)));
  }, []);

  // Fit when the node set changes (a step added or removed), not on every refresh.
  const idsKey = nodeIds.join("|");
  useEffect(() => {
    const t = setTimeout(() => flow.fitView({ padding: 0.18, maxZoom: 1, duration: 200 }), 30);
    return () => clearTimeout(t);
  }, [idsKey, flow]);

  // Asked to show some nodes (a check item, a proposed change, a traced value).
  useEffect(() => {
    if (!focus) return;
    const t = setTimeout(() => {
      const ids = focus.ids.flatMap((id) => [id, `prop:${id}`]).filter((id) => flow.getNode(id));
      if (ids.length) flow.fitView({ nodes: ids.map((id) => ({ id })), padding: 0.7, maxZoom: 1.1, duration: 250 });
    }, 60);
    return () => clearTimeout(t);
  }, [focus, flow]);

  // ── operations ─────────────────────────────────────────────────────────────
  const stepOf = (nodeId) => steps.find((s) => `step:${s.id}` === nodeId);
  const sourceOf = (nodeId) => sources.find((s) => `src:${s.id}` === nodeId);
  const label = (s) => `${s.id} (${view.catalog[s.type]?.label || s.type})`;
  const nodeOf = (id) => nodeOutput(id) || id.slice(5);   // a step id, or an output id for an Output node
  const outName = (oid) => (multi ? outputList(view).find((o) => o.id === oid)?.name || oid : "Output");

  const openDraft = () => send("POST", "/draft");
  const link = (a, b) => send("POST", "/draft/links", { source: a, target: b });
  const unlink = (a, b) => send("POST", "/draft/unlink", { source: a, target: b });
  const wire = (src, s) => {
    if (s.type === "read_table") {   // a CSV source read as a table, in place of a Supabase table
      if (!view.source_tables?.[src.id]) { flash(`${src.id} is not a CSV, so it cannot be read as a table: wire it to an Extract step.`); return null; }
      if (s.config?.source_id === src.id) { flash(`${s.id} already reads ${src.id}.`); return null; }
      return send("PATCH", `/draft/steps/${s.id}`, { config: { ...s.config, source_id: src.id, table: null, fields: null } });
    }
    if (view.catalog[s.type]?.config?.source_id) {   // a step that reads one source itself
      if (s.config?.source_id === src.id) { flash(`${s.id} already reads ${src.id}.`); return null; }
      return send("PATCH", `/draft/steps/${s.id}`, { config: { ...s.config, source_id: src.id } });
    }
    if (s.type === "sql") {   // a CSV source joins a SQL step as a table
      if (!view.source_tables?.[src.id]) { flash(`${src.id} is not a CSV, so it cannot be a table: wire it to an Extract step.`); return null; }
      const tables = s.config?.tables || [];
      if (tables.includes(src.id)) { flash(`${s.id} already joins ${src.id}.`); return null; }
      return send("PATCH", `/draft/steps/${s.id}`, { config: { ...s.config, tables: [...tables, src.id] } });
    }
    if (s.type !== "extract") {
      const ex = steps.filter((x) => x.type === "extract").map((x) => x.id);
      flash(`Sources feed Extract steps (or, for a CSV, a Read table or SQL step as a table).${ex.length ? ` Wire ${src.id} to ${ex.join(" or ")}.` : " Add an Extract step first."}`);
      return null;
    }
    if (reads(s).includes(src.id)) { flash(`${s.id} already reads ${src.id}.`); return null; }
    return send("PATCH", `/draft/steps/${s.id}`, { config: { ...s.config, source_ids: [...reads(s), src.id] } });
  };
  const unwire = (src, s) => {
    const rest = reads(s).filter((x) => x !== src.id);
    if (!rest.length) { flash(`${s.id} must read at least one source. Wire another source to it first, or remove the step.`); return null; }
    return send("PATCH", `/draft/steps/${s.id}`, { config: { ...s.config, source_ids: rest } });
  };
  const unjoin = (src, s) => {
    const rest = (s.config?.tables || []).filter((t) => t !== src.id);
    return send("PATCH", `/draft/steps/${s.id}`, { config: { ...s.config, tables: rest.length ? rest : null } });
  };
  const removeStep = (s) => send("DELETE", `/draft/steps/${s.id}`);
  // Once sources are final this removes it in the draft (opening one); before, straight away.
  const removeSource = (src) => send("DELETE", `/sources/${src.id}`);
  const addSource = (at) => onAddSource({ at, onAdded: (id, pos) => pos && saveLayout({ ...layout, [`src:${id}`]: pos }) });
  const preview = (s) => send("POST", "/run", model.outputFrom.length === 1 && model.outputFrom[0] === s.id ? {} : { upto: s.id });
  const approve = (s) => send("POST", `/steps/${s.id}/approve`);

  const connect = ({ source, target }) => {
    if (!editable || !source || !target || source === target) return;
    const src = sourceOf(source), from = stepOf(source), to = stepOf(target);
    if (src && to) return wire(src, to);
    if (from && (to || nodeOutput(target))) return link(from.id, to ? to.id : nodeOutput(target));
    flash("Wire sources into Extract steps, and steps into the steps (or Output) that take their records.");
  };
  const isValidConnection = ({ source, target }) => {
    if (!source || !target || source === target) return false;
    if (source.startsWith("src:")) return target.startsWith("step:");
    return source.startsWith("step:") && (target.startsWith("step:") || !!nodeOutput(target));
  };
  const reconnect = async (old, next) => {
    if (!editable) return;
    if (old.data?.kind === "source") {
      const oldSrc = sourceOf(old.source), oldStep = stepOf(old.target);
      const src = sourceOf(next.source), to = stepOf(next.target);
      if (!src || !to) { flash("A source wire must run from a source to an Extract step."); return; }
      if (src === oldSrc && to === oldStep) return;
      if (to.type !== "extract") { wire(src, to); return; }
      if (to === oldStep) {   // same step, different source: swap it
        const ids = reads(to).filter((x) => x !== oldSrc.id);
        if (!ids.includes(src.id)) ids.push(src.id);
        send("PATCH", `/draft/steps/${to.id}`, { config: { ...to.config, source_ids: ids } });
        return;
      }
      if (await wire(src, to)) {
        const fresh = steps.find((x) => x.id === oldStep.id);
        if (fresh && src === oldSrc) unwire(oldSrc, fresh);
      }
      return;
    }
    if (!isValidConnection(next)) return;
    if (await unlink(nodeOf(old.source), nodeOf(old.target))) connect(next);
  };
  const onBeforeDelete = async ({ nodes: ns, edges: es }) => {
    if (disabled) return false;
    const doomed = editable ? ns.map((n) => stepOf(n.id)).filter(Boolean) : [];
    const doomedSources = ns.map((n) => sourceOf(n.id)).filter(Boolean);
    for (const src of doomed.length ? [] : doomedSources) if (!(await removeSource(src))) break;
    for (const s of doomed) if (!(await removeStep(s))) break;
    if (!ns.length && editable) {
      for (const e of es) {
        if (e.data?.kind === "source") { if (!(await unwire(sourceOf(e.source), stepOf(e.target)))) break; }
        else if (e.data?.kind === "table") { if (!(await unjoin(sourceOf(e.source), stepOf(e.target)))) break; }
        else if (!(await unlink(nodeOf(e.source), nodeOf(e.target)))) break;
      }
    }
    return false;   // the server's answer redraws the canvas
  };

  const onNodeDragStop = (_e, _n, dragged) => {
    const moved = Object.fromEntries((dragged || []).map((n) => [n.id, { x: Math.round(n.position.x), y: Math.round(n.position.y) }]));
    const keep = Object.fromEntries(Object.entries(layout).filter(([k]) => nodeIds.includes(k) || k.startsWith("step:")));
    saveLayout({ ...keep, ...moved });
  };
  const tidy = () => { saveLayout({}); setTimeout(() => flow.fitView({ padding: 0.18, maxZoom: 1, duration: 200 }), 60); };

  // A step added from the background lands where the planner right-clicked.
  const addAt = (opts) => onAddStep({ ...opts, onAdded: (id, at) => at && saveLayout({ ...layout, [`step:${id}`]: at }) });

  // ── menus ──────────────────────────────────────────────────────────────────
  const at = (e) => {
    const r = wrap.current.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const openMenu = (e, m) => { e.preventDefault(); setMenu({ ...at(e), ...m }); };
  const off = (why) => ({ disabled: true, why });
  const lock = disabled ? off(busy ? "Wait for the agent or the run to finish" : "Working…") : {};
  const design = canDesign ? {} : off(designWhy);

  function liveStepItems(s) {
    const out = view.outputs[s.id];
    const ancestors = [...(model.inputs[s.id] || [])];
    const seen = new Set();
    while (ancestors.length) { const a = ancestors.pop(); if (!seen.has(a)) { seen.add(a); ancestors.push(...(model.inputs[a] || [])); } }
    const blocked = steps.filter((x) => seen.has(x.id) && x.status !== "approved").map((x) => x.id);
    return [
      { label: "Open", run: () => onSelect(`step:${s.id}`) },
      "-",
      { label: out ? "Preview again" : "Preview this step", run: () => preview(s), ...lock },
      s.status !== "approved" && {
        label: `Approve ${s.id}`, run: () => approve(s), ...lock,
        ...(blocked.length ? off(`Approve ${blocked.join(", ")} first`) : !out ? off("Preview it first") : out.error ? off("Its last preview failed") : {}),
      },
      "-",
      { label: "Edit settings… (in a draft)", run: () => onEditStep(s.id), ...lock, ...design },
      { label: "Edit pipeline", run: openDraft, ...lock, ...design },
      { label: "Ask the agent about it…", run: () => onAskAgent(`About step ${label(s)}: `), ...lock },
    ];
  }

  function draftStepItems(s) {
    return [
      { label: "Open", run: () => onSelect(`step:${s.id}`) },
      { label: "Edit settings…", run: () => onEditStep(s.id), ...lock },
      "-",
      { label: "Insert step before…", run: () => addAt({ before: s.id }), ...lock },
      { label: "Insert step after…", run: () => addAt({ after: s.id }), ...lock },
      ...outputList(view).map((o) => ({
        label: `Wire into ${outName(o.id)}`, run: () => link(s.id, o.id), ...lock,
        ...((model.outputsFrom[o.id] || []).includes(s.id) ? off(`Already wired into ${outName(o.id)}`) : {}),
      })),
      { label: "Ask the agent about it…", run: () => onAskAgent(`About step ${label(s)} in the draft: `), ...lock },
      "-",
      { label: "Remove from draft", key: "Del", danger: true, run: () => removeStep(s), ...lock },
    ];
  }

  function menuItems(m) {
    if (m.kind === "pane") return [
      inDraft ? { label: "Add step here…", run: () => addAt({ at: m.flow }), ...lock }
        : { label: "Edit pipeline", run: openDraft, ...lock, ...design },
      { label: d.sources_status === "final" ? "Add source here… (in a draft)" : "Add source here…", run: () => addSource(m.flow), ...lock },
      { label: "Ask the agent…", run: () => onAskAgent(""), ...lock },
      inDraft && { label: "Check results", run: onShowCheck },
      "-",
      { label: "Fit to view", run: () => flow.fitView({ padding: 0.18, maxZoom: 1, duration: 200 }) },
      { label: "Tidy layout", run: tidy, ...(Object.keys(layout).length ? {} : off("Already laid out: nothing has been moved")) },
    ];
    if (m.kind === "edge") {
      const e = edges.find((x) => x.id === m.id);
      if (!e) return [];
      if (!inDraft) return [{ label: "Edit pipeline to rewire", run: openDraft, ...lock, ...design }];
      if (e.data.kind === "source") {
        const src = sourceOf(e.source), s = stepOf(e.target);
        return [
          { label: `${s.id} reads ${src.id}`, run: () => onSelect(e.source) },
          "-",
          { label: "Disconnect", key: "Del", danger: true, run: () => unwire(src, s), ...lock },
        ];
      }
      if (e.data.kind === "table") {
        const src = sourceOf(e.source), s = stepOf(e.target);
        return [
          { label: `${s.id} joins ${src.id} as a table`, run: () => onSelect(e.source) },
          "-",
          { label: "Stop joining it", key: "Del", danger: true, run: () => unjoin(src, s), ...lock },
        ];
      }
      const from = stepOf(e.source);
      return [
        from && { label: "Insert step here…", run: () => addAt({ after: from.id }), ...lock },
        { label: "Remove wire", key: "Del", danger: true, run: () => unlink(nodeOf(e.source), nodeOf(e.target)), ...lock },
      ];
    }
    const id = m.id;
    const s = stepOf(id);
    if (s) return inDraft ? draftStepItems(s) : liveStepItems(s);
    const src = sourceOf(id);
    if (src) return [
      { label: "Open", run: () => onSelect(id) },
      { label: "Open in the source reader", run: () => onOpenSource(src.id) },
      "-",
      { label: d.sources_status === "final" ? "Remove source (in a draft)" : "Remove source", key: "Del", danger: true, run: () => removeSource(src), ...lock },
    ];
    if (nodeOutput(id)) {
      const oid = nodeOutput(id);
      const wiredIn = (model.outputsFrom[oid] || []).length;
      return [
        { label: "Open", run: () => onSelect(id) },
        !inDraft && { label: "Run the whole pipeline", run: () => send("POST", "/run", {}), ...lock, ...(steps.length ? {} : off("No steps yet")) },
        multi && { label: `Add step feeding ${outName(oid)}…`, run: () => addAt({ connect: true, output: oid }), ...lock, ...design },
        oid !== "output" && "-",
        oid !== "output" && { label: "Remove output", danger: true, run: () => send("DELETE", `/outputs/${oid}`), ...lock,
                              ...(wiredIn ? off("Unwire its steps in a draft first") : {}) },
      ];
    }
    if (id === "gaps") return [
      { label: "Explain", run: () => flash(`No step produces ${gaps.join(", ")}. Add the steps that fill them in a draft, or ask the agent.`) },
      { label: "Edit pipeline to add them", run: openDraft, ...lock, ...design },
      { label: "Ask the agent to fill them…", run: () => onAskAgent(`No step produces ${gaps.join(", ")}. Add the steps that fill ${gaps.length > 1 ? "them" : "it"}.`), ...lock },
    ];
    if (id === "steps") return [
      inDraft ? { label: "Add step…", run: () => addAt({ connect: true }), ...lock }
        : { label: "Edit pipeline", run: openDraft, ...lock, ...design },
      { label: "Ask the agent to outline the steps…", run: () => onAskAgent("Outline the steps for this pipeline."), ...lock, ...design },
    ];
    if (id === "sources") return [
      { label: "Add source…", run: () => addSource(null), ...lock },
    ];
    return [];
  }


  // Group boxes stand for their rows; proposed nodes are not editable yet.
  const real = (id) => (id.startsWith("parse:") ? `step:${id.slice(6)}` : id);
  return (
    <div className={"pl-canvas" + (busy ? " busy" : "") + (inDraft ? " drafting" : "") + (fill ? " fill" : "")}>
      <div className="pl-toolbar">
        <button disabled={disabled} onClick={() => addSource(null)}
                title={d.sources_status === "final" ? "Goes into the draft; wire it to an Extract step" : "Fetched and added now"}>+ Source</button>
        <button disabled={disabled || !canDesign} onClick={() => addAt({ connect: true })}
                title={canDesign ? (inDraft ? `Add a step at the end, feeding ${outName("output")}` : "Opens a draft") : designWhy}>+ Step</button>
        {((view.targets || []).length > 1 || view.many_outputs) && <AddOutput view={view} disabled={disabled} send={send} />}
        <span className="pl-toolbar-sep" />
        <button title={Object.keys(layout).length ? "Lay the whole pipeline out again, undoing nodes you moved" : "Already laid out: nothing has been moved"}
                disabled={!Object.keys(layout).length} onClick={tidy}>Tidy</button>
      </div>
      {trace && (
        <div className="pl-trace-chip"><span className="pl-node-dot" /> Tracing <b>{trace}</b>
          <button className="link" onClick={onClearTrace} aria-label="Stop tracing">✕</button></div>
      )}
      <div className="pl-flow" ref={wrap}>
        <ReactFlow
          nodes={nodes} edges={shownEdges} nodeTypes={NODE_TYPES}
          onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
          onNodeClick={(_e, n) => {
            setMenu(null);
            if (n.id.startsWith("prop:")) { flash("Proposed by the agent: add it to the draft from the Agent tab to edit it."); return; }
            if (n.id !== "srcbox") onSelect(real(n.id));
          }}
          onNodeDoubleClick={(_e, n) => {
            const id = real(n.id);
            if (stepOf(id) && !disabled && canDesign) onEditStep(id.slice(5));
            else if (sourceOf(id)) onOpenSource(id.slice(4));
          }}
          onNodeContextMenu={(e, n) => (n.id.startsWith("prop:") ? e.preventDefault()
            : openMenu(e, { kind: "node", id: n.id === "srcbox" ? "sources" : real(n.id) }))}
          onEdgeContextMenu={(e, ed) => openMenu(e, { kind: "edge", id: ed.id })}
          onPaneContextMenu={(e) => openMenu(e, { kind: "pane", flow: flow.screenToFlowPosition({ x: e.clientX, y: e.clientY }) })}
          onNodeMouseEnter={(_e, n) => setHover(real(n.id))}
          onNodeMouseLeave={() => setHover(null)}
          onPaneClick={() => setMenu(null)}
          onMoveStart={() => setMenu(null)}
          onConnect={connect} isValidConnection={isValidConnection}
          onReconnect={reconnect} edgesReconnectable={editable}
          onBeforeDelete={onBeforeDelete} deleteKeyCode={disabled ? null : ["Delete", "Backspace"]}
          onNodeDragStop={onNodeDragStop}
          nodesConnectable={editable} elementsSelectable
          zoomOnScroll={false} preventScrolling={false} zoomOnDoubleClick={false}
          minZoom={0.3} maxZoom={1.6} fitView fitViewOptions={{ padding: 0.18, maxZoom: 1 }}
          connectionLineType={ConnectionLineType.SmoothStep} connectionRadius={28} proOptions={{ hideAttribution: true }}
        >
          <Background gap={16} size={1} />
          <Controls showInteractive={false} position="bottom-left" />
        </ReactFlow>
        {notice && <div className="pl-toast" role="status" onClick={() => setNotice(null)}>{notice.text}</div>}
        {menu && <Menu menu={menu} items={menuItems(menu)} onClose={() => setMenu(null)} />}
      </div>
    </div>
  );
}

export default function PipelineCanvas(props) {
  return (
    <ReactFlowProvider>
      <Flow {...props} />
    </ReactFlowProvider>
  );
}
