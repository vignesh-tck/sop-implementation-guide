import { useEffect, useMemo, useRef, useState } from "react";
import dagre from "@dagrejs/dagre";
import { ConnectionLineType, Controls, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider, useReactFlow } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Outcome } from "../../zoning/ZoningParts.jsx";
import { ACTORS, ActorIcon, ClockIcon } from "./actors.jsx";
import { numbers } from "./graph.js";

// An implementation plan as a flowchart, top to bottom: one card per step
// (primary owner, activity, description, duration, secondary owner), with a line from each step to the steps that
// wait on it. A step that only happens in some cases hangs off a dashed,
// labelled line ("Code violation"), so the branches of a plan read at a glance.
//
// The canvas is laid out by dagre and sized to the panel: its height grows
// with the plan and it scales down (to half size at most) to fit the width,
// so it scrolls with the page instead of inside a box. `fill` is for full
// screen, where it fits the screen instead.
//
// Changes are drawn on top: marks give a step a "New" or "Changed" flag, and
// removed steps come back faded where they used to be. While editing, drag
// from a step's bottom handle to another step to make it wait on this one;
// click a line to remove that link.

const W = 216;
const H = 150;
const PAD = 22;

function StepNode({ data }) {
  const { s, n, mark, removed, sel, badge, editable } = data;
  const kind = ACTORS[s.actor] ? s.actor : "other";
  const flag = removed ? <span className="pc-flag gone">Removed</span>
    : mark?.kind === "new" ? <span className="pc-flag new">New</span>
    : mark?.kind === "changed" ? (
      <span className="pc-flag changed" title={mark.fields.map((f) => `${f.label}: ${f.before || "—"} → ${f.after || "—"}`).join("\n")}>
        Changed
      </span>
    ) : null;
  const status = removed || !badge ? null
    : badge.sme ? <span className="pc-flag sme">Needs SME</span>
    : badge.zoning ? <Outcome outcome={badge.zoning} />
    : badge.funding ? <span className="pc-flag ok">{badge.funding} funding</span>
    : null;
  return (
    <div className={`pc-node actor-${kind}${sel ? " sel" : ""}${mark ? " mark-" + mark.kind : ""}${removed ? " removed" : ""}`}>
      <Handle type="target" position={Position.Top} className="pc-handle" isConnectable={editable && !removed} />
      <div className="pc-head">
        <span className="pc-actor"><ActorIcon kind={kind} size={15} />{ACTORS[kind].label}</span>
        <span className="pc-n">{n}</span>
        <span className="spacer" />
        {flag}{status}
      </div>
      <div className="pc-title">{s.title || <i>Untitled step</i>}</div>
      {s.how && <div className="pc-how">{s.how}</div>}
      <div className="pc-foot">
        <span className="pc-time"><ClockIcon />{s.time || "No set time"}</span>
        {(s.owner || s.secondary_owner) && (
          <span className="pc-owner" title={[s.owner, s.secondary_owner && `Secondary: ${s.secondary_owner}`].filter(Boolean).join(" · ")}>
            {s.owner}{s.secondary_owner && <span className="pc-second"> + {s.secondary_owner.replace(/ reviews.*$/i, "")}</span>}
          </span>
        )}
      </div>
      <Handle type="source" position={Position.Bottom} className="pc-handle" isConnectable={editable && !removed} />
    </div>
  );
}

const NODE_TYPES = { step: StepNode };

function layout(steps, removed) {
  const all = [...steps, ...removed.map((r) => ({ ...r, _removed: true }))];
  const ids = new Set(all.map((s) => s.id));
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: 22, ranksep: 48, marginx: 0, marginy: 0 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const s of all) g.setNode(s.id, { width: W, height: H });
  for (const s of all) for (const d of s.depends_on || []) if (ids.has(d)) g.setEdge(d, s.id);
  dagre.layout(g);
  const pos = {};
  for (const s of all) {
    const n = g.node(s.id);
    pos[s.id] = { x: n.x - W / 2, y: n.y - H / 2 };
  }
  const { width = W, height = H } = g.graph();
  return { all, ids, pos, width: Math.max(width, W), height: Math.max(height, H) };
}

const NO_MARKS = {};
const NONE = [];

function Flow({ steps, marks = NO_MARKS, removed = NONE, selectedId, onSelect, editable, onConnect, onDisconnect, badge, fill }) {
  const wrap = useRef(null);
  const flow = useReactFlow();
  const [box, setBox] = useState({ w: 0, h: 0 });

  useEffect(() => {
    const el = wrap.current;
    if (!el) return undefined;
    const ro = new ResizeObserver(([e]) => setBox({ w: e.contentRect.width, h: e.contentRect.height }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Laid out again only when the links change, not on every edit to a step's text.
  const shape = JSON.stringify([...steps, ...removed].map((s) => [s.id, s.depends_on || []]));
  const placed = useMemo(() => layout(steps, removed), [shape]); // eslint-disable-line react-hooks/exhaustive-deps
  const lay = { ...placed, all: [...steps, ...removed.map((r) => ({ ...r, _removed: true }))] };
  const num = useMemo(() => numbers(steps), [steps]);

  const fitW = box.w ? (box.w - PAD * 2) / lay.width : 1;
  const zoom = fill
    ? Math.min(1, Math.max(0.35, Math.min(fitW, box.h ? (box.h - PAD * 2) / lay.height : 1)))
    : Math.min(1, Math.max(0.5, fitW));
  const height = fill ? "100%" : Math.round(lay.height * zoom + PAD * 2);

  useEffect(() => {
    if (!box.w) return;
    flow.setViewport({ x: Math.max(PAD, (box.w - placed.width * zoom) / 2), y: PAD, zoom });
  }, [box.w, box.h, placed, zoom, flow]);

  const nodes = lay.all.map((s) => ({
    id: s.id, type: "step", position: lay.pos[s.id], draggable: false, selectable: false,
    data: { s, n: s._removed ? s.n : num[s.id], mark: marks[s.id], removed: !!s._removed, sel: s.id === selectedId,
            badge: badge ? badge(s) : null, editable },
  }));

  const edges = [];
  for (const s of lay.all) {
    const deps = (s.depends_on || []).filter((d) => lay.ids.has(d));
    deps.forEach((d, k) => {
      const cond = !!s.condition, gone = !!s._removed || lay.all.find((x) => x.id === d)?._removed;
      const label = cond ? s.condition : s.depends_mode === "any" && deps.length > 1 && k === 0 ? "whichever is first" : undefined;
      const colour = gone ? "var(--red)" : cond ? "var(--amber)" : "var(--faint)";
      edges.push({
        id: `${d}>${s.id}`, source: d, target: s.id, type: "smoothstep",
        className: "pc-edge" + (cond ? " cond" : "") + (gone ? " gone" : "") + (s.depends_mode === "any" ? " any" : ""),
        label, labelBgPadding: [5, 2], labelBgBorderRadius: 4,
        labelStyle: { fontSize: 11, fontWeight: 600, fill: cond ? "var(--amber)" : "var(--muted)" },
        labelBgStyle: { fill: "var(--surface)" },
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: colour },
        interactionWidth: editable ? 18 : 0,
      });
    });
  }

  return (
    <div className="pc-flow" ref={wrap} style={{ height }}>
      <ReactFlow
        nodes={nodes} edges={edges} nodeTypes={NODE_TYPES}
        onNodeClick={(_e, n) => onSelect?.(n.id)}
        onPaneClick={() => onSelect?.(null)}
        onConnect={editable ? ({ source, target }) => onConnect?.(source, target) : undefined}
        onEdgeClick={editable ? (_e, ed) => onDisconnect?.(ed.source, ed.target) : undefined}
        nodesDraggable={false} nodesConnectable={!!editable} elementsSelectable={false} deleteKeyCode={null}
        panOnDrag zoomOnScroll={false} zoomOnDoubleClick={false} preventScrolling={false}
        minZoom={0.3} maxZoom={1.5}
        connectionLineType={ConnectionLineType.SmoothStep} connectionRadius={30} proOptions={{ hideAttribution: true }}
      >
        <Controls showInteractive={false} position="bottom-left" />
      </ReactFlow>
    </div>
  );
}

export default function PlanCanvas(props) {
  return (
    <ReactFlowProvider>
      <Flow {...props} />
    </ReactFlowProvider>
  );
}
