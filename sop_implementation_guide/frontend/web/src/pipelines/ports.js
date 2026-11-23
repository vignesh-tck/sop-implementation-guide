// The steps as a graph, and each step's ports, shared by the canvas and the
// step editor. While a draft is open the canvas shows the draft; the step
// editor below it reviews the applied steps.

const ROWS_TEXT = {
  creates: "creates records",
  adds: "adds records from its sources to those it receives",
  keeps: "keeps every record",
  filters: "may drop records",
  merges: "merges duplicate records",
};

export const STATE_TEXT = { text: "unparsed text", set: "filled" };

// { draft, steps, sources, inputs: {id: [ids]}, outputFrom, outputsFrom: {output id: [ids]}, order, ports }
// outputFrom is the first output's inputs; outputsFrom has every output's (one entry for most pipelines).
export function stepsModel(view, useDraft = !!view.draft) {
  if (useDraft && view.draft) {
    const steps = view.draft.steps || [];
    const order = view.draft.check?.order?.length ? view.draft.check.order : steps.map((s) => s.id);
    const outputFrom = view.draft.output_from || [];
    return { draft: true, steps, sources: view.draft.sources || view.definition.sources || [],
             inputs: Object.fromEntries(steps.map((s) => [s.id, s.inputs || []])),
             outputFrom, outputsFrom: { output: outputFrom, ...(view.draft.outputs_from || {}) },
             order, ports: view.draft.ports || {} };
  }
  const steps = view.definition.steps || [];
  const ids = steps.map((s) => s.id);
  const outputFrom = view.graph?.output_from || ids.slice(-1);
  return {
    draft: false, steps, sources: view.definition.sources || [],
    inputs: view.graph?.inputs || Object.fromEntries(ids.map((id, i) => [id, i ? [ids[i - 1]] : []])),
    outputFrom, outputsFrom: { output: outputFrom, ...(view.graph?.outputs_from || {}) },
    order: view.graph?.order || ids,
    ports: view.ports || {},
  };
}

// ── Several outputs ──────────────────────────────────────────────────────────
//
// A pipeline can have several Output nodes (a zoning pipeline: block zones, use
// permissions, readings), each with its own schema, last run, records, mapping,
// overrides and review. The first is "output"; the others o2, o3 ... Each step
// feeds one output and works in its schema. Most pipelines have one output, and
// then every helper here hands the view through unchanged.

export const isMulti = (view) => (view.output_list || []).length > 1;

export function outputList(view) {
  return view.output_list?.length ? view.output_list
    : [{ id: "output", name: "Output", schema: view.definition.schema, last_run: view.last_run, gate: view.gate }];
}

// The canvas node of an output, and the output of a canvas node.
export const outputNode = (oid) => (oid === "output" ? "output" : `output:${oid}`);
export const nodeOutput = (nodeId) => (nodeId === "output" ? "output" : nodeId?.startsWith("output:") ? nodeId.slice(7) : null);

// Which output a step feeds (the first, when it reaches none yet).
export function ownerOf(view, stepId, useDraft = !!view.draft) {
  if (!isMulti(view)) return "output";
  const m = stepsModel(view, useDraft);
  for (const [oid, froms] of Object.entries(m.outputsFrom)) {
    for (const f of froms) if (f === stepId || ancestorsOf(m, f).has(stepId)) return oid;
  }
  return "output";
}

// The view as one output sees it: its schema, last run, records, mapping, overrides, review and gate.
export function outputView(view, oid = "output") {
  if (!isMulti(view)) return view;
  const o = view.output_list.find((x) => x.id === oid) || view.output_list[0];
  return {
    ...view, output_id: o.id, output_name: o.name, target_label: o.target_label,
    definition: { ...view.definition, schema: o.schema, overrides: o.overrides },
    last_run: o.last_run, last_records: o.last_records || [], load_map: o.load_map, write: o.write, gate: o.gate,
    output_locked: o.output_locked, overrides: o.overrides, review_marks: o.review_marks || {},
    design_gaps: o.design_gaps || [], has_defaults: o.has_defaults,
  };
}

export const stepView = (view, stepId) => outputView(view, ownerOf(view, stepId));

// act for one output: its schema, mapping, records and overrides take ?output=.
export function outputAct(act, oid) {
  if (!oid || oid === "output") return act;
  return (method, path, body, opts) => act(method, `${path}${path.includes("?") ? "&" : "?"}output=${encodeURIComponent(oid)}`, body, opts);
}

// Outputs whose schema still needs approving; the design waits for all of them.
export const unapprovedOutputs = (view) => outputList(view).filter((o) => o.schema?.status !== "approved");

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
  const names = Object.fromEntries(outputList(view).map((o) => [o.id, isMulti(view) ? o.name : "Output"]));
  const feeds = m.steps.filter((s) => (s.inputs || []).includes(stepId)).map((s) => s.id)
    .concat(Object.entries(m.outputsFrom).filter(([, f]) => f.includes(stepId)).map(([oid]) => names[oid] || oid));
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
