import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { call, deadlineLabel, money, num, scoreColour } from "../api.js";
import { Cites, OUTCOME, Outcome, References, SmeQuestions, ZoningFinding, zoningAnswer } from "../zoning/ZoningParts.jsx";
import PlanCanvas from "./plan/PlanCanvas.jsx";
import StepInspector from "./plan/StepInspector.jsx";
import { ACTORS, ActorChip, ActorLegend } from "./plan/actors.jsx";
import { afterText, changeSummary, diffSteps, estimateText, marksFrom, numbers } from "./plan/graph.js";
import { useStepEdits } from "./plan/useStepEdits.js";

// The implementation plan for one recommendation: a flowchart of its steps
// (who does each, how, how long, what it waits on), or the same steps as a
// list. Clicking a step opens it beside the canvas with whatever the agents
// found for it.
//
// What everyone reads is the approved plan, or the playbook's until someone
// approves one. An SME's edits are saved as a draft, drawn with what they
// change, and only become the plan once someone approves them. The Audit tab
// (PlanAudit) shows every draft and approval.
//
// The plan is loaded by the parent (RecommendationView), which also shows it in
// the Zoning, Funding and Sources tabs; every save hands the new plan back up.

const NAME_KEY = "sop.pathways.reviewer"; // shared with the Playbooks tab
const VIEW_KEY = "sop.plan.view";

function readPref(key, fallback) {
  try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
}
function writePref(key, value) {
  try { localStorage.setItem(key, value); } catch { /* a per-viewer convenience only */ }
}

// A zoning step with no finding to show: the block was never looked up, or has
// no approved zoning review.
const noZoning = (s) => s.input_status === "missing" || s.input_status === "not_reviewed";
// The badge for an approved zoning finding: its outcome, or n/a where zoning does not decide it.
const zoningMark = (z) => (z.review?.outcome === "not_applicable" ? "not_applicable" : z.best || "unknown");

// Whether a step is waiting on the SME: a lookup that left questions open, or a
// source that isn't built yet. Once the reviewer edits or approves, it isn't.
export function needsSme(s, plan) {
  if (s.edited || plan.status === "final") return false;
  if (s.zoning) return s.zoning_detail === "full" && (s.input_status === "partial" || s.input_status === "needs_sme");
  return s.input_status === "needs_sme";
}

// What a step's card shows in its corner: the one thing worth seeing at a glance.
const badgeFor = (plan) => (s) => ({
  sme: needsSme(s, plan),
  zoning: s.zoning && !noZoning(s) ? zoningMark(s.zoning) : null,
  funding: s.funding && s.input_status === "attached" ? s.funding.length : null,
});

export function FundingList({ funding, withNarrative = false }) {
  const total = funding.reduce((t, f) => t + (f.planned_amount > 0 ? Number(f.planned_amount) : 0), 0);
  return (
    <ul className="plan-funding">
      {funding.map((f) => {
        const dl = deadlineLabel(f.deadline);
        // The amount approved for this recommendation; blank on funding approved
        // before amounts were required, never the program's published range.
        const amt = f.planned_amount > 0 ? money(f.planned_amount) : null;
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
      {funding.length > 1 && total > 0 && <li className="plan-funding-total">Total <b>{money(total)}</b></li>}
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
  const cls = z.status === "missing" || z.status === "not_reviewed" ? "none" : VERDICT_CLASS[z.best] || "none";
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

// Who approved the finding and how: shown under the verdict, so a reader knows it was reviewed.
function ReviewLine({ r }) {
  if (!r) return null;
  const date = (r.at || "").slice(0, 10);
  const how = r.action === "open" ? "Left open in the zoning review"
    : r.action === "set" && r.outcome !== r.proposed ? `Set to “${OUTCOME[r.outcome]?.text || r.outcome}” in the zoning review`
    : r.auto ? "Settled in the zoning review, nothing to ask" : "Accepted as proposed in the zoning review";
  return (
    <div className="zreview-line">
      {how} by {r.by}{date && `, ${date}`} · run #{r.analysis_id}, Zoning v{r.dataset_version}
      {r.note && <> · “{r.note}”</>}
    </div>
  );
}

function ZoningStepBody({ s, onJump, onZoning }) {
  const z = s.zoning;
  // The full text opens with the questions, because the explanation behind a
  // question (how we read the recommendation, what we checked) lives in it and
  // has to come before the question, not after.
  const [more, setMore] = useState(z.questions.length > 0);
  if (z.status === "not_reviewed") {
    return (
      <>
        <Verdict z={z} />
        {onZoning && <button type="button" className="link" onClick={onZoning}>Open the block's zoning review</button>}
      </>
    );
  }
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
      <ReviewLine r={z.review} />
      <FactRows z={z} />
      {more ? (
        <>
          <ZoningFinding z={z} hideSummary hideQuestions omit={["found", "ordinance", "meaning", "review"]} />
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

function StepBody({ s, plan, onJump, onFunding, onZoning }) {
  if (s.zoning) return <ZoningStepBody s={s} onJump={onJump} onZoning={onZoning} />;
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
        <p className="assess">Playbook wording, not yet reviewed by an SME.</p>
      )}
      <StepSources refs={s.references} />
    </>
  );
}

// What a closed list row shows on the right.
function StepMarker({ s, plan, mark }) {
  if (mark?.kind === "new") return <span className="pc-flag new">New</span>;
  if (mark?.kind === "changed") return <span className="pc-flag changed">Changed</span>;
  if (needsSme(s, plan)) return <span className="tag derived">Needs SME</span>;
  if (s.zoning && !noZoning(s)) return <Outcome outcome={zoningMark(s.zoning)} />;
  if (s.funding && s.input_status === "attached") return <span className="tag ok">{s.funding.length} funding</span>;
  return null;
}

function Estimate({ est, editing }) {
  if (editing) return <span className="kv" title="The estimate is worked out when you save">Time estimate updates when you save</span>;
  const text = estimateText(est);
  if (!text) return null;
  const untimed = est.untimed?.length ? ` Step${est.untimed.length > 1 ? "s" : ""} ${est.untimed.join(", ")} ha${est.untimed.length > 1 ? "ve" : "s"} no set time and count as none.` : "";
  return (
    <span className="plan-estimate" title={"A rough range along the longest chain of steps. The short end skips steps that only happen in some cases." + untimed}>
      Roughly <b>{text}</b>{untimed && " *"}
    </span>
  );
}

const when = (t) => (t || "").slice(0, 10);

export default function PlanView({ plan, blockId, recId, onSaved, onFunding, onZoning }) {
  const [view, setView] = useState(() => readPref(VIEW_KEY, "canvas"));
  const [showing, setShowing] = useState(plan.draft ? "draft" : "approved");
  const [editing, setEditing] = useState(false);
  const [buf, setBuf] = useState([]);
  const [name, setName] = useState(() => readPref(NAME_KEY, ""));
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [promoted, setPromoted] = useState(null);
  const [full, setFull] = useState(false);
  const [open, setOpen] = useState(new Set([1]));
  const edits = useStepEdits(buf, setBuf, { onError: setError });
  const { selected, setSelected } = edits;

  // A fresh plan (another recommendation, or a save) resets the view.
  useEffect(() => {
    setEditing(false);
    setSelected(null);
    setError(null);
    setPromoted(null);
    setShowing(plan.draft ? "draft" : "approved");
  }, [plan]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => writePref(VIEW_KEY, view), [view]);
  useEffect(() => writePref(NAME_KEY, name), [name]);

  // Escape leaves full screen.
  useEffect(() => {
    if (!full) return undefined;
    const onKey = (e) => e.key === "Escape" && setFull(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [full]);

  const draftView = !editing && showing === "draft" && !!plan.draft;
  const steps = editing ? buf : draftView ? plan.draft.steps : plan.steps;
  const planLike = useMemo(() => (editing || draftView ? { ...plan, status: "draft" } : plan), [plan, editing, draftView]);
  const liveChanges = useMemo(() => (editing ? diffSteps(plan.steps, buf, ACTORS) : null), [editing, plan.steps, buf]);
  const changes = editing ? liveChanges : draftView ? plan.draft.changes : null;
  const { marks, removed } = useMemo(() => marksFrom(changes), [changes]);
  const badge = useMemo(() => badgeFor(planLike), [planLike]);

  if (!plan.pathway) {
    return (
      <div className="banner warn">
        This recommendation type is not in a playbook yet, so it has no implementation plan. Add it to one in the Playbooks tab.
      </div>
    );
  }

  const rec = plan.rec;
  const num = numbers(steps);
  const canSave = plan.storage_ready && plan.history_ready;

  // ── editing ──
  function startEdit() {
    setBuf((plan.draft?.steps || plan.steps).map((s) => ({ ...s })));
    setNotes(plan.draft?.notes || "");
    setSelected(null);
    setError(null);
    setView("canvas");
    setEditing(true);
  }

  // ── saving ──
  async function act(method, path, body) {
    setBusy(true);
    setError(null);
    try {
      const p = await call(method, `/blocks/${blockId}/recommendations/${recId}/plan${path}`, body);
      onSaved?.(p);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const saveDraft = () => act("PUT", "", { steps: buf, status: "draft", made_by: name, notes });
  const saveAndApprove = () => {
    if (!window.confirm("Save and approve? These steps become the plan the block page and everything else read.")) return;
    act("PUT", "", { steps: buf, status: "final", made_by: name, notes });
  };
  const approve = () => {
    if (!window.confirm(`Approve ${plan.draft.made_by}'s draft? Its steps become the plan the block page and everything else read.`)) return;
    act("POST", "/approve", { made_by: name });
  };
  const discard = () => {
    if (!window.confirm(`Discard ${plan.draft.made_by}'s draft? The audit trail keeps a record of it.`)) return;
    act("POST", "/discard", { made_by: name });
  };

  // An approved plan's steps become the playbook's, so the next block starts
  // from what the SME settled on rather than the draft guess.
  async function promote() {
    const msg = `Make these ${plan.steps.length} steps the playbook for every "${plan.pathway.name}" implementation plan? ` +
      "Implementation plans already approved keep their own steps.";
    if (!window.confirm(msg)) return;
    setError(null);
    try {
      const t = await call("PUT", `/pathways/${plan.pathway.key}/steps`, {
        steps: plan.steps, updated_by: plan.reviewed_by,
        note: `From the approved plan for ${rec.rec_label} on block ${blockId}`,
      });
      setPromoted(`Saved as playbook ${t.version}.`);
    } catch (e) {
      setError(e.message);
    }
  }

  // ── selection ──
  const selStep = steps.find((s) => s.id === selected) || removed.find((s) => s.id === selected) || null;
  const selRemoved = !!selStep && !steps.some((s) => s.id === selected);

  function jump(n) {
    const s = steps[n - 1];
    if (!s) return;
    if (view === "canvas") { setSelected(s.id); return; }
    setOpen((prev) => new Set(prev).add(n));
    requestAnimationFrame(() => {
      document.getElementById(`plan-step-${recId}-${n}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  function toggle(n) {
    setOpen((prev) => {
      const next = new Set(prev);
      next.has(n) ? next.delete(n) : next.add(n);
      return next;
    });
  }

  const waiting = editing ? [] : steps.map((s, i) => (needsSme(s, planLike) ? i + 1 : null)).filter(Boolean);
  const est = draftView ? plan.draft.estimate : plan.estimate;

  const inspector = selStep && (
    <StepInspector
      s={selStep} n={selRemoved ? selStep.n : num[selStep.id]} steps={steps}
      mark={marks[selStep.id]} removed={selRemoved} editable={editing}
      onChange={(fields) => edits.patch(selStep.id, fields)}
      onDelete={() => edits.remove(selStep.id)}
      onAddAfter={() => edits.addAfter(selStep.id)}
      onSelect={setSelected}
      onClose={() => setSelected(null)}
    >
      {!editing && !selRemoved && <StepBody s={selStep} plan={planLike} onJump={jump} onFunding={onFunding} onZoning={onZoning} />}
    </StepInspector>
  );

  // Full screen has room for the step beside the canvas; the panel, only over it.
  const canvas = (fill) => (
    <div className={"plan-canvas-wrap" + (selStep ? " has-inspector" : "") + (editing ? " editing" : "") + (fill ? " side" : "")}>
      <PlanCanvas
        steps={steps} marks={marks} removed={removed} selectedId={selected} onSelect={setSelected}
        editable={editing} onConnect={edits.connect} onDisconnect={edits.disconnect} badge={badge} fill={fill}
      />
      {inspector}
    </div>
  );

  const toolbar = (
    <div className="plan-toolbar">
      <ActorLegend steps={steps} />
      <span className="spacer" />
      {editing && <button type="button" className="pl-bar-btn" onClick={() => edits.addAfter(selected)}>+ Step</button>}
      {!editing && (
        <span className="seg" role="group" aria-label="Show the plan as">
          <button type="button" aria-pressed={view === "canvas"} onClick={() => setView("canvas")}>Canvas</button>
          <button type="button" aria-pressed={view === "list"} onClick={() => setView("list")}>List</button>
        </span>
      )}
      {view === "canvas" && (
        <button type="button" className="pl-bar-btn" onClick={() => setFull((f) => !f)}>{full ? "Exit full screen" : "Full screen"}</button>
      )}
    </div>
  );

  return (
    <div className="plan">
      <div className="plan-who kv">
        Playbook: <b>{plan.pathway.name}</b> · {plan.pathway.who} · {plan.pathway.branch}
      </div>

      <div className="plan-state">
        {plan.status === "final" ? (
          <span className="state-chip ok" title="This is the plan the block page and everything else read">
            ✓ Approved{plan.revision ? ` · version ${plan.revision}` : ""}
          </span>
        ) : (
          <span className="state-chip" title="Nobody has approved a plan for this recommendation yet">From the playbook · not approved yet</span>
        )}
        {plan.status === "final" && <span className="kv">by {plan.reviewed_by}{plan.finalized_at ? ` on ${when(plan.finalized_at)}` : ""}</span>}
        <span className="spacer" />
        <Estimate est={est} editing={editing} />
      </div>

      {!canSave && (
        <div className="banner warn">
          Saving is off until the <code>implementation_plans</code> and <code>implementation_plan_revisions</code> tables
          exist. Run the IMPLEMENTATION PLANS and IMPLEMENTATION PLAN REVISIONS sections of <code>backend/db/schema.sql</code> in
          the Supabase SQL editor. Viewing works.
        </div>
      )}

      {plan.draft && !editing && (
        <div className="plan-draft-bar">
          <div>
            <b>Draft by {plan.draft.made_by}</b>, {when(plan.draft.created_at)}: {changeSummary(plan.draft.changes)}.
            {" "}Waiting for approval; the block page still shows the {plan.status === "final" ? "approved plan" : "playbook"}.
            {plan.draft.notes && <div className="assess">“{plan.draft.notes}”</div>}
          </div>
          <div className="plan-draft-actions">
            <span className="seg" role="group" aria-label="Show">
              <button type="button" aria-pressed={showing === "draft"} onClick={() => setShowing("draft")}>Draft</button>
              <button type="button" aria-pressed={showing === "approved"} onClick={() => setShowing("approved")}>
                {plan.status === "final" ? "Approved" : "Playbook"}
              </button>
            </span>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name" aria-label="Your name" />
            <button type="button" className="primary" onClick={approve} disabled={busy || !name.trim() || !canSave}>Approve</button>
            <button type="button" onClick={discard} disabled={busy || !name.trim() || !canSave}>Discard</button>
          </div>
        </div>
      )}

      {editing && (
        <div className="plan-attention editing-note">
          Editing{liveChanges?.count ? ` · ${changeSummary(liveChanges)}` : ""}. Click a step to edit it. Drag from the dot under a
          step to another step to make that one wait on it; click a line to remove the link.
        </div>
      )}

      {waiting.length > 0 && (
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

      {toolbar}

      {view === "canvas" || editing ? (
        full ? (
          <>
            <div className="plan-canvas-placeholder kv">Showing in full screen. <button type="button" className="link" onClick={() => setFull(false)}>Close it</button></div>
            {createPortal(
              <div className="plan-full" role="dialog" aria-label={`Implementation plan: ${rec.rec_label}`}>
                <div className="plan-full-head">
                  <b>{rec.rec_label}</b><span className="kv"> · {plan.pathway.name}{draftView ? " · draft" : editing ? " · editing" : ""}</span>
                  <span className="spacer" />
                  <ActorLegend steps={steps} />
                  {editing && <button type="button" className="pl-bar-btn" onClick={() => edits.addAfter(selected)}>+ Step</button>}
                  <button type="button" className="pl-bar-btn" onClick={() => setFull(false)}>Exit full screen</button>
                </div>
                {canvas(true)}
              </div>,
              document.body,
            )}
          </>
        ) : canvas(false)
      ) : (
        <ol className="plan-steps">
          {steps.map((s, i) => {
            const n = i + 1;
            const isOpen = open.has(n);
            return (
              <li key={s.id} id={`plan-step-${recId}-${n}`}
                  className={"plan-step" + (isOpen ? " open" : "") + (needsSme(s, planLike) ? " needs-sme" : "")}>
                <button type="button" className="ps-toggle" aria-expanded={isOpen} onClick={() => toggle(n)}>
                  <span className="ps-n">{n}</span>
                  <span className="ps-text">
                    <span className="ps-title">{s.title}</span>
                    <span className="ps-meta">
                      <ActorChip kind={s.actor} owner={s.owner} /> · {s.time || "No set time"} · {afterText(s, num).toLowerCase()}
                      {s.condition && <> · only if {s.condition.toLowerCase()}</>}
                    </span>
                  </span>
                  {!isOpen && <StepMarker s={s} plan={planLike} mark={marks[s.id]} />}
                  <span className="chev" aria-hidden="true">{isOpen ? "⌃" : "›"}</span>
                </button>
                {isOpen && (
                  <div className="ps-body">
                    {s.how && <p className="si-how">{s.how}</p>}
                    <StepBody s={s} plan={planLike} onJump={jump} onFunding={onFunding} onZoning={onZoning} />
                  </div>
                )}
              </li>
            );
          })}
          {removed.map((s) => (
            <li key={s.id} className="plan-step removed">
              <div className="ps-toggle"><span className="ps-n">–</span><span className="ps-text"><span className="ps-title">{s.title}</span>
                <span className="ps-meta">Removed in this draft (was step {s.n})</span></span></div>
            </li>
          ))}
        </ol>
      )}

      {error && <div className="banner err" style={{ marginTop: ".75rem" }}>{error}</div>}

      {editing ? (
        <div className="plan-edit">
          <div className="plan-edit-row">
            <label>
              Your name
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Required, for the audit trail" />
            </label>
            <label className="grow">
              What you changed and why
              <input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Shown with this draft in the audit trail" />
            </label>
          </div>
          <div className="btn-row">
            <button onClick={saveDraft} disabled={busy || !canSave || !name.trim()}>{busy ? "Saving…" : "Save draft"}</button>
            <button className="primary" onClick={saveAndApprove} disabled={busy || !canSave || !name.trim()}>Save and approve</button>
            <button onClick={() => { setEditing(false); setSelected(null); setError(null); }} disabled={busy}>Cancel</button>
          </div>
        </div>
      ) : (
        <div className="btn-row">
          <button className="primary" onClick={startEdit}>
            {plan.draft ? "Edit the draft" : plan.status === "final" ? "Edit implementation plan" : "Review and edit"}
          </button>
          {plan.status === "final" && !plan.draft && !promoted && (
            <button onClick={promote} title="Every implementation plan from this playbook that nobody has approved yet will start from these steps">
              Use as playbook template
            </button>
          )}
          {plan.notes && <span className="kv">Notes: {plan.notes}</span>}
        </div>
      )}
      {promoted && <div className="banner ok" style={{ marginTop: ".75rem" }}>{promoted}</div>}
    </div>
  );
}
