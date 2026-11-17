import { useEffect, useState } from "react";
import { call, deadlineLabel, money, num, scoreColour } from "../api.js";
import { Cites, Outcome, References, SmeQuestions, ZoningFinding, zoningAnswer } from "../zoning/ZoningParts.jsx";

// The steps to get one recommendation done: its pathway template, with approved
// funding attached to the funding step. A reviewer edits and finalises it here.
//
// Each step reads as a card: what to do, who and how long, then a one-line
// answer. The evidence behind that answer (the zoning narrative, references,
// SME questions) is folded away and opens on its own only where an SME has to
// act, so the plan stays skimmable without hiding where anything came from.

const NEW_STEP = { title: "", owner: "", waits_on: "", time: "", filled_by: "template", edited: true };

function statusLine(plan) {
  if (plan.status === "final") return `Final — reviewed by ${plan.reviewed_by}`;
  if (plan.status === "draft") return "Draft — saved, not finalized";
  return "Template — not reviewed yet";
}

// Whether a step is waiting on the SME: a lookup that left questions open, or a
// source that isn't built yet. Once the reviewer edits or finalises, it isn't.
function needsSme(s, plan) {
  if (s.edited || plan.status === "final") return false;
  if (s.zoning) return s.zoning_detail === "full" && (s.input_status === "partial" || s.input_status === "needs_sme");
  return s.input_status === "needs_sme";
}

// Where a step's content comes from. A live agent's findings are labelled with
// that agent; a source that isn't built yet falls to the SME; anything else is
// the template's wording until the SME edits or confirms it.
function SourceTags({ s, plan }) {
  const final = plan.status === "final";
  if (s.zoning) {
    const resolved = s.input_status === "attached" || s.input_status === "partial";
    return (
      <>
        {s.input_status === "missing" ? (
          <span className="tag derived" title="Run python -m scripts.sync_block_zoning">{s.source_label} · not run</span>
        ) : (
          <span className={"tag " + (resolved ? "ok" : "derived")} title={s.source_note}>{s.source_label}</span>
        )}
        {(s.input_status === "partial" || s.input_status === "needs_sme") && !(s.edited || final) && (
          <span className="tag derived" title="The lookup could not settle part of this; see the questions">Needs SME</span>
        )}
        {s.edited ? <span className="tag edited">SME edited</span> : final && <span className="tag ok">SME confirmed</span>}
      </>
    );
  }
  if (s.input_status === "needs_sme") {
    return s.edited || final ? (
      <span className="tag ok" title={`Supplied by the reviewer. ${s.source_note || ""}`}>SME</span>
    ) : (
      <span className="tag derived" title={`No ${s.source_label} yet. ${s.source_note || ""}`}>Needs SME</span>
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

// Approved programs, one line each. The fit narratives sit in the evidence.
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
          </li>
        );
      })}
    </ul>
  );
}

// A step no source fills yet: why the step needs it, then the specific question.
function SmeAsk({ s }) {
  if (!s.sme_ask && !s.sme_why) return null;
  return (
    <div className="sme-ask-block">
      {s.sme_why && <p className="assess">{s.sme_why}</p>}
      {s.sme_ask && <SmeQuestions items={[{ text: s.sme_ask }]} />}
    </div>
  );
}

// The one line under a step's title that says what was found, if anything was.
function StepAnswer({ s, onJump }) {
  if (s.zoning) {
    if (s.zoning_detail === "summary") {
      return (
        <div className="ps-answer">
          {s.zoning.best && <Outcome outcome={s.zoning.best} />}
          <span>
            {s.zoning.summary}.{" "}
            <button type="button" className="link" onClick={() => onJump(s.zoning_see_step)}>
              Full finding in step {s.zoning_see_step}
            </button>
          </span>
        </div>
      );
    }
    const a = zoningAnswer(s.zoning);
    return (
      <div className="ps-answer">
        {s.input_status !== "missing" && <Outcome outcome={s.zoning.best} />}
        <span>{a.text}<Cites refs={a.refs} /></span>
      </div>
    );
  }
  if (s.funding && s.input_status === "attached") return <FundingList funding={s.funding} />;
  if (s.funding && s.input_status === "missing") {
    return <div className="ps-answer assess">No approved funding for this recommendation yet.</div>;
  }
  return null;
}

// What sits behind the answer. Nothing is dropped here, only folded away.
function evidenceFor(s, plan) {
  const refCount = s.references?.length || 0;
  if (s.zoning) {
    if (s.zoning_detail === "summary") return null;
    const qs = s.zoning.questions.length;
    return {
      label: qs ? `Evidence and ${qs} SME question${qs > 1 ? "s" : ""}` : `Evidence · ${refCount} reference${refCount === 1 ? "" : "s"}`,
      body: (
        <>
          <ZoningFinding z={s.zoning} hideSummary omit={["meaning"]} />
          <References refs={s.references} />
        </>
      ),
    };
  }
  const narratives = (s.input_status === "attached" && s.funding?.filter((f) => f.narrative)) || [];
  const ask = s.input_status === "needs_sme" && !s.edited && plan.status !== "final" && (s.sme_ask || s.sme_why);
  if (!refCount && !narratives.length && !ask) return null;
  const parts = [];
  if (ask) parts.push("What we need from the SME");
  if (narratives.length) parts.push("Why these fit");
  if (refCount) parts.push(`${refCount} reference${refCount === 1 ? "" : "s"}`);
  return {
    label: parts.join(" · "),
    body: (
      <>
        {ask && <SmeAsk s={s} />}
        {narratives.length > 0 && (
          <ul className="fit-why">
            {narratives.map((f) => <li key={f.signal_id ?? f.program_name}><b>{f.program_name}:</b> {f.narrative}</li>)}
          </ul>
        )}
        <References refs={s.references} />
      </>
    ),
  };
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
  // Step numbers whose evidence is unfolded. Steps waiting on an SME start open.
  const [open, setOpen] = useState(new Set());

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

  useEffect(() => {
    if (!plan) return;
    setOpen(new Set(plan.steps.map((s, i) => (needsSme(s, plan) ? i + 1 : null)).filter(Boolean)));
  }, [plan]);

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

  function toggle(n, isOpen) {
    setOpen((prev) => {
      if (prev.has(n) === isOpen) return prev;
      const next = new Set(prev);
      isOpen ? next.add(n) : next.delete(n);
      return next;
    });
  }

  // Open a step's evidence and bring it into view.
  function jump(n) {
    toggle(n, true);
    requestAnimationFrame(() => {
      document.getElementById(`plan-step-${recId}-${n}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
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

  const waiting = plan.steps.map((s, i) => (needsSme(s, plan) ? i + 1 : null)).filter(Boolean);

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
            Who does the work: <b>{plan.pathway.who}</b> · {plan.pathway.branch} · template {plan.template_version}
          </div>

          {!plan.storage_ready && (
            <div className="banner warn">
              Saving is off until the <code>implementation_plans</code> table exists — run the IMPLEMENTATION PLANS
              section of <code>backend/db/schema.sql</code> in the Supabase SQL editor. Viewing works.
            </div>
          )}

          {!editing && waiting.length > 0 && (
            <div className="plan-attention">
              {waiting.length === 1 ? "1 step needs" : `${waiting.length} steps need`} an SME:{" "}
              {waiting.map((n, k) => (
                <span key={n}>
                  {k > 0 && ", "}
                  <button type="button" className="link" onClick={() => jump(n)}>step {n}</button>
                </span>
              ))}
            </div>
          )}

          {editing ? (
            <ol className="plan-steps">
              {steps.map((s, i) => (
                <li key={i} className="plan-step editing">
                  <div className="ps-head">
                    <span className="ps-n">{i + 1}</span>
                    <textarea rows={2} value={s.title || ""} placeholder="What has to happen"
                      onChange={(e) => change(i, "title", e.target.value)} />
                    <span className="row-actions">
                      <button type="button" title="Move up" onClick={() => move(i, -1)} disabled={i === 0}>↑</button>
                      <button type="button" title="Move down" onClick={() => move(i, 1)} disabled={i === steps.length - 1}>↓</button>
                      <button type="button" title="Remove step" onClick={() => setSteps((p) => p.filter((_, j) => j !== i))}>✕</button>
                    </span>
                  </div>
                  <div className="ps-body ps-fields">
                    <label>Owner<input value={s.owner || ""} onChange={(e) => change(i, "owner", e.target.value)} /></label>
                    <label>Waits on<input value={s.waits_on || ""} onChange={(e) => change(i, "waits_on", e.target.value)} /></label>
                    <label>Time<input value={s.time || ""} onChange={(e) => change(i, "time", e.target.value)} /></label>
                  </div>
                  <div className="ps-body ps-tags"><SourceTags s={s} plan={plan} /></div>
                </li>
              ))}
            </ol>
          ) : (
            <ol className="plan-steps">
              {plan.steps.map((s, i) => {
                const n = i + 1;
                const ev = evidenceFor(s, plan);
                const cls = needsSme(s, plan) ? " needs-sme" : s.funding && s.input_status === "attached" ? " has-funding" : "";
                return (
                  <li key={i} id={`plan-step-${recId}-${n}`} className={"plan-step" + cls}>
                    <div className="ps-head">
                      <span className="ps-n">{n}</span>
                      <span className="ps-title">{s.title}</span>
                    </div>
                    <div className="ps-body ps-meta">
                      <span>{s.owner || "No owner yet"}</span>
                      {s.time && <span>· {s.time}</span>}
                      {s.waits_on && <span>· waits on {s.waits_on}</span>}
                      <span className="ps-tags"><SourceTags s={s} plan={plan} /></span>
                    </div>
                    <div className="ps-body">
                      <StepAnswer s={s} onJump={jump} />
                      {s.zoning_detail === "summary" ? null : ev ? (
                        <details className="ps-evidence" open={open.has(n)} onToggle={(e) => toggle(n, e.currentTarget.open)}>
                          <summary>{ev.label}</summary>
                          <div className="ps-evidence-body">{ev.body}</div>
                        </details>
                      ) : null}
                    </div>
                  </li>
                );
              })}
            </ol>
          )}

          <details className="source-legend kv">
            <summary>What the tags mean</summary>
            <span className="tag ok">Funding agent</span> found by an agent ·{" "}
            <span className="tag ok">Zoning lookup</span> looked up on the county map and the use tables ·{" "}
            <span className="tag">Template</span> draft wording, not yet reviewed ·{" "}
            <span className="tag edited">SME edited</span> / <span className="tag ok">SME confirmed</span> set by the reviewer ·{" "}
            <span className="tag derived">Needs SME</span> no agent or lookup supplies this yet, so the reviewer fills it in ·{" "}
            <Outcome outcome="by_right" /> <Outcome outcome="special_exception" /> <Outcome outcome="prohibited" />{" "}
            <Outcome outcome="legacy" /> permitted by right, special exception, prohibited, legacy zone
          </details>

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
