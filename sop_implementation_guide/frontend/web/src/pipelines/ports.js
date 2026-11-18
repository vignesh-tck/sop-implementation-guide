// A step's ports as text and numbers, shared by the step editor and the canvas.

const ROWS_TEXT = {
  creates: "creates records from the sources",
  adds: "adds records from the sources to those before it",
  keeps: "keeps every record",
  filters: "may drop records",
  merges: "merges duplicate records",
};

export function stepPorts(view, stepId) {
  const steps = view.definition.steps || [];
  const i = steps.findIndex((s) => s.id === stepId);
  const step = steps[i];
  const prev = i > 0 ? steps[i - 1] : null;
  const io = view.ports?.[stepId] || {};
  const rows = step?.type === "extract" && prev ? "adds" : io.rows;
  const reads = step?.type === "extract" ? (step.config?.source_ids?.length ? step.config.source_ids.join(", ") : "every source") : null;
  return { i, step, prev, io, rows, reads, input: prev ? view.outputs[prev.id] : null, output: view.outputs[stepId] };
}

const cnt = (o) => (o && !o.error ? o.count : null);

export function portText(view, stepId) {
  const p = stepPorts(view, stepId);
  const inRecs = p.prev ? (cnt(p.input) != null ? `${cnt(p.input)} record(s) from ${p.prev.id}` : `${p.prev.id}'s records (not previewed)`) : null;
  return {
    input: [inRecs, p.reads && `sources: ${p.reads}`].filter(Boolean).join(" + ") || "nothing before it",
    output: `${cnt(p.output) != null ? `${cnt(p.output)} record(s)` : "not previewed"} · ${ROWS_TEXT[p.rows] || ""}`,
    writes: p.io.writes || [],
    tables: p.io.tables || [],
    error: p.io.error,
  };
}
