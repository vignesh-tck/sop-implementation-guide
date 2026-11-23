import { Fragment, useEffect, useState } from "react";
import { num } from "../api.js";
import { OUTCOME, Outcome, References, ZoningFinding } from "../zoning/ZoningParts.jsx";
import { REVIEWER_KEY, decisionOf, decisionProblem } from "./useBlockZoning.js";

// The block's zoning review, shown in place of the map, like the funding review.
// Run: one finding per recommendation against a Zoning version. Review: what
// the analysis could settle on its own comes pre-accepted; the SME decides the
// rest (accept, set the outcome with a reason, or leave open). Approve: once
// for the block, which makes the run the block's current zoning. Plans read
// nothing else.

const when = (iso) => (iso || "").slice(0, 16).replace("T", " ");
const SET_OUTCOMES = ["by_right", "special_exception", "prohibited", "not_applicable"];
const STATUS_TEXT = {
  running: "running", in_review: "awaiting approval", complete: "approved", failed: "failed", discarded: "discarded",
};

function versionText(versions, n) {
  if (n == null) return "—";
  const v = (versions || []).find((x) => x.version === n);
  return `Zoning v${n}${v?.name ? ` “${v.name}”` : ""}`;
}

function readReviewer() {
  try { return localStorage.getItem(REVIEWER_KEY) || ""; } catch { return ""; }
}

// The chip in a finding's row: where its decision stands.
function chip(item, d, editable) {
  if (!d || d.action === "open") {
    if (editable && !d) return item.flagged ? ["Needs SME", "warn"] : ["Settled", "ok"];
    return ["Left open", ""];
  }
  if (d.action === "set" && d.outcome !== item.proposed) return [`Set: ${OUTCOME[d.outcome]?.text || d.outcome}`, "prop"];
  return d.auto ? ["Settled", "ok"] : ["Accepted", "ok"];
}

function RunsBar({ block, s, actions, busy }) {
  const [showAll, setShowAll] = useState(false);
  const id = block.id;
  const versions = s.versions || [];
  const runs = (s.analyses || []).filter((a) => a.status !== "discarded");
  const current = runs.find((a) => a.pinned) || null;
  const latest = versions[0]?.version ?? null;
  const reviewing = runs.find((a) => a.status === "in_review");

  return (
    <div className="vbar">
      <div className="vbar-line">
        <span className="vbar-k">Current zoning</span>
        {current ? (
          <span>
            <b>{versionText(versions, current.dataset_version)}</b> · run #{current.id}{current.name ? ` “${current.name}”` : ""} ·{" "}
            {current.result ? `approved ${when(current.completed_at)}` : "made before zoning reviews, not approved"}
            {current.dataset_version != null && latest != null && current.dataset_version < latest && (
              <span className="tag derived" style={{ marginLeft: ".4rem" }} title="A newer Zoning version exists. Run against it and approve to update this block.">
                v{latest} available
              </span>
            )}
          </span>
        ) : (
          <span className="assess">No approved zoning review yet.</span>
        )}
        <span className="spacer" />
        <button type="button" className="link small" onClick={() => setShowAll((o) => !o)}>
          {showAll ? "Hide runs" : `All runs (${runs.length})`}
        </button>
      </div>

      {!reviewing && !s.running && (
        <div className="vbar-line vbar-run">
          <span className="vbar-k">Next run against</span>
          <select value={s.nextVersion ?? ""} disabled={busy || !versions.length}
                  onChange={(e) => actions.setRunOptions(id, { nextVersion: e.target.value === "" ? null : Number(e.target.value) })}>
            <option value="">{latest != null ? `Latest (v${latest}${versions[0]?.name ? ` “${versions[0].name}”` : ""})` : "Latest"}</option>
            {versions.map((v) => (
              <option key={v.id} value={v.version}>v{v.version}{v.name ? ` “${v.name}”` : ""} · {when(v.created_at).slice(0, 10)}</option>
            ))}
          </select>
          <input placeholder="Run name (optional)" value={s.runName} disabled={busy}
                 onChange={(e) => actions.setRunOptions(id, { runName: e.target.value })} />
        </div>
      )}

      {s.historyError && <div className="banner err" style={{ margin: ".5rem 0 0" }}>{s.historyError}</div>}

      {showAll && (
        <div className="table-wrap vbar-runs">
          <table>
            <thead><tr><th>Run</th><th>Zoning version</th><th>Status</th><th>Open</th><th>When</th><th></th></tr></thead>
            <tbody>
              {runs.length === 0 && <tr><td colSpan={6} className="assess">No runs yet.</td></tr>}
              {runs.map((a) => (
                <tr key={a.id} className={a.id === s.run?.id ? "sel" : ""}>
                  <td>
                    #{a.id}{a.name ? ` “${a.name}”` : ""}
                    {a.pinned && <span className="tag ok" style={{ marginLeft: ".35rem" }}>current</span>}
                  </td>
                  <td className="assess">{versionText(versions, a.dataset_version)}</td>
                  <td className="assess" title={a.error || ""}>
                    {a.status === "complete" && !a.result ? "made before reviews" : STATUS_TEXT[a.status] || a.status}
                  </td>
                  <td className="assess">{a.status === "complete" ? a.result?.open ?? "" : a.result?.flagged != null ? `${a.result.flagged} to decide` : ""}</td>
                  <td className="assess">{when(a.completed_at || a.created_at)}{a.created_by ? ` · ${a.created_by}` : ""}</td>
                  <td className="nowrap">
                    {a.id !== s.run?.id && a.result && (
                      <button type="button" className="link small" disabled={busy} onClick={() => actions.show(id, a.id)}>
                        {a.status === "in_review" ? "Review" : "View"}
                      </button>
                    )}{" "}
                    {a.status === "complete" && !a.pinned && a.result && (
                      <button type="button" className="link small" disabled={busy} onClick={() => actions.pin(id, a.id)}>Make current</button>
                    )}{" "}
                    {["running", "in_review", "failed"].includes(a.status) && (
                      <button type="button" className="link small" disabled={busy} onClick={() => actions.discard(id, a.id)}>Discard</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="muted-note">
            Each run is tied to the Zoning version it was made against, and holds its findings as they were then.
            Making a run current changes what this block's plans and gold read; other blocks are not affected. New
            versions come from a zoning pipeline in the Pipeline development tab.
          </div>
        </div>
      )}
    </div>
  );
}

// The SME's choice on one finding, while the run awaits approval.
function DecisionEditor({ item, d, onChange }) {
  const action = d?.action || (item.flagged ? null : "accept");
  const problem = decisionProblem(item, d);
  const proposedText = item.proposed ? OUTCOME[item.proposed]?.text || item.proposed : null;
  return (
    <div className="zdecide">
      <div className="zdecide-h">Your decision</div>
      <label className={!item.proposed ? "disabled" : ""}>
        <input type="radio" name={`d-${item.key}`} checked={action === "accept"} disabled={!item.proposed}
               onChange={() => onChange({ action: "accept", outcome: item.proposed })} />
        Accept the proposed outcome{proposedText ? <>: <b>{proposedText}</b></> : " (there is none: the analysis could not resolve it)"}
      </label>
      <label>
        <input type="radio" name={`d-${item.key}`} checked={action === "set"}
               onChange={() => onChange({ action: "set", outcome: d?.outcome && d.outcome !== item.proposed ? d.outcome : null })} />
        Set the outcome{" "}
        <select value={action === "set" ? d?.outcome || "" : ""}
                onChange={(e) => onChange({ action: "set", outcome: e.target.value || null })}>
          <option value="">Choose…</option>
          {SET_OUTCOMES.map((o) => <option key={o} value={o}>{OUTCOME[o].text}</option>)}
        </select>
      </label>
      <label>
        <input type="radio" name={`d-${item.key}`} checked={action === "open"}
               onChange={() => onChange({ action: "open", outcome: null })} />
        Leave open: the plan keeps showing the questions, and the score leaves it out
      </label>
      <textarea rows={2} value={d?.note || ""}
                placeholder={action === "set" ? "Why? Required when the outcome differs from the proposed one." : "Note (optional): your answer to the questions, or what you checked."}
                onChange={(e) => onChange({ note: e.target.value, ...(d ? {} : { action: item.flagged ? "open" : "accept", outcome: item.flagged ? null : item.proposed }) })} />
      {problem && <div className="warn-text">To approve: {problem}.</div>}
      {(!item.proposed || action === "set") && (
        <p className="assess">
          This decision is for this block only. To change how “{item.rec_label}” is read on every block, edit the
          recommendation readings in the zoning pipeline (Pipeline development tab).
        </p>
      )}
    </div>
  );
}

// The decision as approved, for a run that is no longer awaiting review.
function DecisionLine({ item }) {
  const d = item.decision;
  if (!d || d.action === "open") return <div className="zdecision open">Left open{d?.by ? ` by ${d.by}` : ""}: the questions below still need an answer.</div>;
  const txt = d.action === "set" && d.outcome !== item.proposed
    ? <>Set to <b>{OUTCOME[d.outcome]?.text}</b> (proposed: {item.proposed ? OUTCOME[item.proposed]?.text : "not resolved"})</>
    : d.auto ? <>Settled: <b>{OUTCOME[d.outcome]?.text}</b>, nothing to ask</> : <>Accepted: <b>{OUTCOME[d.outcome]?.text}</b></>;
  return (
    <div className="zdecision">
      {txt}{d.by ? ` · ${d.by}` : ""}{d.at ? `, ${when(d.at).slice(0, 10)}` : ""}
      {d.note && <div className="assess">“{d.note}”</div>}
    </div>
  );
}

export default function ZoningWorkspace({ block, s, actions, onClose }) {
  const id = block.id;
  const [openRow, setOpenRow] = useState(null);
  const [reviewer, setReviewer] = useState(readReviewer);
  useEffect(() => { try { localStorage.setItem(REVIEWER_KEY, reviewer); } catch { /* per-viewer convenience only */ } }, [reviewer]);
  useEffect(() => { actions.open(id); }, [id, actions.open]);
  useEffect(() => setOpenRow(null), [s.run?.id]);

  const run = s.run;
  const items = run?.result?.findings || null;
  const editable = run?.status === "in_review";
  const oldRun = run && run.status === "complete" && !items;
  const busy = s.running || s.approving;

  const decided = items ? items.map((i) => ({ i, d: editable ? decisionOf(s, i) : i.decision })) : [];
  const toDecide = decided.filter(({ i, d }) => i.flagged && !d).length;
  const leftOpen = decided.filter(({ d }) => !d || d.action === "open").length;
  const problems = editable ? decided.filter(({ i, d }) => decisionProblem(i, d)).length : 0;
  const acceptable = editable ? decided.filter(({ i, d }) => i.flagged && i.proposed && !d).length : 0;

  let primary;
  if (s.running) primary = <button disabled>Running…</button>;
  else if (editable) {
    primary = (
      <button className="primary" disabled={s.approving || problems > 0 || !reviewer.trim()}
              title={!reviewer.trim() ? "Enter your name below first" : problems ? "Some decisions are incomplete" : undefined}
              onClick={() => actions.approve(id, reviewer.trim())}>
        {s.approving ? "Approving…" : `Approve for the block${leftOpen ? ` (${leftOpen} left open)` : ""}`}
      </button>
    );
  } else primary = <button className={run ? "" : "primary"} onClick={() => actions.runReview(id)}>{run ? "Re-run zoning review" : "Run zoning review"}</button>;

  return (
    <div className="fw">
      <div className="fw-head">
        <div className="fw-title">
          <div className="eyebrow-sm">Block {id} · {block.street_name || "(unnamed)"}</div>
          <h2>Zoning review</h2>
          <p className="assess">Checks every recommendation against the zoning map and use tables of one Zoning version. You approve once for the block.</p>
        </div>
        <div className="fw-actions">
          {primary}
          <button type="button" className="fw-close" aria-label="Close zoning review, back to the map" onClick={onClose}>✕</button>
        </div>
      </div>

      <RunsBar block={block} s={s} actions={actions} busy={busy} />

      {s.runError && <div className="banner err">Run failed — {s.runError}</div>}
      {s.approveError && <div className="banner err">Approve failed — {s.approveError}</div>}
      {s.runLoading && !run && <div className="spin">Loading…</div>}

      {!run && !s.runLoading && !s.running && (
        <div className="banner warn">
          No zoning review yet. Until one is approved, this block's plans show zoning as not reviewed. Run one against
          the latest Zoning version, or pick a version above.
        </div>
      )}
      {oldRun && (
        <div className="banner warn">
          Run #{run.id} was made before zoning findings were reviewed, so it holds no findings to show. Run a zoning
          review and approve it; until then plans show zoning as not reviewed.
        </div>
      )}

      {items && (
        <>
          <div className="eyebrow-sm" style={{ margin: ".2rem 0 .4rem" }}>
            Run #{run.id}{run.name ? ` “${run.name}”` : ""} · {versionText(s.versions, run.dataset_version)} ·{" "}
            {editable ? "awaiting approval" : `approved by ${run.result.approved_by || "—"} ${when(run.result.approved_at)}`}
            {!run.pinned && run.status === "complete" && " · not this block's current zoning"}
          </div>
          <div className="tiles fw-stats">
            <div className="tile"><span className="tile-k">Recommendations</span><span className="tile-v big-v">{items.length}</span>
              <span className="tile-s">zones: {(run.result.zones || []).join(", ") || "none found"}</span></div>
            <div className={"tile" + (editable && toDecide ? " cond" : "")}>
              <span className="tile-k">{editable ? "Need your decision" : "Overridden"}</span>
              <span className="tile-v big-v">{editable ? toDecide : run.result.summary?.overridden ?? 0}</span>
            </div>
            <div className="tile">
              <span className="tile-k">{editable ? "Will be left open" : "Left open"}</span>
              <span className="tile-v big-v">{leftOpen}</span>
              {!editable && run.result.summary?.zoning_score != null && <span className="tile-s">zoning score {num(run.result.summary.zoning_score)}</span>}
            </div>
          </div>

          {editable ? (
            <div className="banner warn">
              Findings with nothing to ask are already accepted. Open each one marked <b>Needs SME</b>: accept the
              proposed outcome, set your own with a reason, or leave it open. Open findings keep their questions in the
              plan and are not scored.
              {acceptable > 0 && (
                <> <button type="button" className="link small" onClick={() => actions.acceptAllProposed(id)}>
                  Accept the proposed outcome on all {acceptable}
                </button></>
              )}
            </div>
          ) : s.approved && run.id === s.approved.analysis_id ? (
            <div className="banner ok">
              Approved for the block. Each recommendation's plan now shows this zoning{leftOpen ? `, with ${leftOpen} finding${leftOpen > 1 ? "s" : ""} left open` : ""}.
            </div>
          ) : null}

          <div className="fw-table">
            <table>
              <thead>
                <tr><th>#</th><th>Recommendation</th><th>Finding</th><th>Proposed</th><th>Decision</th></tr>
              </thead>
              <tbody>
                {decided.map(({ i, d }, n) => {
                  const [label, cls] = chip(i, d, editable);
                  const isOpen = openRow === i.key;
                  const down = i.direction === "Decrease";
                  return (
                    <Fragment key={i.key}>
                      <tr className={"openable" + (isOpen ? " open" : "")} onClick={() => setOpenRow(isOpen ? null : i.key)}>
                        <td className="n">{n + 1}</td>
                        <td>
                          <span className={"pill " + (down ? "no" : "yes")}>{down ? "reduce" : "increase"}</span>{" "}
                          <b>{i.rec_label}</b>
                          {i.rec_ids.length > 1 && <span className="assess" title="Listed under several dimensions; one decision covers all"> ×{i.rec_ids.length}</span>}
                        </td>
                        <td className="assess">{i.finding.summary}</td>
                        <td>{i.proposed ? <Outcome outcome={i.proposed} /> : <Outcome outcome="unknown" />}</td>
                        <td className="nowrap">
                          <span className={"status-chip " + cls}>{label}</span>
                          <span className="chev" aria-hidden="true">{isOpen ? "⌃" : "›"}</span>
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="fw-detail">
                          <td />
                          <td colSpan={4}>
                            {editable
                              ? <DecisionEditor item={i} d={d} onChange={(p) => actions.setDecision(id, i.key, p)} />
                              : <DecisionLine item={i} />}
                            <ZoningFinding z={i.finding} />
                            <div className="zoning-h refs-h">Sources</div>
                            <References refs={i.finding.references} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
                {!items.length && <tr><td colSpan={5} className="assess">No recommendations on this block.</td></tr>}
              </tbody>
            </table>
          </div>

          {editable && (
            <div className="zapprove">
              <label>
                Approving as
                <input value={reviewer} placeholder="Your name" onChange={(e) => setReviewer(e.target.value)} />
              </label>
              <label className="grow">
                Note on this approval (optional)
                <input value={s.note} onChange={(e) => actions.setNote(id, e.target.value)} />
              </label>
              {problems > 0 && <span className="warn-text">{problems} decision{problems > 1 ? "s need" : " needs"} finishing first.</span>}
            </div>
          )}
        </>
      )}
    </div>
  );
}
