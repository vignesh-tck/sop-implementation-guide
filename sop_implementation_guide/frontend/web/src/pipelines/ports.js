// The steps as a graph, and each step's ports, shared by the canvas and the
// step editor. While a draft is open the canvas shows the draft; the step
// editor below it reviews the applied steps.

const ROWS_TEXT = {
  creates: "creates records from the sources",
  adds: "adds records from its sources to those it receives",
  keeps: "keeps every record",
  filters: "may drop records",
  merges: "merges duplicate records",
};

export const STATE_TEXT = { text: "unparsed text", set: "filled" };

// { draft, steps, sources, inputs: {id: [ids]}, outputFrom, order, ports }
export function stepsModel(view, useDraft = !!view.draft) {
  if (useDraft && view.draft) {
    const steps = view.draft.steps || [];
    const order = view.draft.check?.order?.length ? view.draft.check.order : steps.map((s) => s.id);
    return { draft: true, steps, sources: view.draft.sources || view.definition.sources || [],
             inputs: Object.fromEntries(steps.map((s) => [s.id, s.inputs || []])),
             outputFrom: view.draft.output_from || [], order, ports: view.draft.ports || {} };
  }
  const steps = view.definition.steps || [];
  const ids = steps.map((s) => s.id);
  return {
    draft: false, steps, sources: view.definition.sources || [],
    inputs: view.graph?.inputs || Object.fromEntries(ids.map((id, i) => [id, i ? [ids[i - 1]] : []])),
    outputFrom: view.graph?.output_from || ids.slice(-1),
    order: view.graph?.order || ids,
    ports: view.ports || {},
  };
}

export function ancestorsOf(model, id) {
  const seen = new Set();
  const todo = [...(model.inputs[id] || [])];
  while (todo.length) {
    const i = todo.pop();
    if (!seen.has(i)) { seen.add(i); todo.push(...(model.inputs[i] || [])); }
  }
  return seen;
}

const cnt = (o) => (o && !o.error ? o.count : null);

// The applied step's ports, with counts from the last previews.
export function stepPorts(view, stepId) {
  const m = stepsModel(view, false);
  const step = m.steps.find((s) => s.id === stepId);
  const ins = m.inputs[stepId] || [];
  const io = m.ports[stepId] || {};
  return {
    step, ins, io,
    rows: step?.type === "extract" && ins.length ? "adds" : io.rows,
    reads: step?.type === "extract" ? (step.config?.source_ids?.length ? step.config.source_ids.join(", ") : "every source") : null,
    inputs: ins.map((i) => [i, view.outputs[i]]),
    output: view.outputs[stepId],
  };
}

export function portText(view, stepId) {
  const p = stepPorts(view, stepId);
  const inRecs = p.inputs.map(([i, o]) => (cnt(o) != null ? `${cnt(o)} from ${i}` : `${i} (not previewed)`)).join(" + ");
  return {
    input: [inRecs && `${inRecs}${p.inputs.length > 1 ? (p.io.joins?.length ? " (each its own table)" : " (merged)") : ""}`, p.reads && `sources: ${p.reads}`].filter(Boolean).join(" + ") || "nothing",
    output: `${cnt(p.output) != null ? `${cnt(p.output)} record(s)` : "not previewed"} · ${ROWS_TEXT[p.rows] || ""}`,
    writes: p.io.writes || [],
    tables: p.io.tables || [],
    joins: p.io.joins || [],
    error: p.io.error,
  };
}

// The draft step's ports: no records yet, so the schema each port carries, from the check.
export function draftPortText(view, stepId) {
  const m = stepsModel(view, true);
  const step = m.steps.find((s) => s.id === stepId);
  const ins = m.inputs[stepId] || [];
  const io = m.ports[stepId] || {};
  const reads = step?.type === "extract" ? (step.config?.source_ids?.length ? step.config.source_ids.join(", ") : "every source") : null;
  const feeds = m.steps.filter((s) => (s.inputs || []).includes(stepId)).map((s) => s.id).concat(m.outputFrom.includes(stepId) ? ["Output"] : []);
  return {
    input: [ins.length && `${ins.join(" + ")}${ins.length > 1 ? (io.joins?.length ? " (each its own table)" : " (merged)") : ""}`, reads && `sources: ${reads}`].filter(Boolean).join(" + ") || "nothing wired in",
    output: feeds.length ? `to ${feeds.join(", ")}` : "not wired out",
    writes: io.writes || [],
    tables: io.tables || [],
    joins: io.joins || [],
    error: io.error,
    schema: view.draft?.check?.ports?.[stepId] || null,
    inSchema: ins.length ? view.draft?.check?.ports?.[ins[0]] || null : null,
  };
}
