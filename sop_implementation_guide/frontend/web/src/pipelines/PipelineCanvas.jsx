import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow, ReactFlowProvider,
  useNodesState, useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

// The pipeline as an editable node map: sources → steps → output.
//
// Steps run as one chain, so wires mean two things:
//   step → step     "run the target right after the source" (reorders the chain)
//   source → Extract "this Extract step reads that source" (its source_ids)
// Nodes go wherever the planner drags them; positions are saved in the
// definition's layout, which is display only (no step is reset by it).
//
// Right-click anything for its actions and details; Delete removes the
// selection; drag from a node's right-hand dot to rewire.

const COL = 230;    // auto-layout spacing for nodes with no saved position
const ROW = 76;

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
    default: return "";
  }
}

function stepStatus(s, out) {
  if (out?.error) return "bad";
  if (s.status === "approved") return "ok";
  if (s.status === "proposed") return "pending";
  return "idle";
}

const STATUS_TEXT = { ok: "approved", pending: "needs approval", bad: "failed", idle: "not set" };
const TEAL = "#18B4A5", FAINT = "#8FA8BF";

// ── Nodes ────────────────────────────────────────────────────────────────────

function Box({ data, children, inputs, outputs }) {
  return (
    <div className={`pl-node ${data.status}${data.sel ? " sel" : ""}${data.running ? " running" : ""}${data.ghost ? " ghost" : ""}`}
         title={STATUS_TEXT[data.status] ? `${data.title}: ${STATUS_TEXT[data.status]}` : data.title}>
      {inputs && <Handle type="target" position={Position.Left} className="pl-handle" isConnectable={!!data.connectable} />}
      <span className="pl-node-kind">
        <span className="pl-node-dot" />{data.kind}
        {data.n != null && <span className="pl-node-n">#{data.n}</span>}
        {data.model && <span className="pl-node-model" title="Calls the model">model</span>}
      </span>
      <span className="pl-node-title">{data.title}</span>
      {data.sub && <span className="pl-node-sub">{data.sub}</span>}
      {data.meta && <span className="pl-node-meta">{data.meta}</span>}
      {children}
      {outputs && <Handle type="source" position={Position.Right} className="pl-handle" isConnectable={!!data.connectable} />}
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

function DetailsPane({ id, view, onClose, actions }) {
  const d = view.definition;
  const steps = d.steps || [];
  const sources = d.sources || [];
  let title, body;
  if (id.startsWith("step:")) {
    const sid = id.slice(5);
    const i = steps.findIndex((s) => s.id === sid);
    const s = steps[i];
    if (!s) return null;
    const cat = view.catalog[s.type] || {};
    const out = view.outputs[s.id];
    const prompt = view.prompts?.[s.id];
    title = `${s.id} · ${cat.label || s.type}`;
    body = (
      <>
        {cat.description && <div className="muted-note" style={{ marginTop: 0 }}>{cat.description}</div>}
        <Detail label="Status">{STATUS_TEXT[stepStatus(s, out)]}</Detail>
        <Detail label="Runs">{`#${i + 1} of ${steps.length}`}{i > 0 ? `, after ${steps[i - 1].id}` : ", first"}</Detail>
        {s.type === "extract" && <Detail label="Reads">{s.config?.source_ids?.length ? s.config.source_ids.join(", ") : `every source (${sources.length})`}</Detail>}
        <Detail label="Model call">{cat.model ? "yes, its prompt is in the editor below" : "no"}</Detail>
        <Detail label="Note">{s.note}</Detail>
        <Detail label="Settings"><ConfigTable config={s.config} /></Detail>
        {prompt?.value && <Detail label="Instructions"><span className="pl-pre">{prompt.value}</span></Detail>}
        <Detail label="Last preview">{out ? (out.error ? <span className="pl-bad">{out.error}</span> : `${out.count} record(s)`) : "not previewed with these settings"}</Detail>
        {out?.warnings?.length > 0 && <Detail label="Warnings"><ul className="pl-warn-list">{out.warnings.map((w, k) => <li key={k}>{w}</li>)}</ul></Detail>}
      </>
    );
  } else if (id.startsWith("src:")) {
    const s = sources.find((x) => `src:${x.id}` === id);
    if (!s) return null;
    const meta = view.sources[s.id] || {};
    const readers = steps.filter((x) => x.type === "extract" && (!x.config?.source_ids?.length || x.config.source_ids.includes(s.id)));
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
    body = run ? (
      <>
        <Detail label="Last run">{(run.at || "").slice(0, 16).replace("T", " ")}{run.upto ? ` (preview to ${run.upto})` : " (full run)"}</Detail>
        <Detail label="Records">{run.coverage?.records ?? 0}</Detail>
        <Detail label="Required blanks">{(run.coverage?.required_gaps || []).join(", ") || "none"}</Detail>
        <Detail label="Publish">{view.gate?.length ? <ul className="pl-warn-list">{view.gate.map((g, k) => <li key={k}>{g}</li>)}</ul> : "ready for review"}</Detail>
      </>
    ) : <div className="assess">No run yet.</div>;
  } else if (id === "gaps") {
    title = "Missing steps";
    body = <div>No step produces {(view.design_gaps || []).map((g) => <code key={g} style={{ marginRight: ".3rem" }}>{g}</code>)}. Add a step that fills them, or ask the agent.</div>;
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

// ── The canvas ───────────────────────────────────────────────────────────────

function Flow({ view, sel, onSelect, act, disabled, onAddStep, onEditStep, onAskAgent, onOpenSource }) {
  const d = view.definition;
  const sources = useMemo(() => d.sources || [], [d.sources]);
  const steps = useMemo(() => d.steps || [], [d.steps]);
  const gaps = useMemo(() => (steps.length ? view.design_gaps || [] : []), [steps.length, view.design_gaps]);
  const busy = !!view.busy;
  const run = view.last_run;
  const canAdd = d.schema?.status === "approved" && d.sources_status === "final";
  const addWhy = d.schema?.status !== "approved" ? "Approve the target schema first" : "Mark the sources final first";
  const editable = !disabled;
  const flow = useReactFlow();
  const wrap = useRef(null);

  const [menu, setMenu] = useState(null);
  const [details, setDetails] = useState(null);
  const [notice, setNotice] = useState(null);
  const [tall, setTall] = useState(false);
  const flash = useCallback((text) => setNotice({ text, at: Date.now() }), []);
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 6000);
    return () => clearTimeout(t);
  }, [notice]);

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

  // ── layout: saved positions, else follow the chain ─────────────────────────
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
    srcIds.forEach((id, i) => { pos[id] = layout[id] || { x: 0, y: (i - (srcIds.length - 1) / 2) * ROW }; });
    let prev = { x: 0, y: 0 };
    for (const id of nodeIds.filter((x) => !srcIds.includes(x))) {
      pos[id] = layout[id] || { x: prev.x + COL, y: prev.y };
      prev = pos[id];
    }
    return pos;
  }, [nodeIds, layout]);

  // ── what to draw ───────────────────────────────────────────────────────────
  const status = useMemo(() => {
    const st = {};
    const srcState = d.sources_status === "final" ? "ok" : sources.length ? "pending" : "idle";
    sources.forEach((s) => { st[`src:${s.id}`] = view.sources[s.id]?.error ? "bad" : srcState; });
    st.sources = srcState;
    steps.forEach((s) => { st[`step:${s.id}`] = stepStatus(s, view.outputs[s.id]); });
    st.steps = "idle";
    st.gaps = "bad";
    st.output = run ? (run.upto ? "pending" : "ok") : "idle";
    return st;
  }, [d.sources_status, sources, steps, view.sources, view.outputs, run]);

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
      }, { deletable: editable, connectable: editable });
    });
    if (!steps.length) add("steps", "marker", {
      kind: "Steps", ghost: true, title: busy && d.sources_status === "final" ? "Outlining…" : "No steps yet",
      sub: canAdd ? "right-click to add, or ask the agent" : d.sources_status === "final" ? "the agent outlines them" : "after sources are final",
    });
    steps.forEach((s, i) => {
      const cat = view.catalog[s.type] || {};
      const out = view.outputs[s.id];
      add(`step:${s.id}`, "step", {
        kind: s.id, n: i + 1, title: cat.label || s.type, sub: stepSummary(s), model: cat.model,
        running: running === s.id, connectable: editable,
        meta: out?.error ? "error" : out ? `${out.count} record${out.count === 1 ? "" : "s"}` : "not previewed",
      }, { deletable: editable, connectable: editable });
    });
    if (gaps.length) add("gaps", "marker", { kind: "Missing", title: "No step fills", sub: cut(gaps.join(", "), 40), meta: "right-click for help", ghost: true });
    add("output", "output", {
      kind: "Output", title: run && !run.upto ? "Final output" : "Output", connectable: editable,
      sub: run ? `${run.coverage?.records ?? 0} records` : "no run yet",
      meta: run?.upto ? `preview to ${run.upto}` : run ? (run.at || "").slice(5, 16).replace("T", " ") : null,
    }, { connectable: editable });
    return ns;
  }, [positions, status, sel, sources, steps, view.sources, view.catalog, view.outputs, editable, running, busy, d.sources_status, canAdd, gaps, run]);

  const edges = useMemo(() => {
    const es = [];
    const edge = (source, target, kind, extra = {}) => {
      const done = status[source] === "ok" && status[target] !== "bad";
      es.push({
        id: `${kind}:${source}>${target}`, source, target, data: { kind }, animated: busy,
        className: `pl-wire ${kind}${done ? " done" : ""}`, deletable: false, reconnectable: false,
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: done ? TEAL : FAINT }, ...extra,
      });
    };
    const srcNode = (id) => `src:${id}`;
    steps.forEach((s, i) => {
      if (i > 0) edge(`step:${steps[i - 1].id}`, `step:${s.id}`, "chain", { reconnectable: editable });
      if (s.type === "extract") reads(s).forEach((sid) => edge(srcNode(sid), `step:${s.id}`, "source", { deletable: editable, reconnectable: editable }));
    });
    const firstIn = sources.length ? sources.map((s) => srcNode(s.id)) : ["sources"];
    if (!steps.length) firstIn.forEach((n) => edge(n, "steps", "chain"));
    else if (!steps.some((s) => s.type === "extract")) firstIn.forEach((n) => edge(n, `step:${steps[0].id}`, "implicit"));
    const last = steps.length ? `step:${steps[steps.length - 1].id}` : "steps";
    if (gaps.length) { edge(last, "gaps", "chain"); edge("gaps", "output", "chain"); }
    else edge(last, "output", "chain", { reconnectable: editable && steps.length > 0 });
    return es;
  }, [steps, sources, gaps.length, status, busy, editable, reads]);

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

  const placeAt = (s, index) => {
    if (steps.indexOf(s) === index) return null;
    return send("POST", `/steps/${s.id}/move`, { position: index });
  };
  // Run `s` right after `after` (null = first).
  const placeAfter = (s, after) => {
    const order = steps.filter((x) => x !== s);
    return placeAt(s, after ? order.indexOf(after) + 1 : 0);
  };
  const wire = (src, s) => {
    if (s.type !== "extract") {
      const ex = steps.filter((x) => x.type === "extract").map((x) => x.id);
      flash(`Sources feed Extract steps only; ${s.id} works on the records before it.${ex.length ? ` Wire ${src.id} to ${ex.join(" or ")}.` : " Add an Extract step first."}`);
      return null;
    }
    if (reads(s).includes(src.id)) { flash(`${s.id} already reads ${src.id}.`); return null; }
    return send("PATCH", `/steps/${s.id}`, { config: { ...s.config, source_ids: [...reads(s), src.id] } });
  };
  const unwire = (src, s) => {
    const rest = reads(s).filter((x) => x !== src.id);
    if (!rest.length) { flash(`${s.id} must read at least one source. Wire another source to it first, or remove the step.`); return null; }
    return send("PATCH", `/steps/${s.id}`, { config: { ...s.config, source_ids: rest } });
  };
  const removeStep = (s) => {
    const later = steps.slice(steps.indexOf(s) + 1).filter((x) => x.status === "approved").length;
    if (!window.confirm(`Remove step ${label(s)}?${later ? ` ${later} later approved step(s) go back for approval.` : ""}`)) return null;
    return send("DELETE", `/steps/${s.id}`);
  };
  const removeSource = (src) => {
    const msg = d.sources_status === "final" ? " The sources are reopened and every step goes back for approval." : "";
    if (!window.confirm(`Remove source ${src.id} (${src.label})?${msg}`)) return null;
    return send("DELETE", `/sources/${src.id}`);
  };
  const preview = (s) => send("POST", "/run", s === steps[steps.length - 1] ? {} : { upto: s.id });
  const approve = (s) => send("POST", `/steps/${s.id}/approve`);

  const connect = ({ source, target }) => {
    if (!editable || !source || !target || source === target) return;
    const src = sourceOf(source), from = stepOf(source), to = stepOf(target);
    if (src && to) return wire(src, to);
    if (from && to) return placeAfter(to, from);
    if (from && target === "output") return placeAt(from, steps.length - 1);
    flash("That wire has no meaning here: wire sources into Extract steps, and steps into the steps that should follow them.");
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
        send("PATCH", `/steps/${to.id}`, { config: { ...to.config, source_ids: ids } });
        return;
      }
      if (await wire(src, to)) {
        const fresh = steps.find((x) => x.id === oldStep.id);
        if (fresh && src === oldSrc) unwire(oldSrc, fresh);
      }
      return;
    }
    connect(next);
  };
  const onBeforeDelete = async ({ nodes: ns, edges: es }) => {
    if (!editable) return false;
    const doomedSteps = ns.map((n) => stepOf(n.id)).filter(Boolean);
    const doomedSources = ns.map((n) => sourceOf(n.id)).filter(Boolean);
    if (doomedSteps.length + doomedSources.length > 1) {
      flash("Remove one node at a time: each removal can send steps back for approval.");
    } else if (doomedSteps[0]) removeStep(doomedSteps[0]);
    else if (doomedSources[0]) removeSource(doomedSources[0]);
    else {
      const e = es.find((x) => x.data?.kind === "source");
      if (e) unwire(sourceOf(e.source), stepOf(e.target));
    }
    return false;   // the server's answer redraws the canvas
  };

  const onNodeDragStop = (_e, _n, dragged) => {
    const moved = Object.fromEntries((dragged || []).map((n) => [n.id, { x: Math.round(n.position.x), y: Math.round(n.position.y) }]));
    const keep = Object.fromEntries(Object.entries(layout).filter(([k]) => nodeIds.includes(k)));
    saveLayout({ ...keep, ...moved });
  };
  const tidy = () => { saveLayout({}); setTimeout(() => flow.fitView({ padding: 0.18, maxZoom: 1, duration: 200 }), 60); };

  // A step added on the canvas lands where the planner right-clicked.
  const addAt = (opts) => onAddStep({ ...opts, onAdded: (id, at) => at && saveLayout({ ...layout, [`step:${id}`]: at }) });

  // ── menus ──────────────────────────────────────────────────────────────────
  const at = (e) => {
    const r = wrap.current.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const openMenu = (e, m) => { e.preventDefault(); setMenu({ ...at(e), ...m }); };
  const off = (why) => ({ disabled: true, why });
  const lock = disabled ? off(busy ? "Wait for the agent or the run to finish" : "Working…") : {};

  function stepItems(s) {
    const i = steps.indexOf(s);
    const out = view.outputs[s.id];
    const blocked = steps.slice(0, i).filter((x) => x.status !== "approved").map((x) => x.id);
    return [
      { label: "Open in editor", run: () => onSelect(`step:${s.id}`) },
      { label: "Details", run: () => setDetails(`step:${s.id}`) },
      { label: "Edit settings…", run: () => onEditStep(s.id), ...lock },
      "-",
      { label: out ? "Preview again" : "Preview this step", run: () => preview(s), ...lock },
      s.status !== "approved" && {
        label: `Approve ${s.id}`, run: () => approve(s), ...lock,
        ...(blocked.length ? off(`Approve ${blocked.join(", ")} first`) : !out ? off("Preview it first") : out.error ? off("Its last preview failed") : {}),
      },
      "-",
      { label: "Insert step before…", run: () => addAt({ position: i }), ...lock, ...(canAdd ? {} : off(addWhy)) },
      { label: "Insert step after…", run: () => addAt({ position: i + 1 }), ...lock, ...(canAdd ? {} : off(addWhy)) },
      { label: "Move earlier", run: () => placeAt(s, i - 1), ...lock, ...(i === 0 ? off("Already first") : {}) },
      { label: "Move later", run: () => placeAt(s, i + 1), ...lock, ...(i === steps.length - 1 ? off("Already last") : {}) },
      { label: "Ask the agent about it…", run: () => onAskAgent(`About step ${label(s)}: `), ...lock },
      "-",
      { label: "Remove step", key: "Del", danger: true, run: () => removeStep(s), ...lock },
    ];
  }

  function menuItems(m) {
    if (m.kind === "pane") return [
      { label: "Add step here…", run: () => addAt({ position: steps.length, at: m.flow }), ...lock, ...(canAdd ? {} : off(addWhy)) },
      { label: "Ask the agent…", run: () => onAskAgent(""), ...lock },
      "-",
      { label: "Fit to view", run: () => flow.fitView({ padding: 0.18, maxZoom: 1, duration: 200 }) },
      { label: "Tidy layout", run: tidy, ...(Object.keys(layout).length ? {} : off("Nothing has been moved")) },
    ];
    if (m.kind === "edge") {
      const e = edges.find((x) => x.id === m.id);
      if (!e) return [];
      if (e.data.kind === "source") {
        const src = sourceOf(e.source), s = stepOf(e.target);
        return [
          { label: `${src.id} → ${s.id}: ${s.id} reads ${src.id}`, run: () => setDetails(e.source), disabled: false },
          "-",
          { label: "Disconnect", key: "Del", danger: true, run: () => unwire(src, s), ...lock },
        ];
      }
      const to = stepOf(e.target), from = stepOf(e.source);
      const index = to ? steps.indexOf(to) : from ? steps.indexOf(from) + 1 : 0;
      return [
        { label: "Insert step here…", run: () => addAt({ position: index }), ...lock, ...(canAdd ? {} : off(addWhy)) },
        { label: "Run order: drag either end of this wire to reorder", disabled: true },
      ];
    }
    const id = m.id;
    const s = stepOf(id);
    if (s) return stepItems(s);
    const src = sourceOf(id);
    if (src) return [
      { label: "Details", run: () => setDetails(id) },
      { label: "Open in the source reader", run: () => onOpenSource(src.id) },
      "-",
      { label: "Remove source", key: "Del", danger: true, run: () => removeSource(src), ...lock },
    ];
    if (id === "output") return [
      { label: "Open output", run: () => onSelect("output") },
      { label: "Details", run: () => setDetails("output") },
      { label: "Run the whole pipeline", run: () => send("POST", "/run", {}), ...lock, ...(steps.length ? {} : off("No steps yet")) },
    ];
    if (id === "gaps") return [
      { label: "Details", run: () => setDetails("gaps") },
      { label: "Add a step to fill them…", run: () => addAt({ position: steps.length }), ...lock, ...(canAdd ? {} : off(addWhy)) },
      { label: "Ask the agent to fill them…", run: () => onAskAgent(`No step produces ${gaps.join(", ")}. Add the steps that fill ${gaps.length > 1 ? "them" : "it"}.`), ...lock },
    ];
    if (id === "steps") return [
      { label: "Add step…", run: () => addAt({ position: 0 }), ...lock, ...(canAdd ? {} : off(addWhy)) },
      { label: "Ask the agent to outline the steps…", run: () => onAskAgent("Outline the steps for this pipeline."), ...lock },
    ];
    if (id === "sources") return [{ label: "Add sources", run: () => onOpenSource(null) }];
    return [];
  }

  function detailActions(id) {
    const s = stepOf(id);
    if (s) {
      const items = stepItems(s).filter((x) => x && x !== "-" && ["Open in editor", "Edit settings…", "Preview this step", "Preview again"].includes(x.label));
      return items.map((x, k) => ({ ...x, primary: k === 0 }));
    }
    const src = sourceOf(id);
    if (src) return [{ label: "Open in the source reader", run: () => onOpenSource(src.id) }];
    if (id === "output") return [{ label: "Open output", run: () => onSelect("output"), primary: true }];
    return [];
  }

  const approved = steps.filter((s) => s.status === "approved").length;
  return (
    <div className={"pl-canvas" + (busy ? " busy" : "") + (tall ? " tall" : "")}>
      <div className="pl-canvas-bar">
        <b>Pipeline</b>
        <span className="assess">{steps.length ? `${approved}/${steps.length} steps approved` : "no steps yet"}</span>
        <span className="spacer" />
        <button className="pl-bar-btn" disabled={disabled || !canAdd} title={canAdd ? "Add a step at the end" : addWhy}
                onClick={() => addAt({ position: steps.length })}>+ Add step</button>
        <button className="pl-bar-btn" disabled={disabled} onClick={() => onAskAgent("")}>Ask the agent</button>
        <button className="pl-bar-btn" title="Put every node back in run order" disabled={!Object.keys(layout).length} onClick={tidy}>Tidy</button>
        <button className="pl-bar-btn" title={tall ? "Shrink the canvas" : "Make the canvas taller"} onClick={() => setTall(!tall)}>{tall ? "Shrink" : "Expand"}</button>
      </div>
      <div className="pl-flow" ref={wrap}>
        <ReactFlow
          nodes={nodes} edges={shownEdges} nodeTypes={NODE_TYPES}
          onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
          onNodeClick={(_e, n) => { setMenu(null); onSelect(n.id); }}
          onNodeDoubleClick={(_e, n) => {
            if (stepOf(n.id) && editable) onEditStep(n.id.slice(5));
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
          onBeforeDelete={onBeforeDelete} deleteKeyCode={editable ? ["Delete", "Backspace"] : null}
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
        <div className="pl-flow-hint">Drag a node’s right-hand dot to rewire · right-click for more · Ctrl+scroll to zoom</div>
        {notice && <div className="pl-toast" role="status" onClick={() => setNotice(null)}>{notice.text}</div>}
        {menu && <Menu menu={menu} items={menuItems(menu)} onClose={() => setMenu(null)} />}
        {details && <DetailsPane id={details} view={view} onClose={() => setDetails(null)} actions={detailActions(details)} />}
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
