import { useEffect, useState } from "react";
import { call } from "../api.js";
import PlanCanvas from "../blocks/plan/PlanCanvas.jsx";
import { ACTORS, ACTOR_KEYS } from "../blocks/plan/actors.jsx";
import { afterInput, nextId, numbers, parseAfter, topoOrder, wouldLoop } from "../blocks/plan/graph.js";

// Edit the playbooks every implementation plan starts from: each playbook's
// steps (who does each, how, how long, what it waits on), drawn as a canvas
// above the table, and which recommendation types belong to it. A save adds a
// new version; older versions stay restorable. Plans nobody has approved
// follow the new version at once; approved plans keep their own steps.

const NEW_STEP = { title: "", how: "", actor: "city", owner: "", time: "", depends_on: [], filled_by: "template" };

// "After" as step numbers ("2, 4" or "3 or 5"), applied when the field is left,
// so typing "3 o" on the way to "3 or 5" doesn't rewire anything.
function AfterInput({ step, steps, onCommit }) {
  const shown = afterInput(step, numbers(steps));
  const [text, setText] = useState(shown);
  useEffect(() => setText(shown), [shown]);
  const commit = () => {
    const next = parseAfter(text, steps, step.id);
    const cleared = steps.map((x) => (x.id === step.id ? { ...x, depends_on: [] } : x));
    if (next.depends_on.some((d) => wouldLoop(cleared, d, step.id))) {
      setText(shown);
      window.alert("That would make steps wait on each other in a loop.");
      return;
    }
    if (afterInput({ ...step, ...next }, numbers(steps)) !== shown) onCommit(next);
    else setText(shown);
  };
  return (
    <input value={text} placeholder="start" title="Step numbers this one waits on, e.g. 2, 4 or 3 or 5"
           onChange={(e) => setText(e.target.value)} onBlur={commit} onKeyDown={(e) => e.key === "Enter" && commit()} />
  );
}
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
  const sources = Object.entries(data.filled_by);
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

  function changeSteps(fn) {
    edit((d) => ({ ...d, pathways: d.pathways.map((p) => (p.key === selectedKey ? { ...p, steps: fn(p.steps) } : p)) }));
  }

  function changeStep(i, field, value) {
    changeSteps((steps) => steps.map((s, j) => (j === i ? { ...s, [field]: value } : s)));
  }

  function changeAfter(i, next) {
    changeSteps((steps) => topoOrder(steps.map((s, j) => (j === i ? { ...s, ...next } : s))));
  }

  function addStep() {
    changeSteps((steps) => {
      const last = steps[steps.length - 1];
      return [...steps, { ...NEW_STEP, id: nextId(steps), depends_on: last ? [last.id] : [] }];
    });
  }

  // Steps after a removed one take over what it waited on, so the chain stays joined.
  function removeStep(i) {
    changeSteps((steps) => {
      const gone = steps[i];
      return steps.filter((_, j) => j !== i).map((s) => (
        (s.depends_on || []).includes(gone.id)
          ? { ...s, depends_on: [...new Set([...(s.depends_on || []).filter((d) => d !== gone.id), ...(gone.depends_on || [])])] }
          : s
      ));
    });
  }

  function moveStep(i, delta) {
    changeSteps((steps) => {
      const j = i + delta;
      if (j < 0 || j >= steps.length) return steps;
      const next = [...steps];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
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

                <details className="pathway-canvas" open>
                  <summary>Canvas</summary>
                  <PlanCanvas steps={pathway.steps} />
                </details>

                <div className="table-wrap">
                  <table className="plan-table">
                    <thead>
                      <tr><th>#</th><th>Step and how it is done</th><th>Who</th><th>Who exactly</th><th>After</th><th>Only if</th><th>Time</th><th>Source</th><th /></tr>
                    </thead>
                    <tbody>
                      {pathway.steps.map((s, i) => (
                        <tr key={s.id || i}>
                          <td className="n">{i + 1}</td>
                          <td>
                            <textarea rows={2} value={s.title || ""} placeholder="What has to happen" onChange={(e) => changeStep(i, "title", e.target.value)} />
                            <textarea rows={2} value={s.how || ""} placeholder="How it is done" className="how" onChange={(e) => changeStep(i, "how", e.target.value)} />
                          </td>
                          <td>
                            <select value={ACTORS[s.actor] ? s.actor : "other"} onChange={(e) => changeStep(i, "actor", e.target.value)}
                                    title={ACTORS[s.actor]?.desc}>
                              {ACTOR_KEYS.map((k) => <option key={k} value={k}>{ACTORS[k].label}</option>)}
                            </select>
                          </td>
                          <td><input value={s.owner || ""} onChange={(e) => changeStep(i, "owner", e.target.value)} /></td>
                          <td className="after"><AfterInput step={s} steps={pathway.steps} onCommit={(next) => changeAfter(i, next)} /></td>
                          <td><input value={s.condition || ""} placeholder="always" onChange={(e) => changeStep(i, "condition", e.target.value)} /></td>
                          <td><input value={s.time || ""} onChange={(e) => changeStep(i, "time", e.target.value)} /></td>
                          <td>
                            <select value={s.filled_by || "template"} onChange={(e) => changeStep(i, "filled_by", e.target.value)}
                              title={data.filled_by[s.filled_by]?.note}>
                              {sources.map(([k, src]) => (
                                <option key={k} value={k}>{src.label}{src.live ? "" : " (not built, SME fills)"}</option>
                              ))}
                            </select>
                          </td>
                          <td className="row-actions">
                            <button type="button" title="Move up" onClick={() => moveStep(i, -1)} disabled={i === 0}>↑</button>
                            <button type="button" title="Move down" onClick={() => moveStep(i, 1)} disabled={i === pathway.steps.length - 1}>↓</button>
                            <button type="button" title="Remove step" onClick={() => removeStep(i)}>✕</button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="kv plan-meta">
                  Source is where a step's content comes from: an agent, a lookup, or the template's own wording.
                  A source marked "not built" shows as Needs SME on every plan until it exists.
                  "After" is the step numbers a step waits on ("2, 4", or "3 or 5" for whichever is first); links follow a
                  step when you move it. "Only if" makes a step a branch that happens in some cases.
                </div>
                <div className="btn-row">
                  <button onClick={addStep}>+ Add step</button>
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
