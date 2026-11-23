import { useEffect, useState } from "react";
import { call } from "../api.js";
import PlanCanvas from "../blocks/plan/PlanCanvas.jsx";
import StepInspector from "../blocks/plan/StepInspector.jsx";
import { ACTORS } from "../blocks/plan/actors.jsx";
import { diffSteps, marksFrom } from "../blocks/plan/graph.js";
import { useStepEdits } from "../blocks/plan/useStepEdits.js";
import PlaybookAgent, { EMPTY_CHAT } from "./PlaybookAgent.jsx";

// Edit the playbooks every implementation plan starts from: each playbook's
// steps on a canvas (click a step to edit it in the panel beside it; drag
// between steps to link them), and which recommendation types belong to it. A
// save adds a new version; older versions stay restorable. Plans nobody has
// approved follow the new version at once; approved plans keep their own steps.
//
// The panel's Agent tab is a chat with the playbook agent. Its changes come
// back as a proposal, drawn here in place of the draft with what it changes
// marked, until the planner adds it to the draft (then saves as usual) or
// discards it. The chat and any pending proposal are kept in this browser.

const NEW_STEP = { title: "", how: "", actor: "city", owner: "", time: "", depends_on: [], filled_by: "template" };
const NO_STEPS = [];

const REVIEWER_KEY = "sop.pathways.reviewer";
const CHAT_KEY = "sop.playbooks.agent.chat";
const PROPOSAL_KEY = "sop.playbooks.agent.proposal";

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

function readStored(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v ? JSON.parse(v) : fallback;
  } catch {
    return fallback;
  }
}

function store(key, value) {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch { /* a per-viewer convenience only */ }
}

const OP_CLASS = { "+": "new", "~": "changed", "-": "gone", "→": "moved" };

export default function PathwaysView() {
  const [data, setData] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [draft, setDraft] = useState(null);
  const [selectedKey, setSelectedKey] = useState(null);
  // Plain text, not JSON: the implementation plan reads the same name.
  const [reviewer, setReviewer] = useState(() => { try { return localStorage.getItem(REVIEWER_KEY) || ""; } catch { return ""; } });
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [saved, setSaved] = useState(null);
  const [newName, setNewName] = useState("");
  const [stepError, setStepError] = useState(null);
  const [panel, setPanel] = useState("step");
  const [chat, setChat] = useState(() => readStored(CHAT_KEY, EMPTY_CHAT));
  const [proposal, setProposal] = useState(() => readStored(PROPOSAL_KEY, null));
  const [showProposal, setShowProposal] = useState(true);
  const [agentBusy, setAgentBusy] = useState(false);

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

  useEffect(() => { try { localStorage.setItem(REVIEWER_KEY, reviewer); } catch { /* a per-viewer convenience only */ } }, [reviewer]);
  useEffect(() => store(CHAT_KEY, chat), [chat]);
  useEffect(() => store(PROPOSAL_KEY, proposal), [proposal]);

  // The playbooks on screen: the agent's proposal while it is shown, else the draft.
  const viewing = !!proposal && showProposal;
  const shown = viewing ? proposal.doc : draft;
  const shownKeys = (shown?.pathways || []).map((p) => p.key).join("|");
  useEffect(() => {
    const keys = shownKeys ? shownKeys.split("|") : [];
    if (keys.length && !keys.includes(selectedKey)) setSelectedKey(keys[0]);
  }, [shownKeys, selectedKey]);

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

  if (loadError) return <main className="view"><div className="pathways-wrap"><div className="banner err">Could not load playbooks — {loadError}</div></div></main>;
  if (!data || !draft) return <main className="view"><div className="pathways-wrap"><div className="card spin">Loading playbooks…</div></div></main>;

  const dirty = JSON.stringify(draft) !== JSON.stringify(editable(data));
  const pathway = shown.pathways.find((p) => p.key === selectedKey) || null;
  const draftPathway = draft.pathways.find((p) => p.key === selectedKey) || null;
  const { marks, removed } = viewing && pathway
    ? marksFrom(diffSteps(draftPathway?.steps || [], pathway.steps, ACTORS))
    : { marks: undefined, removed: undefined };
  const selStep = pathway?.steps.find((s) => s.id === stepEdits.selected)
    || removed?.find((s) => s.id === stepEdits.selected) || null;
  const selRemoved = !!selStep && !pathway?.steps.includes(selStep);
  const recsFor = (key) => Object.keys(shown.rec_types).filter((l) => shown.rec_types[l] === key).sort();
  // Types in the data with no pathway, plus any this edit has just unmapped.
  const allLabels = [...new Set([...data.unmapped_rec_labels, ...Object.keys(data.rec_types), ...Object.keys(shown.rec_types)])];
  const unmapped = allLabels.filter((l) => !shown.rec_types[l]).sort();
  const touched = new Set((proposal?.changes || []).map((c) => c.playbook));
  const draftKeys = new Set(draft.pathways.map((p) => p.key));

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

  // ── the agent's proposal ──
  const chatNote = (text) => setChat((c) => ({ ...c, display: [...c.display, { kind: "event", level: "info", text }] }));

  function onProposal(p, sentDraft) {
    if (!p) {
      setProposal(null);
      return;
    }
    setProposal({ ...p, base: JSON.stringify(sentDraft) });
    setShowProposal(true);
    const keys = p.changes.map((c) => c.playbook);
    if (keys.length && !keys.includes(selectedKey)) setSelectedKey(keys[0]);
  }

  function addProposal() {
    if (JSON.stringify(draft) !== proposal.base &&
        !window.confirm("You changed the playbooks after the agent made this proposal. Adding it replaces those changes " +
                        "with the agent's version. Add it anyway?")) return;
    const n = proposal.changes.length;
    setDraft(editable(proposal.doc));
    setSaved(null);
    setNote((v) => v || "Changes proposed by the playbook agent");
    setProposal(null);
    chatNote(`You added the agent's proposal to your draft (${n} change${n === 1 ? "" : "s"}). Save to keep it.`);
  }

  function discardProposal() {
    setProposal(null);
    chatNote("You discarded the agent's proposal.");
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
  const selectStep = (id) => {
    stepEdits.setSelected(id);
    if (id) setPanel("step");
  };

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
          <span className="spacer" />
          <button type="button" className="ask-agent" onClick={() => setPanel("agent")}>
            ✦ Ask the agent{agentBusy && <span className="pl-spin" />}
          </button>
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
              {shown.pathways.map((p) => (
                <li key={p.key} aria-selected={p.key === selectedKey} onClick={() => setSelectedKey(p.key)}>
                  <div className="name">
                    {p.name || <i>Unnamed</i>}
                    {viewing && !draftKeys.has(p.key) && <span className="pc-flag new">New</span>}
                    {viewing && draftKeys.has(p.key) && touched.has(p.key) && <span className="pc-flag changed">Changed</span>}
                  </div>
                  <div className="meta">{p.steps.length} steps · {recsFor(p.key).length} rec types</div>
                </li>
              ))}
            </ul>
            <div className="pathway-new">
              <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="New playbook name"
                disabled={viewing} onKeyDown={(e) => e.key === "Enter" && addPathway()} />
              <button onClick={addPathway} disabled={viewing || !newName.trim()}>Add</button>
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
            {proposal && (
              <div className="pb-proposal">
                <div className="pb-proposal-head">
                  <b>✦ The agent's proposal</b>
                  <span className="kv">{proposal.changes.length} change{proposal.changes.length === 1 ? "" : "s"}</span>
                  <span className="spacer" />
                  <span className="seg" role="group" aria-label="Show">
                    <button type="button" aria-pressed={showProposal} onClick={() => setShowProposal(true)}>Proposal</button>
                    <button type="button" aria-pressed={!showProposal} onClick={() => setShowProposal(false)}>Your draft</button>
                  </span>
                  <button type="button" className="primary" onClick={addProposal}>Add to draft</button>
                  <button type="button" onClick={discardProposal}>Discard</button>
                </div>
                <ul className="pb-changes">
                  {proposal.changes.map((c, i) => (
                    <li key={i}>
                      <button type="button" className="link" onClick={() => { setShowProposal(true); setSelectedKey(c.playbook); }}>
                        <span className={"pb-op " + (OP_CLASS[c.op] || "")}>{c.op}</span> {c.text}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {!pathway ? (
              <div className="card empty">No playbook selected.</div>
            ) : (
              <div className="card">
                <h4>
                  Playbook <code className="assess">{pathway.key}</code>
                  {viewing && <span className="pc-flag changed" style={{ marginLeft: ".5rem" }}>Showing the agent's proposal</span>}
                </h4>
                <div className="pathway-fields">
                  <label>Name<input value={pathway.name} disabled={viewing} onChange={(e) => changePathway("name", e.target.value)} /></label>
                  <label>Who does the work<input value={pathway.who} disabled={viewing} onChange={(e) => changePathway("who", e.target.value)} /></label>
                  <label className="wide">
                    The deciding question
                    <textarea rows={2} value={pathway.branch} disabled={viewing} onChange={(e) => changePathway("branch", e.target.value)} />
                  </label>
                </div>

                <div className={"plan-canvas-wrap side" + (viewing ? " proposal" : " editing")}>
                  <PlanCanvas
                    steps={pathway.steps} marks={marks} removed={removed}
                    selectedId={stepEdits.selected} onSelect={selectStep}
                    editable={!viewing} onConnect={stepEdits.connect} onDisconnect={stepEdits.disconnect}
                  />
                  <div className="side-panel">
                    <div className="side-tabs" role="tablist">
                      <button type="button" role="tab" aria-selected={panel === "step"} onClick={() => setPanel("step")}>Step</button>
                      <button type="button" role="tab" aria-selected={panel === "agent"} onClick={() => setPanel("agent")}>
                        ✦ Agent{agentBusy && <span className="pl-spin" />}
                      </button>
                    </div>
                    {panel === "step" && (selStep ? (
                      <StepInspector
                        s={selStep} n={selRemoved ? selStep.n : pathway.steps.indexOf(selStep) + 1} steps={pathway.steps}
                        mark={marks?.[selStep.id]} removed={selRemoved} editable={!viewing} sources={data.filled_by}
                        onChange={(fields) => stepEdits.patch(selStep.id, fields)}
                        onDelete={() => stepEdits.remove(selStep.id)}
                        onAddAfter={() => stepEdits.addAfter(selStep.id)}
                        onSelect={selectStep}
                        onClose={() => stepEdits.setSelected(null)}
                      />
                    ) : (
                      <div className="si-empty">
                        <b>Click a step to {viewing ? "see it" : "edit it"}</b>
                        <p className="assess">
                          Each step has an activity, a description, what it depends on, a primary and a secondary owner,
                          and a potential duration.
                        </p>
                        {viewing ? (
                          <p className="assess">This is the agent's proposal. Add it to your draft to edit it, or switch to your draft.</p>
                        ) : (
                          <>
                            <p className="assess">
                              To link steps, drag from the dot under a step to the step that waits on it. Click a line to
                              remove the link. Or ask the agent to make the change.
                            </p>
                            <button type="button" onClick={() => stepEdits.addAfter(null)}>+ Add step</button>
                          </>
                        )}
                      </div>
                    ))}
                    <PlaybookAgent
                      hidden={panel !== "agent"} chat={chat} setChat={setChat}
                      draft={draft} proposal={proposal} selectedKey={selectedKey}
                      onProposal={onProposal} onBusy={setAgentBusy}
                    />
                  </div>
                </div>
                {stepError && <div className="banner err" style={{ marginTop: ".6rem" }}>{stepError}</div>}

                <div className="btn-row">
                  <button onClick={() => stepEdits.addAfter(null)} disabled={viewing}>+ Add step</button>
                  <span className="spacer" />
                  <button onClick={removePathway} disabled={viewing}>Delete playbook</button>
                </div>

                <h4 className="recs-head">Recommendation types in this playbook</h4>
                <ul className="pathway-recs">
                  {recsFor(pathway.key).map((label) => (
                    <li key={label}>
                      <span>{label}</span>
                      <select value={pathway.key} disabled={viewing} onChange={(e) => mapRec(label, e.target.value)} title="Move to another playbook">
                        {shown.pathways.map((p) => <option key={p.key} value={p.key}>{p.name || p.key}</option>)}
                        <option value="">No playbook</option>
                      </select>
                    </li>
                  ))}
                </ul>
                {unmapped.length > 0 && !viewing && (
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
              {proposal && <div className="assess" style={{ marginTop: ".5rem" }}>Save keeps your draft. The agent's proposal is only saved once you add it to your draft.</div>}
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
