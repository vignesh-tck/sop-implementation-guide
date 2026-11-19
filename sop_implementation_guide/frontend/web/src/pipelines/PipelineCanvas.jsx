import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow, ReactFlowProvider,
  useNodesState, useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { STATE_TEXT, draftPortText, portText, stepsModel } from "./ports.js";

// The pipeline as a node map: sources → steps → output.
//
// Two modes. Outside a draft the canvas shows the applied steps: inspect,
// preview and approve them, and drag nodes around. "Edit pipeline" opens a
// draft, where steps are added, wired, merged and removed freely; nothing runs
// or resets until Check passes and the planner presses Apply.
//
// Wires: step → step feeds the target (several wires into one step merge
// their records); step → Output feeds the final output; source → Extract
// makes that Extract step read the source. Node positions are saved in the
// definition's layout, which is display only.

const COL = 230;    // auto-layout spacing for nodes with no saved position
const ROW = 90;

function cut(v, n = 28) {
  if (v == null) return "";
  const s = typeof v === "object" ? JSON.stringify(v) : String(v);
  return s.length > n ? s.slice(0, n) + "…" : s;
}

// One line saying what a step does, from its config.
function stepSummary(s) {
  const c = s.config || {};
  switch (s.type) {
    case "extract": return c.source_ids?.length ? `from ${c.source_ids.join(", ")}` : "from all sources";
    case "parse": return `${c.field} as ${c.as}`;
    case "map_values": return `${c.field}: ${Object.keys(c.mapping || {}).length} mappings`;
    case "set_value": return `${c.field} = ${cut(c.value, 16)}`;
    case "filter": return `${c.field} ${c.op}${c.value != null ? " " + cut(c.value, 14) : ""}`;
    case "dedupe": return `by ${(c.keys || []).join(", ")}`;
    case "research": return c.fields?.length ? `fills ${c.fields.join(", ")}` : "fills blank fields";
    case "llm_fill": return `fills ${c.field}`;
    case "geo_tie": return `fills ${c.field || "geo_scope"}`;
    case "sql": return c.tables?.length ? `joins ${c.tables.join(", ")}` : cut((c.query || "").replace(/\s+/g, " "), 30);
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
const TEAL = "#18B4A5", FAINT = "#8FA8BF";

// ── Nodes ────────────────────────────────────────────────────────────────────

function Box({ data, children, inputs, outputs }) {
  const tip = data.errs?.length ? data.errs.join("\n")
    : data.draft ? `${data.title}: ${DRAFT_TEXT[data.draft]}` : STATUS_TEXT[data.status] ? `${data.title}: ${STATUS_TEXT[data.status]}` : data.title;
  return (
    <div className={`pl-node ${data.status}${data.sel ? " sel" : ""}${data.running ? " running" : ""}${data.ghost ? " ghost" : ""}${data.draft ? " draft-" + data.draft : ""}`}
         title={tip}>
      {inputs && <Handle type="target" position={Position.Left} className="pl-handle" isConnectable={!!data.connectable} title={data.inTip} />}
      <span className="pl-node-kind">
        <span className="pl-node-dot" />{data.kind}
        {data.n != null && <span className="pl-node-n">#{data.n}</span>}
        {data.draft && data.draft !== "unchanged" && <span className={"pl-node-draft " + data.draft}>{data.draft}</span>}
        {data.model && <span className="pl-node-model" title="Calls the model">model</span>}
      </span>
      <span className="pl-node-title">{data.title}</span>
      {data.sub && <span className="pl-node-sub">{data.sub}</span>}
      {data.meta && <span className="pl-node-meta">{data.meta}</span>}
      {data.errs?.length > 0 && <span className="pl-node-err" aria-label={`${data.errs.length} problem(s)`}>!</span>}
      {children}
      {outputs && <Handle type="source" position={Position.Right} className="pl-handle" isConnectable={!!data.connectable} title={data.outTip} />}
    </div>
  );
}

const NODE_TYPES = {
  source: (p) => <Box {...p} outputs />,
  step: (p) => <Box {...p} inputs outputs />,
  output: (p) => <Box {...p} inputs />,
  marker: (p) => <Box {...p} inputs outputs />,   // ghosts and the gaps node: wired, never connectable
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

function Detail({ label, children }) {
  if (children == null || children === "" || children === false) return null;
  return <div className="pl-dl"><span>{label}</span><div>{children}</div></div>;
}

function ConfigTable({ config }) {
  const entries = Object.entries(config || {}).filter(([, v]) => v != null && v !== "");
  if (!entries.length) return <span className="assess">defaults</span>;
  return (
    <table className="pl-kv"><tbody>
      {entries.map(([k, v]) => (
        <tr key={k}><td><code>{k}</code></td>
          <td>{typeof v === "object" ? <pre>{JSON.stringify(v, null, 2)}</pre> : String(v)}</td></tr>
      ))}
    </tbody></table>
  );
}

function Settings({ s }) {
  return s.type === "sql" ? (
    <>
      <Detail label="Joins">{s.config?.tables?.join(", ") || "records only"}</Detail>
      <Detail label="SQL"><pre className="pl-sql pl-sql-small">{s.config?.query}</pre></Detail>
    </>
  ) : <Detail label="Settings"><ConfigTable config={s.config} /></Detail>;
}

function DetailsPane({ id, view, model, problems, onClose, actions }) {
  const d = view.definition;
  const sources = d.sources || [];
  const fields = d.schema?.fields || [];
  let title, body;
  if (id.startsWith("step:")) {
    const sid = id.slice(5);
    const s = model.steps.find((x) => x.id === sid);
    if (!s) return null;
    const cat = view.catalog[s.type] || {};
    title = `${s.id} · ${cat.label || s.type}`;
    const errs = problems.errors[id] || [], warns = problems.warnings[id] || [];
    if (model.draft) {
      const port = draftPortText(view, sid);
      body = (
        <>
          {cat.description && <div className="muted-note" style={{ marginTop: 0 }}>{cat.description}</div>}
          {errs.length > 0 && <Detail label="Errors"><ul className="pl-warn-list pl-bad">{errs.map((t, k) => <li key={k}>{t}</li>)}</ul></Detail>}
          {warns.length > 0 && <Detail label="Warnings"><ul className="pl-warn-list">{warns.map((t, k) => <li key={k}>{t}</li>)}</ul></Detail>}
          <Detail label="Draft">{DRAFT_TEXT[s.draft_state]}</Detail>
          <Detail label="Input">{port.input}</Detail>
          <Detail label="Output">{port.output}</Detail>
          <Detail label="Writes">{port.writes.length ? port.writes.map((w) => <code key={w} style={{ marginRight: ".3rem" }}>{w}</code>) : "no fields"}</Detail>
          {port.schema && <Detail label="Out schema">{fields.map((f) => (
            <div key={f.name}><code>{f.name}</code> <span className="assess">{STATE_TEXT[port.schema[f.name]] || "not produced"}</span></div>
          ))}</Detail>}
          <Detail label="Note">{s.note}</Detail>
          <Settings s={s} />
        </>
      );
    } else {
      const order = model.order;
      const out = view.outputs[s.id];
      const prompt = view.prompts?.[s.id];
      const port = portText(view, s.id);
      body = (
        <>
          {cat.description && <div className="muted-note" style={{ marginTop: 0 }}>{cat.description}</div>}
          <Detail label="Status">{STATUS_TEXT[stepStatus(s, out)]}</Detail>
          <Detail label="Runs">{`#${order.indexOf(s.id) + 1} of ${order.length}`}</Detail>
          <Detail label="Model call">{cat.model ? "yes, its prompt is in the editor below" : "no"}</Detail>
          <Detail label="Note">{s.note}</Detail>
          <Detail label="Input port">{port.input}</Detail>
          <Detail label="Output port">{port.output}</Detail>
          <Detail label="Writes">{port.writes.length ? port.writes.map((w) => <code key={w} style={{ marginRight: ".3rem" }}>{w}</code>) : "no fields"}</Detail>
          {port.error && <Detail label="Problem"><span className="pl-bad">{port.error}</span></Detail>}
          <Settings s={s} />
          {prompt?.value && <Detail label="Instructions"><span className="pl-pre">{prompt.value}</span></Detail>}
          <Detail label="Last preview">{out ? (out.error ? <span className="pl-bad">{out.error}</span> : `${out.count} record(s)`) : "not previewed with these settings"}</Detail>
          {out?.warnings?.length > 0 && <Detail label="Warnings"><ul className="pl-warn-list">{out.warnings.map((w, k) => <li key={k}>{w}</li>)}</ul></Detail>}
        </>
      );
    }
  } else if (id.startsWith("src:")) {
    const s = sources.find((x) => `src:${x.id}` === id);
    if (!s) return null;
    const meta = view.sources[s.id] || {};
    const readers = model.steps.filter((x) => x.type === "extract" && (!x.config?.source_ids?.length || x.config.source_ids.includes(s.id)));
    title = `${s.id} · source`;
    body = (
      <>
        <Detail label="Label">{s.label}</Detail>
        <Detail label={s.kind === "grants_gov" ? "Search" : "URL"}>
          {s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.url}</a> : s.query}</Detail>
        <Detail label="Fetched">{meta.error ? <span className="pl-bad">{meta.error}</span>
          : meta.chars != null ? `${meta.chars.toLocaleString()} characters${meta.truncated ? " (truncated)" : ""}` : "—"}</Detail>
        <Detail label="Read by">{readers.length ? readers.map((x) => x.id).join(", ") : "no Extract step"}</Detail>
        <Detail label="Sources">{d.sources_status === "final" ? "final" : "open"}</Detail>
        {meta.preview && <Detail label="Preview"><span className="pl-pre">{cut(meta.preview, 400)}</span></Detail>}
      </>
    );
  } else if (id === "output") {
    const run = view.last_run;
    title = "Output";
    const errs = problems.errors.output || [], warns = problems.warnings.output || [];
    body = (
      <>
        <Detail label="Takes">{model.outputFrom.join(" + ") || "nothing wired in"}</Detail>
        {errs.length > 0 && <Detail label="Errors"><ul className="pl-warn-list pl-bad">{errs.map((t, k) => <li key={k}>{t}</li>)}</ul></Detail>}
        {warns.length > 0 && <Detail label="Warnings"><ul className="pl-warn-list">{warns.map((t, k) => <li key={k}>{t}</li>)}</ul></Detail>}
        {run ? (
          <>
            <Detail label="Last run">{(run.at || "").slice(0, 16).replace("T", " ")}{run.upto ? ` (preview to ${run.upto})` : " (full run)"}</Detail>
            <Detail label="Records">{run.coverage?.records ?? 0}</Detail>
            <Detail label="Required blanks">{(run.coverage?.required_gaps || []).join(", ") || "none"}</Detail>
            <Detail label="Publish">{view.gate?.length ? <ul className="pl-warn-list">{view.gate.map((g, k) => <li key={k}>{g}</li>)}</ul> : "ready for review"}</Detail>
          </>
        ) : <div className="assess">No run yet.</div>}
      </>
    );
  } else if (id === "gaps") {
    title = "Missing steps";
    body = <div>No step produces {(view.design_gaps || []).map((g) => <code key={g} style={{ marginRight: ".3rem" }}>{g}</code>)}. Add a step that fills them in a draft, or ask the agent.</div>;
  } else return null;
  return (
    <aside className="pl-details" onContextMenu={(e) => e.preventDefault()}>
      <div className="pl-details-head"><b>{title}</b><span className="spacer" /><button className="link" onClick={onClose} aria-label="Close details">✕</button></div>
      <div className="pl-details-body">{body}</div>
      {actions?.length > 0 && <div className="pl-details-foot">{actions.map((a) => (
        <button key={a.label} disabled={a.disabled} title={a.why || ""} className={a.primary ? "primary" : ""} onClick={a.run}>{a.label}</button>
      ))}</div>}
    </aside>
  );
}

// What Check found, and what Apply would do.
function CheckPanel({ view, onFocus, onClose }) {
  const c = view.draft?.check;
  if (!c) return null;
  const steps = view.draft.steps || [];
  const added = steps.filter((s) => s.draft_state === "new").map((s) => s.id);
  const changed = steps.filter((s) => s.draft_state === "changed").map((s) => s.id);
  const removed = view.draft.removed || [];
  const plan = [added.length && `adds ${added.join(", ")}`, changed.length && `changes ${changed.join(", ")}`,
                removed.length && `removes ${removed.join(", ")}`].filter(Boolean).join("; ");
  const item = (x, k, cls) => (
    <li key={k} className={cls}>
      {x.node ? <button type="button" className="link" onClick={() => onFocus(x.node)}>{x.node === "output" ? "Output" : x.node}</button> : null}
      {x.node ? " · " : ""}{x.text}
    </li>
  );
  return (
    <div className="pl-check">
      <div className="pl-check-head">
        <b>Check</b>
        {c.ok ? <span className="tag ok">no errors</span> : <span className="tag gone">{c.errors.length} error{c.errors.length > 1 ? "s" : ""}</span>}
        {c.warnings.length > 0 && <span className="tag edited">{c.warnings.length} warning{c.warnings.length > 1 ? "s" : ""}</span>}
        <span className="assess">Apply {plan || "changes nothing"}. Unchanged steps keep their approval.</span>
        <span className="spacer" />
        <button className="link" onClick={onClose} aria-label="Hide check">✕</button>
      </div>
      {(c.errors.length > 0 || c.warnings.length > 0) && (
        <ul className="pl-check-list">
          {c.errors.map((x, k) => item(x, "e" + k, "err"))}
          {c.warnings.map((x, k) => item(x, "w" + k, "warn"))}
        </ul>
      )}
    </div>
  );
}

// ── The canvas ───────────────────────────────────────────────────────────────

function Flow({ view, sel, onSelect, act, disabled, onAddStep, onEditStep, onAskAgent, onOpenSource }) {
  const d = view.definition;
  const model = useMemo(() => stepsModel(view), [view]);
  const inDraft = model.draft;
  const sources = useMemo(() => d.sources || [], [d.sources]);
  const steps = model.steps;
  const liveSteps = useMemo(() => Object.fromEntries((d.steps || []).map((s) => [s.id, s])), [d.steps]);
  const gaps = useMemo(() => (!inDraft && steps.length ? view.design_gaps || [] : []), [inDraft, steps.length, view.design_gaps]);
  const busy = !!view.busy;
  const run = view.last_run;
  const canDesign = d.schema?.status === "approved" && d.sources_status === "final";
  const designWhy = d.schema?.status !== "approved" ? "Approve the target schema first" : "Mark the sources final first";
  const editable = inDraft && !disabled;
  const flow = useReactFlow();
  const wrap = useRef(null);

  const [menu, setMenu] = useState(null);
  const [details, setDetails] = useState(null);
  const [notice, setNotice] = useState(null);
  const [tall, setTall] = useState(false);
  const [showCheck, setShowCheck] = useState(false);
  const flash = useCallback((text) => setNotice({ text, at: Date.now() }), []);
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 6000);
    return () => clearTimeout(t);
  }, [notice]);

  // Problems from the draft's check, by node id on the canvas.
  const problems = useMemo(() => {
    const errors = {}, warnings = {};
    const key = (n) => (n === "output" ? "output" : n ? `step:${n}` : null);
    for (const e of view.draft?.check?.errors || []) if (key(e.node)) (errors[key(e.node)] ||= []).push(e.text);
    for (const w of view.draft?.check?.warnings || []) if (key(w.node)) (warnings[key(w.node)] ||= []).push(w.text);
    return { errors, warnings };
  }, [view.draft]);

  // Which step the server is on, from progress lines like "Running s2 (parse)".
  const [running, setRunning] = useState(null);
  const progress = busy ? view.progress || "" : null;
  const [seenProgress, setSeenProgress] = useState(undefined);
  if (progress !== seenProgress) {
    setSeenProgress(progress);
    const m = /^Running (\S+) /.exec(progress || "");
    setRunning(progress === null ? null : m ? m[1] : /^Reading \d+ source/.test(progress) ? "sources" : running);
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

  const nodeIds = useMemo(() => [
    ...(sources.length ? sources.map((s) => `src:${s.id}`) : ["sources"]),
    ...(steps.length ? steps.map((s) => `step:${s.id}`) : ["steps"]),
    ...(gaps.length ? ["gaps"] : []), "output",
  ], [sources, steps, gaps.length]);

  const positions = useMemo(() => {
    const pos = {};
    const srcIds = nodeIds.filter((id) => id.startsWith("src:") || id === "sources");
    srcIds.forEach((id, i) => { pos[id] = layout[id] || { x: 0, y: (i - (srcIds.length - 1) / 2) * ROW * 0.85 }; });
    const depth = {};
    for (const sid of model.order) depth[sid] = Math.max(-1, ...(model.inputs[sid] || []).map((i) => depth[i] ?? -1)) + 1;
    for (const s of steps) if (depth[s.id] == null) depth[s.id] = 0;
    const cols = {};
    for (const s of steps) (cols[depth[s.id]] ||= []).push(s.id);
    for (const [c, ids] of Object.entries(cols)) {
      ids.forEach((sid, i) => { pos[`step:${sid}`] = layout[`step:${sid}`] || { x: (Number(c) + 1) * COL, y: (i - (ids.length - 1) / 2) * ROW }; });
    }
    const last = steps.length ? Math.max(...Object.values(depth)) + 1 : 1;
    if (!steps.length) pos.steps = layout.steps || { x: COL, y: 0 };
    if (gaps.length) pos.gaps = layout.gaps || { x: (last + 1) * COL, y: 0 };
    pos.output = layout.output || { x: (last + 1 + (gaps.length ? 1 : 0)) * COL, y: 0 };
    return pos;
  }, [nodeIds, layout, model, steps, gaps.length]);

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
    st.output = problems.errors.output ? "bad" : run ? (run.upto ? "pending" : "ok") : "idle";
    return st;
  }, [d.sources_status, sources, steps, view.sources, view.outputs, run, inDraft, problems, liveSteps]);

  const reads = useCallback((s) => (s.config?.source_ids?.length ? s.config.source_ids : sources.map((x) => x.id)), [sources]);

  const base = useMemo(() => {
    const ns = [];
    const add = (id, type, data, extra = {}) => ns.push({
      id, type, position: positions[id], deletable: false, connectable: false, ...extra,
      data: { status: status[id], sel: sel === id, connectable: false, ...data },
    });
    if (!sources.length) add("sources", "marker", { kind: "Sources", title: "No sources yet", sub: "add URLs or files", ghost: true });
    sources.forEach((s) => {
      const meta = view.sources[s.id] || {};
      add(`src:${s.id}`, "source", {
        kind: s.id, title: cut(s.label, 24), connectable: editable, running: running === "sources",
        sub: s.kind === "grants_gov" ? "Grants.gov search" : cut(s.url, 30),
        meta: meta.error ? "unreadable" : meta.chars != null ? `${meta.chars.toLocaleString()} chars` : null,
      }, { deletable: !disabled, connectable: editable });
    });
    if (!steps.length) add("steps", "marker", {
      kind: "Steps", ghost: true, title: busy && d.sources_status === "final" ? "Outlining…" : "No steps yet",
      sub: canDesign ? (inDraft ? "add a step, or ask the agent" : "Edit pipeline to add steps") : designWhy.toLowerCase(),
    });
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
      add(id, "step", {
        kind: s.id, n: model.order.indexOf(s.id) + 1, title: cat.label || s.type, sub: stepSummary(s), model: cat.model,
        running: running === s.id, connectable: editable, meta, inTip, outTip,
        draft: inDraft ? s.draft_state : null, errs: problems.errors[id],
      }, { deletable: editable, connectable: editable });
    });
    if (gaps.length) add("gaps", "marker", { kind: "Missing", title: "No step fills", sub: cut(gaps.join(", "), 40), meta: "right-click for help", ghost: true });
    add("output", "output", {
      kind: "Output", title: run && !run.upto && !inDraft ? "Final output" : "Output", connectable: editable,
      sub: inDraft ? (model.outputFrom.length ? `takes ${model.outputFrom.join(" + ")}` : "nothing wired in") : run ? `${run.coverage?.records ?? 0} records` : "no run yet",
      meta: inDraft ? null : run?.upto ? `preview to ${run.upto}` : run ? (run.at || "").slice(5, 16).replace("T", " ") : null,
      errs: problems.errors.output,
    }, { connectable: editable });
    return ns;
  }, [positions, status, sel, sources, steps, view, editable, disabled, running, busy, d.sources_status, canDesign, designWhy, gaps, run, model, inDraft, problems]);

  const edges = useMemo(() => {
    const es = [];
    const edge = (source, target, kind, extra = {}) => {
      const done = !inDraft && status[source] === "ok" && status[target] !== "bad";
      es.push({
        id: `${kind}:${source}>${target}`, source, target, data: { kind }, animated: busy,
        className: `pl-wire ${kind}${done ? " done" : ""}`, deletable: false, reconnectable: false,
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: done ? TEAL : FAINT }, ...extra,
      });
    };
    const wired = { deletable: editable, reconnectable: editable };
    for (const s of steps) {
      for (const i of model.inputs[s.id] || []) edge(`step:${i}`, `step:${s.id}`, "chain", wired);
      if (s.type === "extract") reads(s).forEach((sid) => edge(`src:${sid}`, `step:${s.id}`, "source", wired));
    }
    if (!steps.length) (sources.length ? sources.map((s) => `src:${s.id}`) : ["sources"]).forEach((n) => edge(n, "steps", "chain"));
    const ends = model.outputFrom.filter((i) => steps.some((s) => s.id === i)).map((i) => `step:${i}`);
    if (gaps.length) { ends.forEach((e) => edge(e, "gaps", "chain")); edge("gaps", "output", "chain"); }
    else ends.forEach((e) => edge(e, "output", "out", wired));
    return es;
  }, [steps, sources, gaps.length, status, busy, editable, reads, model, inDraft]);

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
  const shownEdges = useMemo(() => edges.map((e) => ({ ...e, selected: selEdges.includes(e.id) })), [edges, selEdges]);
  const onEdgesChange = useCallback((changes) => {
    for (const c of changes) if (c.type === "select") setSelEdges((cur) => (c.selected ? [...new Set([...cur, c.id])] : cur.filter((x) => x !== c.id)));
  }, []);

  // Fit when the node set changes (a step added or removed), not on every refresh.
  const idsKey = nodeIds.join("|");
  useEffect(() => {
    const t = setTimeout(() => flow.fitView({ padding: 0.18, maxZoom: 1, duration: 200 }), 30);
    return () => clearTimeout(t);
  }, [idsKey, tall, flow]);

  // ── operations ─────────────────────────────────────────────────────────────
  const stepOf = (nodeId) => steps.find((s) => `step:${s.id}` === nodeId);
  const sourceOf = (nodeId) => sources.find((s) => `src:${s.id}` === nodeId);
  const label = (s) => `${s.id} (${view.catalog[s.type]?.label || s.type})`;
  const nodeOf = (id) => (id === "output" ? "output" : id.slice(5));

  const openDraft = () => send("POST", "/draft");
  const discard = () => {
    if (window.confirm("Discard the draft? Your changes to the steps are lost; the applied steps stay as they are.")) send("DELETE", "/draft");
  };
  const apply = async () => {
    const r = await send("POST", "/draft/apply");
    if (r) flash(`Applied${r.reset?.length ? `: ${r.reset.join(", ")} need approving again` : ""}.`);
    else setShowCheck(true);
  };
  const link = (a, b) => send("POST", "/draft/links", { source: a, target: b });
  const unlink = (a, b) => send("POST", "/draft/unlink", { source: a, target: b });
  const wire = (src, s) => {
    if (s.type !== "extract") {
      const ex = steps.filter((x) => x.type === "extract").map((x) => x.id);
      flash(`Sources feed Extract steps only.${ex.length ? ` Wire ${src.id} to ${ex.join(" or ")}.` : " Add an Extract step first."}`);
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
  const removeStep = (s) => send("DELETE", `/draft/steps/${s.id}`);
  const removeSource = (src) => {
    const msg = d.sources_status === "final" ? " The sources are reopened and every step goes back for approval." : "";
    if (!window.confirm(`Remove source ${src.id} (${src.label})?${msg}`)) return null;
    return send("DELETE", `/sources/${src.id}`);
  };
  const preview = (s) => send("POST", "/run", model.outputFrom.length === 1 && model.outputFrom[0] === s.id ? {} : { upto: s.id });
  const approve = (s) => send("POST", `/steps/${s.id}/approve`);

  const connect = ({ source, target }) => {
    if (!editable || !source || !target || source === target) return;
    const src = sourceOf(source), from = stepOf(source), to = stepOf(target);
    if (src && to) return wire(src, to);
    if (from && (to || target === "output")) return link(from.id, to ? to.id : "output");
    flash("Wire sources into Extract steps, and steps into the steps (or Output) that take their records.");
  };
  const isValidConnection = ({ source, target }) => {
    if (!source || !target || source === target) return false;
    if (source.startsWith("src:")) return target.startsWith("step:");
    return source.startsWith("step:") && (target.startsWith("step:") || target === "output");
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
    if (doomedSources.length > 1) flash("Remove one source at a time: each removal reopens the sources.");
    else if (doomedSources[0] && !doomed.length) removeSource(doomedSources[0]);
    for (const s of doomed) if (!(await removeStep(s))) break;
    if (!ns.length && editable) {
      for (const e of es) {
        if (e.data?.kind === "source") { if (!(await unwire(sourceOf(e.source), stepOf(e.target)))) break; }
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
  const focus = (node) => {
    const id = node === "output" ? "output" : `step:${node}`;
    onSelect(id);
    flow.fitView({ nodes: [{ id }], padding: 0.8, maxZoom: 1.1, duration: 250 });
  };

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
      { label: "Open in editor", run: () => onSelect(`step:${s.id}`) },
      { label: "Details", run: () => setDetails(`step:${s.id}`) },
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
      { label: "Details", run: () => setDetails(`step:${s.id}`) },
      { label: "Edit settings…", run: () => onEditStep(s.id), ...lock },
      "-",
      { label: "Insert step before…", run: () => addAt({ before: s.id }), ...lock },
      { label: "Insert step after…", run: () => addAt({ after: s.id }), ...lock },
      { label: "Wire into Output", run: () => link(s.id, "output"), ...lock, ...(model.outputFrom.includes(s.id) ? off("Already wired into Output") : {}) },
      { label: "Ask the agent about it…", run: () => onAskAgent(`About step ${label(s)} in the draft: `), ...lock },
      "-",
      { label: "Remove from draft", key: "Del", danger: true, run: () => removeStep(s), ...lock },
    ];
  }

  function menuItems(m) {
    if (m.kind === "pane") return [
      inDraft ? { label: "Add step here…", run: () => addAt({ at: m.flow }), ...lock }
        : { label: "Edit pipeline", run: openDraft, ...lock, ...design },
      { label: "Ask the agent…", run: () => onAskAgent(""), ...lock },
      "-",
      { label: "Fit to view", run: () => flow.fitView({ padding: 0.18, maxZoom: 1, duration: 200 }) },
      { label: "Tidy layout", run: tidy, ...(Object.keys(layout).length ? {} : off("Nothing has been moved")) },
    ];
    if (m.kind === "edge") {
      const e = edges.find((x) => x.id === m.id);
      if (!e) return [];
      if (!inDraft) return [{ label: "Edit pipeline to rewire", run: openDraft, ...lock, ...design }];
      if (e.data.kind === "source") {
        const src = sourceOf(e.source), s = stepOf(e.target);
        return [
          { label: `${s.id} reads ${src.id}`, run: () => setDetails(e.source) },
          "-",
          { label: "Disconnect", key: "Del", danger: true, run: () => unwire(src, s), ...lock },
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
      { label: "Details", run: () => setDetails(id) },
      { label: "Open in the source reader", run: () => onOpenSource(src.id) },
      "-",
      { label: "Remove source", danger: true, run: () => removeSource(src), ...lock },
    ];
    if (id === "output") return [
      !inDraft && { label: "Open output", run: () => onSelect("output") },
      { label: "Details", run: () => setDetails("output") },
      !inDraft && { label: "Run the whole pipeline", run: () => send("POST", "/run", {}), ...lock, ...(steps.length ? {} : off("No steps yet")) },
    ];
    if (id === "gaps") return [
      { label: "Details", run: () => setDetails("gaps") },
      { label: "Edit pipeline to add them", run: openDraft, ...lock, ...design },
      { label: "Ask the agent to fill them…", run: () => onAskAgent(`No step produces ${gaps.join(", ")}. Add the steps that fill ${gaps.length > 1 ? "them" : "it"}.`), ...lock },
    ];
    if (id === "steps") return [
      inDraft ? { label: "Add step…", run: () => addAt({ connect: true }), ...lock }
        : { label: "Edit pipeline", run: openDraft, ...lock, ...design },
      { label: "Ask the agent to outline the steps…", run: () => onAskAgent("Outline the steps for this pipeline."), ...lock, ...design },
    ];
    if (id === "sources") return [{ label: "Add sources", run: () => onOpenSource(null) }];
    return [];
  }

  function detailActions(id) {
    const s = stepOf(id);
    if (s) {
      if (inDraft) return [{ label: "Edit settings…", run: () => onEditStep(s.id), primary: true, ...lock }];
      const items = liveStepItems(s).filter((x) => x && x !== "-" && ["Open in editor", "Preview this step", "Preview again"].includes(x.label));
      return items.map((x, k) => ({ ...x, primary: k === 0 }));
    }
    const src = sourceOf(id);
    if (src) return [{ label: "Open in the source reader", run: () => onOpenSource(src.id) }];
    if (id === "output" && !inDraft) return [{ label: "Open output", run: () => onSelect("output"), primary: true }];
    return [];
  }

  const check = view.draft?.check;
  const approved = steps.filter((s) => s.status === "approved").length;
  return (
    <div className={"pl-canvas" + (busy ? " busy" : "") + (tall ? " tall" : "") + (inDraft ? " drafting" : "")}>
      <div className="pl-canvas-bar">
        <b>Pipeline</b>
        {inDraft ? (
          <>
            <span className="tag edited">draft</span>
            <span className="assess">{check ? (check.ok ? `ready to apply${check.warnings.length ? `, ${check.warnings.length} warning(s)` : ""}` : `${check.errors.length} error(s) to fix`) : ""}</span>
          </>
        ) : <span className="assess">{steps.length ? `${approved}/${steps.length} steps approved` : "no steps yet"}</span>}
        <span className="spacer" />
        {inDraft ? (
          <>
            <button className="pl-bar-btn" disabled={disabled} onClick={() => addAt({ connect: true })} title="Add a step at the end, feeding Output">+ Add step</button>
            <button className={"pl-bar-btn" + (showCheck ? " on" : "")} onClick={() => setShowCheck(!showCheck)}>
              Check{check && !check.ok ? ` (${check.errors.length})` : ""}
            </button>
            <button className="pl-bar-btn primary" disabled={disabled || !check?.ok} onClick={apply}
                    title={check?.ok ? "Replace the steps with the draft" : "Fix the errors Check reports first"}>Apply</button>
            <button className="pl-bar-btn" disabled={disabled} onClick={discard}>Discard</button>
          </>
        ) : (
          <button className="pl-bar-btn primary" disabled={disabled || !canDesign} title={canDesign ? "Change the steps in a draft" : designWhy}
                  onClick={openDraft}>Edit pipeline</button>
        )}
        <button className="pl-bar-btn" disabled={disabled} onClick={() => onAskAgent("")}>Ask the agent</button>
        <button className="pl-bar-btn" title="Put every node back in its default place" disabled={!Object.keys(layout).length} onClick={tidy}>Tidy</button>
        <button className="pl-bar-btn" title={tall ? "Shrink the canvas" : "Make the canvas taller"} onClick={() => setTall(!tall)}>{tall ? "Shrink" : "Expand"}</button>
      </div>
      {inDraft && showCheck && <CheckPanel view={view} onFocus={focus} onClose={() => setShowCheck(false)} />}
      <div className="pl-flow" ref={wrap}>
        <ReactFlow
          nodes={nodes} edges={shownEdges} nodeTypes={NODE_TYPES}
          onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
          onNodeClick={(_e, n) => { setMenu(null); onSelect(n.id); }}
          onNodeDoubleClick={(_e, n) => {
            if (stepOf(n.id) && !disabled && canDesign) onEditStep(n.id.slice(5));
            else if (sourceOf(n.id)) onOpenSource(n.id.slice(4));
            else setDetails(n.id);
          }}
          onNodeContextMenu={(e, n) => openMenu(e, { kind: "node", id: n.id })}
          onEdgeContextMenu={(e, ed) => openMenu(e, { kind: "edge", id: ed.id })}
          onPaneContextMenu={(e) => openMenu(e, { kind: "pane", flow: flow.screenToFlowPosition({ x: e.clientX, y: e.clientY }) })}
          onPaneClick={() => setMenu(null)}
          onMoveStart={() => setMenu(null)}
          onConnect={connect} isValidConnection={isValidConnection}
          onReconnect={reconnect} edgesReconnectable={editable}
          onBeforeDelete={onBeforeDelete} deleteKeyCode={disabled ? null : ["Delete", "Backspace"]}
          onNodeDragStop={onNodeDragStop}
          nodesConnectable={editable} elementsSelectable
          zoomOnScroll={false} preventScrolling={false} zoomOnDoubleClick={false}
          minZoom={0.3} maxZoom={1.6} fitView fitViewOptions={{ padding: 0.18, maxZoom: 1 }}
          connectionRadius={28} proOptions={{ hideAttribution: true }}
        >
          <Background gap={16} size={1} />
          <Controls showInteractive={false} position="bottom-left" />
          <MiniMap pannable zoomable position="bottom-right" className="pl-minimap"
                   nodeColor={(n) => ({ ok: TEAL, pending: "#6055C9", bad: "#D1495B" }[n.data?.status] || FAINT)} />
        </ReactFlow>
        <div className="pl-flow-hint">
          {inDraft ? "Draft: drag from a dot to wire · several wires into a step merge · right-click for more · nothing changes until Apply"
            : "Edit pipeline to change the steps · right-click for more · Ctrl+scroll to zoom"}
        </div>
        {notice && <div className="pl-toast" role="status" onClick={() => setNotice(null)}>{notice.text}</div>}
        {menu && <Menu menu={menu} items={menuItems(menu)} onClose={() => setMenu(null)} />}
        {details && <DetailsPane id={details} view={view} model={model} problems={problems} onClose={() => setDetails(null)} actions={detailActions(details)} />}
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
