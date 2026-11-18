import { useEffect, useLayoutEffect, useRef, useState } from "react";

// A small node map of the pipeline: sources → steps → output. Clicking a node
// selects it; the panel below shows that part for editing.
// Nodes are laid out with CSS; the connecting lines are measured from the DOM
// after each render and drawn in one SVG behind them.

const MAX_SOURCES = 6;

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

function Node({ id, reg, sel, onSelect, status, kind, title, sub, meta, model, running, ghost }) {
  return (
    <button type="button" ref={(el) => reg(id, el)} onClick={() => onSelect(id)}
            className={`pl-node ${status}${sel === id ? " sel" : ""}${running ? " running" : ""}${ghost ? " ghost" : ""}`}
            title={STATUS_TEXT[status] ? `${title}: ${STATUS_TEXT[status]}` : title}>
      <span className="pl-node-kind">
        <span className="pl-node-dot" />{kind}
        {model && <span className="pl-node-model" title="Calls the model">model</span>}
      </span>
      <span className="pl-node-title">{title}</span>
      {sub && <span className="pl-node-sub">{sub}</span>}
      {meta && <span className="pl-node-meta">{meta}</span>}
    </button>
  );
}

export default function PipelineCanvas({ view, sel, onSelect }) {
  const d = view.definition;
  const sources = d.sources || [];
  const steps = d.steps || [];
  const gaps = steps.length ? view.design_gaps || [] : [];
  const busy = !!view.busy;
  const run = view.last_run;

  // Which step the server is on, from progress lines like "Running s2 (parse)".
  // Sub-step lines ("Reading 4 page(s)…") keep the last step seen.
  const [running, setRunning] = useState(null);
  const progress = busy ? view.progress || "" : null;
  const [seenProgress, setSeenProgress] = useState(undefined);   // undefined: read it on the first render
  if (progress !== seenProgress) {
    setSeenProgress(progress);
    const m = /^Running (\S+) /.exec(progress || "");
    setRunning(progress === null ? null : m ? m[1] : /^Reading \d+ source/.test(progress) ? "sources" : running);
  }

  // ── what to draw ───────────────────────────────────────────────────────────
  const shownSources = sources.slice(0, MAX_SOURCES);
  const hidden = sources.slice(MAX_SOURCES).map((s) => s.id);
  const srcNode = (sid) => (hidden.includes(sid) ? "src:+more" : `src:${sid}`);
  const status = {};
  const edges = [];
  const edge = (from, to) => edges.push({ from, to });

  status.sources = d.sources_status === "final" ? "ok" : sources.length ? "pending" : "idle";
  steps.forEach((s, i) => {
    status[s.id] = stepStatus(s, view.outputs[s.id]);
    if (i > 0) edge(steps[i - 1].id, s.id);
    if (s.type === "extract") {
      const ids = s.config?.source_ids?.length ? s.config.source_ids : sources.map((x) => x.id);
      [...new Set(ids.map(srcNode))].forEach((n) => edge(n, s.id));
    }
  });
  if (steps.length && !steps.some((s) => s.type === "extract")) edge("sources", steps[0].id);
  const last = steps.length ? steps[steps.length - 1].id : "steps";
  if (!steps.length) edge("sources", "steps");
  if (gaps.length) { edge(last, "gaps"); edge("gaps", "output"); } else edge(last, "output");
  for (const s of shownSources) status[`src:${s.id}`] = view.sources[s.id]?.error ? "bad" : status.sources;
  if (hidden.length) status["src:+more"] = status.sources;
  status.steps = "idle";
  status.gaps = "bad";
  status.output = run ? (run.upto ? "pending" : "ok") : "idle";

  // ── measuring the lines ────────────────────────────────────────────────────
  const viewport = useRef(null);
  const inner = useRef(null);
  const nodes = useRef({});
  const [paths, setPaths] = useState([]);
  const reg = (id, el) => { if (el) nodes.current[id] = el; else delete nodes.current[id]; };

  const measure = () => {
    const box = inner.current;
    if (!box) return;
    const b = box.getBoundingClientRect();
    const next = edges.map(({ from, to }) => {
      const a = nodes.current[from], c = nodes.current[to];
      if (!a || !c) return null;
      const ra = a.getBoundingClientRect(), rc = c.getBoundingClientRect();
      const x1 = ra.right - b.left, y1 = ra.top + ra.height / 2 - b.top;
      const x2 = rc.left - b.left, y2 = rc.top + rc.height / 2 - b.top;
      const dx = Math.max(18, (x2 - x1) / 2);
      const done = status[from] === "ok" && status[to] !== "bad";
      return { key: `${from}>${to}`, x2, y2, done,
               d: `M${x1.toFixed(1)},${y1.toFixed(1)} C${(x1 + dx).toFixed(1)},${y1.toFixed(1)} ${(x2 - dx).toFixed(1)},${y2.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}` };
    }).filter(Boolean);
    setPaths((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
  };
  const measureRef = useRef(null);
  useLayoutEffect(() => { measureRef.current = measure; measure(); });
  useEffect(() => {
    const ro = new ResizeObserver(() => measureRef.current?.());
    if (inner.current) ro.observe(inner.current);
    return () => ro.disconnect();
  }, []);

  // Keep the selected node in view when selection moves (e.g. after an approval).
  const selKey = sel?.startsWith("step:") ? sel.slice(5) : sel;
  useEffect(() => {
    const vp = viewport.current, el = nodes.current[selKey];
    if (!vp || !el) return;
    const v = vp.getBoundingClientRect(), r = el.getBoundingClientRect();
    if (r.left < v.left + 8) vp.scrollBy({ left: r.left - v.left - 40, behavior: "smooth" });
    else if (r.right > v.right - 8) vp.scrollBy({ left: r.right - v.right + 40, behavior: "smooth" });
  }, [selKey]);

  // Drag the background to pan; a vertical wheel scrolls sideways.
  const drag = useRef(null);
  const onPointerDown = (e) => {
    if (e.button !== 0 || e.target.closest("button, .pl-group")) return;
    drag.current = { x: e.clientX, left: viewport.current.scrollLeft };
    viewport.current.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e) => {
    if (drag.current) viewport.current.scrollLeft = drag.current.left - (e.clientX - drag.current.x);
  };
  const onPointerUp = () => { drag.current = null; };
  useEffect(() => {
    const vp = viewport.current;
    if (!vp) return;
    const onWheel = (e) => {
      if (Math.abs(e.deltaY) <= Math.abs(e.deltaX) || vp.scrollWidth <= vp.clientWidth) return;
      const atEdge = (e.deltaY < 0 && vp.scrollLeft <= 0) || (e.deltaY > 0 && vp.scrollLeft + vp.clientWidth >= vp.scrollWidth - 1);
      if (atEdge) return;   // let the page scroll on past the ends
      e.preventDefault();
      vp.scrollLeft += e.deltaY;
    };
    vp.addEventListener("wheel", onWheel, { passive: false });
    return () => vp.removeEventListener("wheel", onWheel);
  }, []);

  const pick = (id) => onSelect(steps.some((s) => s.id === id) ? `step:${id}` : id);
  const common = { reg, sel: selKey, onSelect: pick };
  const approved = steps.filter((s) => s.status === "approved").length;

  return (
    <div className={"pl-canvas" + (busy ? " busy" : "")}>
      <div className="pl-canvas-bar">
        <b>Pipeline</b>
        <span className="assess">{steps.length ? `${approved}/${steps.length} steps approved` : "no steps yet"}</span>
        <span className="spacer" />
        <span className="pl-canvas-legend">
          <span className="ok">approved</span><span className="pending">needs approval</span><span className="bad">failed</span>
        </span>
      </div>
      <div className="pl-canvas-vp" ref={viewport} onPointerDown={onPointerDown} onPointerMove={onPointerMove}
           onPointerUp={onPointerUp} onPointerCancel={onPointerUp}>
        <div className="pl-canvas-inner" ref={inner}>
          <svg className="pl-edges" aria-hidden="true">
            {paths.map((p) => (
              <g key={p.key} className={"pl-edge" + (p.done ? " done" : "")}>
                <path d={p.d} />
                <circle cx={p.x2} cy={p.y2} r="2.6" />
              </g>
            ))}
          </svg>

          <div className="pl-col">
            <div ref={(el) => reg("sources", el)} role="button" tabIndex={0}
                 className={`pl-group ${status.sources}${selKey === "sources" ? " sel" : ""}${running === "sources" ? " running" : ""}`}
                 onClick={() => pick("sources")} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && pick("sources")}>
              <span className="pl-node-kind"><span className="pl-node-dot" />Sources
                <span className="pl-group-state">{d.sources_status === "final" ? "final" : sources.length ? "open" : ""}</span></span>
              {sources.length === 0 && <span className="pl-node-sub">add URLs or files</span>}
              {shownSources.map((s) => {
                const meta = view.sources[s.id] || {};
                return (
                  <span key={s.id} ref={(el) => reg(`src:${s.id}`, el)} className={`pl-chip ${status[`src:${s.id}`]}`}
                        title={meta.error || s.url || s.query || s.label}>
                    <b>{s.id}</b> {cut(s.label, 22)}
                  </span>
                );
              })}
              {hidden.length > 0 && <span ref={(el) => reg("src:+more", el)} className="pl-chip">+{hidden.length} more</span>}
            </div>
          </div>

          {steps.length === 0 && (
            <div className="pl-col">
              <Node id="steps" {...common} status="idle" ghost kind="Steps" title={busy && d.sources_status === "final" ? "Outlining…" : "No steps yet"}
                    sub={d.sources_status === "final" ? "the agent outlines them" : "after sources are final"} />
            </div>
          )}
          {steps.map((s) => {
            const cat = view.catalog[s.type] || {};
            const out = view.outputs[s.id];
            return (
              <div className="pl-col" key={s.id}>
                <Node id={s.id} {...common} status={status[s.id]} running={running === s.id} model={cat.model}
                      kind={s.id} title={cat.label || s.type} sub={stepSummary(s)}
                      meta={out?.error ? "error" : out ? `${out.count} record${out.count === 1 ? "" : "s"}` : "not previewed"} />
              </div>
            );
          })}
          {gaps.length > 0 && (
            <div className="pl-col">
              <Node id="gaps" {...common} status="bad" ghost kind="Missing" title="No step fills"
                    sub={cut(gaps.join(", "), 40)} meta="ask the agent" />
            </div>
          )}

          <div className="pl-col">
            <Node id="output" {...common} status={status.output} kind="Output" title={run && !run.upto ? "Final output" : "Output"}
                  sub={run ? `${run.coverage?.records ?? 0} records` : "no run yet"}
                  meta={run?.upto ? `preview to ${run.upto}` : run ? (run.at || "").slice(5, 16).replace("T", " ") : null} />
          </div>
        </div>
      </div>
    </div>
  );
}
