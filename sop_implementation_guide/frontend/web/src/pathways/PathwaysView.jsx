import { useEffect, useState } from "react";
import { call } from "../api.js";
import PlanCanvas from "../blocks/plan/PlanCanvas.jsx";
import StepInspector from "../blocks/plan/StepInspector.jsx";
import { useStepEdits } from "../blocks/plan/useStepEdits.js";

// Edit the playbooks every implementation plan starts from: each playbook's
// steps on a canvas (click a step to edit it in the panel beside it; drag
// between steps to link them), and which recommendation types belong to it. A save adds a
// new version; older versions stay restorable. Plans nobody has approved
// follow the new version at once; approved plans keep their own steps.

const NEW_STEP = { title: "", how: "", actor: "city", owner: "", time: "", depends_on: [], filled_by: "template" };
const NO_STEPS = [];

const REVIEWER_KEY = "sop.pathways.reviewer";

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

function readReviewer() {
  try { return localStorage.getItem(REVIEWER_KEY) || ""; } catch { return ""; }
}

export default function PathwaysView() {
  const [data, setData] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [draft, setDraft] = useState(null);
  const [selectedKey, setSelectedKey] = useState(null);
  const [reviewer, setReviewer] = useState(readReviewer);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [saved, setSaved] = useState(null);
  const [newName, setNewName] = useState("");
  const [stepError, setStepError] = useState(null);
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
  useEffect(() => { setSelectedStep(null); setStepError(null); }, [selectedKey, data, setSelectedStep]);

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

  useEffect(() => {
    try { localStorage.setItem(REVIEWER_KEY, reviewer); } catch { /* per-viewer convenience only */ }
  }, [reviewer]);

  if (loadError) return <main className="view"><div className="pathways-wrap"><div className="banner err">Could not load playbooks — {loadError}</div></div></main>;
  if (!data || !draft) return <main className="view"><div className="pathways-wrap"><div className="card spin">Loading playbooks…</div></div></main>;

  const dirty = JSON.stringify(draft) !== JSON.stringify(editable(data));
  const pathway = draft.pathways.find((p) => p.key === selectedKey) || null;
  const selStep = pathway?.steps.find((s) => s.id === stepEdits.selected) || null;
  const recsFor = (key) => Object.keys(draft.rec_types).filter((l) => draft.rec_types[l] === key).sort();
  // Types in the data with no pathway, plus any this edit has just unmapped.
  const allLabels = [...new Set([...data.unmapped_rec_labels, ...Object.keys(data.rec_types)])];
  const unmapped = allLabels.filter((l) => !draft.rec_types[l]).sort();

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

  function addPathway() {
    const name = newName.trim();
    if (!name) return;
    const key = slug(name, draft.pathways.map((p) => p.key));
    edit((d) => ({ ...d, pathways: [...d.pathways, { key, name, who: "", branch: "", steps: [{ ...NEW_STEP, id: "s1" }] }] }));
    setSelectedKey(key);
    setNewName("");
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
    setSelectedKey(draft.pathways.find((p) => p.key !== selectedKey)?.key ?? null);
  }

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
              {draft.pathways.map((p) => (
                <li key={p.key} aria-selected={p.key === selectedKey} onClick={() => setSelectedKey(p.key)}>
                  <div className="name">{p.name || <i>Unnamed</i>}</div>
                  <div className="meta">{p.steps.length} steps · {recsFor(p.key).length} rec types</div>
                </li>
              ))}
            </ul>
            <div className="pathway-new">
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="New playbook name"
                onKeyDown={(e) => e.key === "Enter" && addPathway()} />
              <button onClick={addPathway} disabled={!newName.trim()}>Add</button>
            </div>

            <h4 className="history-head">History</h4>
            <ul className="pathway-history">
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
                <div className="pathway-fields">
                  <label>Name<input value={pathway.name} onChange={(e) => changePathway("name", e.target.value)} /></label>
                  <label>Who does the work<input value={pathway.who} onChange={(e) => changePathway("who", e.target.value)} /></label>
                  <label className="wide">
                    The deciding question
                    <textarea rows={2} value={pathway.branch} onChange={(e) => changePathway("branch", e.target.value)} />
                  </label>
                </div>

                <div className={"plan-canvas-wrap side editing" + (selStep ? " has-inspector" : "")}>
                  <PlanCanvas
                    steps={pathway.steps} selectedId={stepEdits.selected} onSelect={stepEdits.setSelected}
                    editable onConnect={stepEdits.connect} onDisconnect={stepEdits.disconnect}
                  />
                  {selStep ? (
                    <StepInspector
                      s={selStep} n={pathway.steps.indexOf(selStep) + 1} steps={pathway.steps} editable sources={data.filled_by}
                      onChange={(fields) => stepEdits.patch(selStep.id, fields)}
                      onDelete={() => stepEdits.remove(selStep.id)}
                      onAddAfter={() => stepEdits.addAfter(selStep.id)}
                      onSelect={stepEdits.setSelected}
                      onClose={() => stepEdits.setSelected(null)}
                    />
                  ) : (
                    <aside className="step-inspector si-empty">
                      <b>Click a step to edit it</b>
                      <p className="assess">
                        Each step has an activity, a description, what it depends on, a primary and a secondary owner, and
                        a potential duration.
                      </p>
                      <p className="assess">
                        To link steps, drag from the dot under a step to the step that waits on it. Click a line to remove the link.
                      </p>
                      <button type="button" onClick={() => stepEdits.addAfter(null)}>+ Add step</button>
                    </aside>
                  )}
                </div>
                {stepError && <div className="banner err" style={{ marginTop: ".6rem" }}>{stepError}</div>}

                <div className="btn-row">
                  <button onClick={() => stepEdits.addAfter(null)}>+ Add step</button>
                  <span className="spacer" />
                  <button onClick={removePathway}>Delete playbook</button>
                </div>

                <h4 className="recs-head">Recommendation types in this playbook</h4>
                <ul className="pathway-recs">
                  {recsFor(pathway.key).map((label) => (
                    <li key={label}>
                      <span>{label}</span>
                      <select value={pathway.key} onChange={(e) => mapRec(label, e.target.value)} title="Move to another playbook">
                        {draft.pathways.map((p) => <option key={p.key} value={p.key}>{p.name || p.key}</option>)}
                        <option value="">No playbook</option>
                      </select>
                    </li>
                  ))}
                </ul>
                {unmapped.length > 0 && (
                  <select value="" onChange={(e) => e.target.value && mapRec(e.target.value, pathway.key)}>
                    <option value="">+ Add an unmapped recommendation type…</option>
                    {unmapped.map((l) => <option key={l} value={l}>{l}</option>)}
                  </select>
                )}
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
    </main>
  );
}
