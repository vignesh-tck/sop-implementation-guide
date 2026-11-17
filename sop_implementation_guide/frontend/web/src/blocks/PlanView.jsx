import { useEffect, useState } from "react";
import { call, deadlineLabel, money, num, scoreColour } from "../api.js";

// The steps to get one recommendation done: its pathway template, with approved
// funding attached to the funding step. A reviewer edits and finalises it here.

const NEW_STEP = { title: "", owner: "", waits_on: "", time: "", filled_by: "template", edited: true };

function statusLine(plan) {
  if (plan.status === "final") return `Final — reviewed by ${plan.reviewed_by}`;
  if (plan.status === "draft") return "Draft — saved, not finalized";
  return "Template — not reviewed yet";
}

// Where a step's content comes from. A live agent's findings are labelled with
// that agent; a source that isn't built yet falls to the SME; anything else is
// the template's wording until the SME edits or confirms it.
function SourceTags({ s, plan }) {
  const final = plan.status === "final";
  if (s.input_status === "needs_sme") {
    return s.edited || final ? (
      <span className="tag ok" title={`Supplied by the reviewer. ${s.source_note || ""}`}>SME</span>
    ) : (
      <>
        <span className="tag derived" title={s.source_note}>Needs SME</span>
        <div className="source-hint">no {s.source_label} yet</div>
      </>
    );
  }
  return (
    <>
      {s.input_status === "attached" && (
        <span className="tag ok" title={s.source_note}>{s.source_label} · {s.funding.length}</span>
      )}
      {s.input_status === "missing" && (
        <span className="tag derived" title="Run funding analysis for this block, then approve the fits you want kept">
          {s.source_label} · none approved yet
        </span>
      )}
      {s.edited ? (
        <span className="tag edited">SME edited</span>
      ) : final ? (
        <span className="tag ok">SME confirmed</span>
      ) : (
        <span className="tag" title="Draft template wording, not yet reviewed by an SME">Template</span>
      )}
    </>
  );
}

function FundingList({ funding }) {
  return (
    <ul className="plan-funding">
      {funding.map((f) => {
        const dl = deadlineLabel(f.deadline);
        const amt = f.award_amount_min || f.award_amount_max
          ? `${money(f.award_amount_min)} – ${money(f.award_amount_max)}` : null;
        return (
          <li key={f.signal_id ?? f.program_name}>
            <b style={{ color: scoreColour(f.fit_score) }}>{num(f.fit_score, 2)}</b>{" "}
            {f.application_url ? (
              <a href={f.application_url} target="_blank" rel="noopener noreferrer">{f.program_name} ↗</a>
            ) : (
              <span>{f.program_name}</span>
            )}
            {f.deadline && <span className={"deadline" + (dl.soon ? " soon" : "")}> · due {dl.text}</span>}
            {amt && <span className="assess"> · {amt}</span>}
            {f.narrative && <div className="assess">{f.narrative}</div>}
          </li>
        );
      })}
    </ul>
  );
}

export default function PlanView({ blockId, recId, refreshKey, onSaved }) {
  const [plan, setPlan] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [editing, setEditing] = useState(false);
  const [steps, setSteps] = useState([]);
  const [reviewer, setReviewer] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [promoted, setPromoted] = useState(null);

  useEffect(() => {
    setPlan(null);
    setLoadError(null);
    setEditing(false);
    setSaveError(null);
    setPromoted(null);
    let live = true;
    call("GET", `/blocks/${blockId}/recommendations/${recId}/plan`)
      .then((p) => { if (live) setPlan(p); })
      .catch((e) => { if (live) setLoadError(e.message); });
    return () => { live = false; };
  }, [blockId, recId, refreshKey]);

  if (loadError) return <div className="banner err">Could not load the plan — {loadError}</div>;
  if (!plan) return <div className="card spin">Loading plan…</div>;

  const rec = plan.rec;
  const down = rec.direction === "Decrease";

  function startEdit() {
    setSteps(plan.steps.map((s) => ({ ...s })));
    setReviewer(plan.reviewed_by || "");
    setNotes(plan.notes || "");
    setSaveError(null);
    setEditing(true);
  }

  function change(i, field, value) {
    setSteps((prev) => prev.map((s, j) => (j === i ? { ...s, [field]: value, edited: true } : s)));
  }

  function move(i, delta) {
    setSteps((prev) => {
      const j = i + delta;
      if (j < 0 || j >= prev.length) return prev;
      const next = [...prev];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  }

  async function save(status) {
    setSaving(true);
    setSaveError(null);
    try {
      const p = await call("PUT", `/blocks/${blockId}/recommendations/${recId}/plan`, {
        steps, status, reviewed_by: reviewer, notes,
      });
      setPlan(p);
      setEditing(false);
      onSaved?.();
    } catch (e) {
      setSaveError(e.message);
    } finally {
      setSaving(false);
    }
  }

  // A finalised plan's steps become the pathway's template, so the next block
  // starts from what the SME settled on rather than the draft guess.
  async function promote() {
    const msg = `Make these ${plan.steps.length} steps the template for every "${plan.pathway.name}" plan? ` +
      "Plans already saved keep their own steps.";
    if (!window.confirm(msg)) return;
    setSaveError(null);
    try {
      const t = await call("PUT", `/pathways/${plan.pathway.key}/steps`, {
        steps: plan.steps, updated_by: plan.reviewed_by,
        note: `From the final plan for ${rec.rec_label} on block ${blockId}`,
      });
      setPromoted(`Saved as template ${t.version}.`);
    } catch (e) {
      setSaveError(e.message);
    }
  }

  const rows = editing ? steps : plan.steps;

  return (
    <div className="card plan">
      <h4>Implementation plan</h4>
      <div className="plan-head">
        <span className={"pill " + (down ? "no" : "yes")}>{down ? "reduce" : "increase"}</span>
        <h5>{rec.rec_label}</h5>
        {plan.pathway && <span className="tag">{plan.pathway.name}</span>}
        <span className="spacer" />
        <span className={"plan-status " + plan.status}>{statusLine(plan)}</span>
      </div>

      {!plan.pathway ? (
        <div className="banner warn">
          This recommendation type is not mapped to a pathway yet. Add it to one in the Pathways tab.
        </div>
      ) : (
        <>
          <div className="kv plan-meta">
            Who does the work: <b>{plan.pathway.who}</b> · {plan.pathway.branch}
          </div>
          <div className="kv plan-meta">
            Template {plan.template_version}.
            {plan.funding_count > 0
              ? ` ${plan.funding_count} approved funding match(es) attached.`
              : " No approved funding for this recommendation yet."}
          </div>

          {!plan.storage_ready && (
            <div className="banner warn">
              Saving is off until the <code>implementation_plans</code> table exists — run the IMPLEMENTATION PLANS
              section of <code>backend/db/schema.sql</code> in the Supabase SQL editor. Viewing works.
            </div>
          )}

          <div className="table-wrap">
            <table className="plan-table">
              <thead>
                <tr><th>#</th><th>Step</th><th>Owner</th><th>Waits on</th><th>Time</th><th>Source</th>{editing && <th />}</tr>
              </thead>
              <tbody>
                {rows.map((s, i) => (
                  <tr key={i} className={s.input_status === "attached" ? "has-funding" : ""}>
                    <td className="n">{i + 1}</td>
                    {editing ? (
                      <>
                        <td><textarea rows={2} value={s.title || ""} onChange={(e) => change(i, "title", e.target.value)} /></td>
                        <td><input value={s.owner || ""} onChange={(e) => change(i, "owner", e.target.value)} /></td>
                        <td><input value={s.waits_on || ""} onChange={(e) => change(i, "waits_on", e.target.value)} /></td>
                        <td><input value={s.time || ""} onChange={(e) => change(i, "time", e.target.value)} /></td>
                        <td className="source"><SourceTags s={s} plan={plan} /></td>
                        <td className="row-actions">
                          <button type="button" title="Move up" onClick={() => move(i, -1)} disabled={i === 0}>↑</button>
                          <button type="button" title="Move down" onClick={() => move(i, 1)} disabled={i === rows.length - 1}>↓</button>
                          <button type="button" title="Remove step" onClick={() => setSteps((p) => p.filter((_, j) => j !== i))}>✕</button>
                        </td>
                      </>
                    ) : (
                      <>
                        <td>
                          <div>{s.title}</div>
                          {s.input_status === "attached" && <FundingList funding={s.funding} />}
                        </td>
                        <td>{s.owner || <span className="assess">—</span>}</td>
                        <td className="assess">{s.waits_on || "—"}</td>
                        <td className="nowrap">{s.time || <span className="assess">—</span>}</td>
                        <td className="source"><SourceTags s={s} plan={plan} /></td>
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="kv source-legend">
            <b>Source:</b> <span className="tag ok">Funding agent</span> found by an agent ·{" "}
            <span className="tag">Template</span> draft wording, not yet reviewed ·{" "}
            <span className="tag edited">SME edited</span> / <span className="tag ok">SME confirmed</span> set by the reviewer ·{" "}
            <span className="tag derived">Needs SME</span> no agent or lookup supplies this yet, so the reviewer fills it in
          </div>

          {editing ? (
            <div className="plan-edit">
              <button type="button" onClick={() => setSteps((p) => [...p, { ...NEW_STEP }])}>+ Add step</button>
              <div className="plan-edit-row">
                <label>
                  Reviewer
                  <input value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="Name (required to finalize)" />
                </label>
                <label className="grow">
                  Notes
                  <input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Anything the next reviewer should know" />
                </label>
              </div>
              {saveError && <div className="banner err">Save failed — {saveError}</div>}
              <div className="btn-row">
                <button onClick={() => save("draft")} disabled={saving || !plan.storage_ready}>Save draft</button>
                <button className="primary" onClick={() => save("final")} disabled={saving || !plan.storage_ready || !reviewer.trim()}>
                  {saving ? "Saving…" : "Finalize plan"}
                </button>
                <button onClick={() => setEditing(false)} disabled={saving}>Cancel</button>
              </div>
            </div>
          ) : (
            <div className="btn-row">
              <button className="primary" onClick={startEdit}>
                {plan.status === "template" ? "Review and edit" : "Edit plan"}
              </button>
              {plan.status === "final" && !promoted && (
                <button onClick={promote} title="Every plan on this pathway that nobody has saved yet will start from these steps">
                  Use as pathway template
                </button>
              )}
              {plan.notes && <span className="kv">Notes: {plan.notes}</span>}
            </div>
          )}
          {!editing && saveError && <div className="banner err" style={{ marginTop: ".75rem" }}>{saveError}</div>}
          {promoted && <div className="banner ok" style={{ marginTop: ".75rem" }}>{promoted}</div>}
        </>
      )}
    </div>
  );
}
