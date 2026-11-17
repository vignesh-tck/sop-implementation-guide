import { useEffect, useState } from "react";
import { call, deadlineLabel, money, num, scoreColour } from "../api.js";
import { Cites, Outcome, References, SmeQuestions, ZoningFinding, zoningAnswer } from "../zoning/ZoningParts.jsx";

// The steps to get one recommendation done, as an accordion. A closed step is
// one line: what, who, how long. An open step leads with its verdict, then the
// facts behind it, with the full ordinance text and sources one click further.
// Steps waiting on an SME open on their own; so does step 1, so the plan never
// opens as a wall of closed rows.
//
// The plan is loaded by the parent (RecommendationView), which also shows it in
// the Zoning, Funding and Sources tabs; saving hands the new plan back up.

const NEW_STEP = { title: "", owner: "", waits_on: "", time: "", filled_by: "template", edited: true };

export function statusLine(plan) {
  if (plan.status === "final") return `Final — reviewed by ${plan.reviewed_by}`;
  if (plan.status === "draft") return "Draft — saved, not finalized";
  return "Template — not reviewed yet";
}

// Whether a step is waiting on the SME: a lookup that left questions open, or a
// source that isn't built yet. Once the reviewer edits or finalises, it isn't.
export function needsSme(s, plan) {
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

export function FundingList({ funding, withNarrative = false }) {
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
            {withNarrative && f.narrative && <div className="assess">{f.narrative}</div>}
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

const VERDICT_CLASS = { by_right: "ok", special_exception: "cond", prohibited: "no" };

// The zoning verdict, coloured by outcome.
function Verdict({ z }) {
  const a = zoningAnswer(z);
  const cls = z.status === "missing" ? "none" : VERDICT_CLASS[z.best] || "none";
  const mark = { ok: "✓", cond: "!", no: "✕", none: "?" }[cls];
  return (
    <div className={"verdict " + cls}>
      <span className="verdict-mark" aria-hidden="true">{mark}</span>
      <span>{a.text}<Cites refs={a.refs} /></span>
    </div>
  );
}

// "Found" and "Ordinance" as label/value rows: the two facts the verdict rests on.
function FactRows({ z }) {
  const rows = [
    ["Found", z.narrative.filter((p) => p.section === "found")],
    ["Ordinance", z.narrative.filter((p) => p.section === "ordinance")],
  ].filter(([, ps]) => ps.length);
  if (!rows.length) return null;
  return (
    <dl className="fact-rows">
      {rows.map(([label, ps]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{ps.map((p, i) => <p key={i}>{p.text}<Cites refs={p.refs} /></p>)}</dd>
        </div>
      ))}
    </dl>
  );
}

function ZoningStepBody({ s, onJump }) {
  const z = s.zoning;
  // The full text opens with the questions, because the explanation behind a
  // question (how we read the recommendation, what we checked) lives in it and
  // has to come before the question, not after.
  const [more, setMore] = useState(z.questions.length > 0);
  if (s.zoning_detail === "summary") {
    return (
      <>
        <Verdict z={z} />
        <p className="assess">
          Same zoning finding as{" "}
          <button type="button" className="link" onClick={() => onJump(s.zoning_see_step)}>step {s.zoning_see_step}</button>.
        </p>
      </>
    );
  }
  return (
    <>
      <Verdict z={z} />
      <FactRows z={z} />
      {more ? (
        <>
          <ZoningFinding z={z} hideSummary hideQuestions omit={["found", "ordinance", "meaning"]} />
          {z.questions.length > 0 && <SmeQuestions items={z.questions} />}
          <div className="zoning-h refs-h">Sources</div>
          <References refs={s.references} />
          <button type="button" className="ghost" onClick={() => setMore(false)}>Hide ordinance text</button>
        </>
      ) : (
        <button type="button" className="ghost" onClick={() => setMore(true)}>
          Show ordinance text and sources ({s.references.length})
        </button>
      )}
    </>
  );
}

// Sources for a non-zoning step, folded: they back the step, they aren't the step.
function StepSources({ refs }) {
  if (!refs?.length) return null;
  return (
    <details className="step-sources">
      <summary>Sources ({refs.length})</summary>
      <References refs={refs} />
    </details>
  );
}

function StepBody({ s, plan, onJump, onFunding }) {
  if (s.zoning) return <ZoningStepBody s={s} onJump={onJump} />;
  const waiting = needsSme(s, plan);
  return (
    <>
      {s.funding && s.input_status === "attached" && <FundingList funding={s.funding} withNarrative />}
      {s.funding && s.input_status === "missing" && (
        <div className="verdict cond">
          <span className="verdict-mark" aria-hidden="true">!</span>
          <span>
            No approved funding for this recommendation yet. Funding is analysed for the whole block at once.{" "}
            <button type="button" className="link" onClick={onFunding}>Open the block's funding review</button>.
          </span>
        </div>
      )}
      {waiting && <SmeAsk s={s} />}
      {s.input_status === "template" && !s.edited && plan.status !== "final" && (
        <p className="assess">Template wording, not yet reviewed by an SME.</p>
      )}
      <StepSources refs={s.references} />
    </>
  );
}

// What a closed step shows on the right: the one thing worth seeing at a glance.
function StepMarker({ s, plan }) {
  if (needsSme(s, plan)) return <span className="tag derived">Needs SME</span>;
  if (s.zoning && s.input_status !== "missing") return <Outcome outcome={s.zoning.best} />;
  if (s.funding && s.input_status === "attached") return <span className="tag ok">{s.funding.length} funding</span>;
  return null;
}

export default function PlanView({ plan, blockId, recId, onSaved, onFunding }) {
  const [editing, setEditing] = useState(false);
  const [steps, setSteps] = useState([]);
  const [reviewer, setReviewer] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [promoted, setPromoted] = useState(null);
  const [open, setOpen] = useState(new Set());

  // A fresh plan (another recommendation, or a save) resets the view.
  useEffect(() => {
    setEditing(false);
    setSaveError(null);
    setPromoted(null);
    const start = new Set(plan.steps.map((s, i) => (needsSme(s, plan) ? i + 1 : null)).filter(Boolean));
    if (plan.steps.length) start.add(1);
    setOpen(start);
  }, [plan]);

  if (!plan.pathway) {
    return (
      <div className="banner warn">
        This recommendation type is not mapped to a pathway yet. Add it to one in the Pathways tab.
      </div>
    );
  }

  const rec = plan.rec;

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

  function toggle(n) {
    setOpen((prev) => {
      const next = new Set(prev);
      next.has(n) ? next.delete(n) : next.add(n);
      return next;
    });
  }

  function jump(n) {
    setOpen((prev) => new Set(prev).add(n));
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
      setEditing(false);
      onSaved?.(p);
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
    <div className="plan">
      <div className="plan-who kv">
        <b>{plan.pathway.name}</b> · {plan.pathway.who} · {plan.pathway.branch}
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
              <div className="ps-fields">
                <label>Owner<input value={s.owner || ""} onChange={(e) => change(i, "owner", e.target.value)} /></label>
                <label>Waits on<input value={s.waits_on || ""} onChange={(e) => change(i, "waits_on", e.target.value)} /></label>
                <label>Time<input value={s.time || ""} onChange={(e) => change(i, "time", e.target.value)} /></label>
              </div>
              <div className="ps-tags"><SourceTags s={s} plan={plan} /></div>
            </li>
          ))}
        </ol>
      ) : (
        <ol className="plan-steps">
          {plan.steps.map((s, i) => {
            const n = i + 1;
            const isOpen = open.has(n);
            const meta = [s.owner || "No owner yet", s.time, s.waits_on ? `waits on ${s.waits_on}` : "no dependencies"]
              .filter(Boolean).join(" · ");
            return (
              <li key={i} id={`plan-step-${recId}-${n}`}
                className={"plan-step" + (isOpen ? " open" : "") + (needsSme(s, plan) ? " needs-sme" : "")}>
                <button type="button" className="ps-toggle" aria-expanded={isOpen} onClick={() => toggle(n)}>
                  <span className="ps-n">{n}</span>
                  <span className="ps-text">
                    <span className="ps-title">{s.title}</span>
                    <span className="ps-meta">{meta}</span>
                  </span>
                  {!isOpen && <StepMarker s={s} plan={plan} />}
                  <span className="chev" aria-hidden="true">{isOpen ? "⌃" : "›"}</span>
                </button>
                {isOpen && (
                  <div className="ps-body">
                    <StepBody s={s} plan={plan} onJump={jump} onFunding={onFunding} />
                    <div className="ps-tags"><SourceTags s={s} plan={plan} /></div>
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}

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
    </div>
  );
}
