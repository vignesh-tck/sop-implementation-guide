import { Fragment, useState } from "react";
import { money } from "../api.js";
import { rankRecs } from "./AllRecommendations.jsx";
import { FundingList } from "./PlanView.jsx";
import ProfileCard from "./ProfileCard.jsx";
import { FitRow } from "./ReviewTable.jsx";
import { fitKey } from "./useBlockFunding.js";

// The block's funding review, shown in place of the map. Three steps: analyze
// proposes programs for every recommendation, the reviewer checks them and
// approves once for the block, then gold rolls the result into the block's
// feasibility profile. One row per recommendation shows its strongest match;
// opening a row shows every program assessed for it, where individual matches
// can be kept, dropped or rescored before approving.

function amountOf(f) {
  if (!f) return "—";
  return f.award_amount_min || f.award_amount_max ? `${money(f.award_amount_min)} – ${money(f.award_amount_max)}` : "varies";
}

// Which of the three steps the block is on, and the rows to show for it.
function phaseOf(s) {
  if (s.analyzing) return "running";
  if (s.thread && !s.thread.approved) return "review";
  if (s.thread?.approved) return "approved";
  if (s.signals?.recommendations?.length) return "earlier";
  return "none";
}

function buildRows(s, phase) {
  const floor = s.review?.default_floor ?? 0.3;
  if (phase === "review" || phase === "approved") {
    return rankRecs(s.review.recommendations || []).map((rec) => {
      const fits = [...(rec.fits || [])]
        .map((f) => ({ f, e: s.edits[fitKey(rec.rec_id, f.program_name)] || { fit_score: f.fit_score, approved: false } }))
        .sort((a, b) => (b.e.fit_score ?? -1) - (a.e.fit_score ?? -1));
      const kept = fits.filter((x) => x.e.approved);
      return { rec, fits, kept, top: kept[0]?.f, floor };
    });
  }
  const funded = new Map((s.signals?.recommendations || []).map((g) => [g.rec_id, g]));
  return rankRecs(s.recs || []).map((rec) => {
    const g = funded.get(rec.rec_id);
    const fits = [...(g?.fits || [])].sort((a, b) => (b.fit_score ?? 0) - (a.fit_score ?? 0));
    return { rec, fits: fits.map((f) => ({ f, e: f })), kept: fits.map((f) => ({ f, e: f })), top: fits[0], floor };
  });
}

const STATUS = {
  running: () => ["Analyzing", "run"],
  none: () => ["Not run", "idle"],
  review: (r) => (r.kept.length ? ["Proposed", "prop"] : ["No match", "idle"]),
  approved: (r) => (r.kept.length ? ["Approved", "ok"] : ["Not funded", "idle"]),
  earlier: (r) => (r.kept.length ? ["Approved", "ok"] : ["No funding", "idle"]),
};

function StepChips({ phase, gold }) {
  const done = {
    analyze: phase !== "none" && phase !== "running",
    approve: phase === "approved" || phase === "earlier",
    gold: !!gold,
  };
  const current = phase === "none" || phase === "running" ? "analyze" : phase === "review" ? "approve" : done.gold ? null : "gold";
  const chip = (k, label) => (
    <span className={"step-chip" + (done[k] ? " done" : "") + (current === k ? " current" : "")}>
      {done[k] && "✓ "}{label}
    </span>
  );
  return <div className="step-chips">{chip("analyze", "1 · Analyze")}{chip("approve", "2 · Approve")}{chip("gold", "3 · Compute gold")}</div>;
}

// Every program assessed for one recommendation, while it can still be changed.
function FitPicker({ row, onEdit }) {
  const [showWeak, setShowWeak] = useState(false);
  const strong = row.fits.filter((x) => x.e.approved || (x.e.fit_score ?? 0) >= row.floor);
  const shown = showWeak ? row.fits : strong;
  const weak = row.fits.length - strong.length;
  return (
    <div className="fit-picker">
      {row.fits.length === 0 ? (
        <p className="assess">No programs were assessed for this recommendation.</p>
      ) : (
        <table>
          <thead><tr><th>Keep</th><th>Fit</th><th>Program / why it fits / source</th><th>Deadline</th><th>Award</th></tr></thead>
          <tbody>
            {shown.map(({ f, e }) => (
              <FitRow key={f.program_name} recId={row.rec.rec_id} f={f} edit={{ ...e, floor: row.floor }}
                onChange={(p) => onEdit(fitKey(row.rec.rec_id, f.program_name), p)} />
            ))}
          </tbody>
        </table>
      )}
      {weak > 0 && (
        <button type="button" className="link" onClick={() => setShowWeak((v) => !v)}>
          {showWeak ? "Hide weaker matches" : `Show ${weak} weaker match${weak > 1 ? "es" : ""} (below ${row.floor})`}
        </button>
      )}
    </div>
  );
}

export default function FundingWorkspace({ block, s, actions, onClose }) {
  const [openRow, setOpenRow] = useState(null);
  const phase = phaseOf(s);
  const rows = buildRows(s, phase);
  const id = block.id;
  const total = rows.length;
  const matched = rows.filter((r) => r.kept.length).length;
  const programs = new Set(rows.flatMap((r) => r.kept.map((x) => x.f.program_name))).size;
  const keptFits = rows.reduce((n, r) => n + r.kept.length, 0);
  const failed = phase === "review" ? rows.flatMap((r) => r.fits).filter((x) => x.f.assessment_failed).length : 0;
  const canOpen = phase === "review" || phase === "approved" || phase === "earlier";

  let primary;
  if (phase === "none") primary = <button className="primary" onClick={() => actions.analyze(id)}>Run analysis</button>;
  else if (phase === "running") primary = <button disabled>Analyzing…</button>;
  else if (phase === "review") {
    primary = (
      <button className="primary" onClick={() => actions.approve(id)} disabled={s.approving}>
        {s.approving ? "Approving…" : `Approve ${keptFits} match${keptFits === 1 ? "" : "es"} for the block`}
      </button>
    );
  } else if (phase === "approved" && !s.goldProfile) {
    primary = (
      <button className="primary" onClick={() => actions.computeGold(id)} disabled={s.computingGold}>
        {s.computingGold ? "Computing…" : "Compute gold"}
      </button>
    );
  } else primary = <button onClick={() => actions.analyze(id)}>Re-run analysis</button>;

  return (
    <div className="fw">
      <div className="fw-head">
        <div className="fw-title">
          <div className="eyebrow-sm">Block {id} · {block.street_name || "(unnamed)"}</div>
          <h2>Funding analysis</h2>
          <p className="assess">Proposes funding for all {total || ""} recommendations. You approve once for the block.</p>
        </div>
        <div className="fw-actions">
          {primary}
          <button type="button" className="fw-close" aria-label="Close funding review, back to the map" onClick={onClose}>✕</button>
        </div>
      </div>

      <StepChips phase={phase} gold={s.goldProfile} />

      {phase === "running" && (
        <div className="fw-progress">
          <div className="bar"><div /></div>
          <div className="assess">
            Matching funding to {total} recommendations · usually 15–45 s · you can go back to the map or open a
            recommendation meanwhile
          </div>
        </div>
      )}
      {s.analyzeError && <div className="banner err">Analyze failed — {s.analyzeError}</div>}
      {s.approveError && <div className="banner err">Approve failed — {s.approveError}</div>}
      {s.goldError && <div className="banner err">Gold failed — {s.goldError}</div>}

      {canOpen && (
        <div className="tiles fw-stats">
          <div className="tile"><span className="tile-k">Matched</span><span className="tile-v big-v">{matched} of {total}</span></div>
          <div className="tile"><span className="tile-k">No match</span><span className="tile-v big-v">{total - matched}</span></div>
          <div className="tile"><span className="tile-k">Distinct programs</span><span className="tile-v big-v">{programs}</span></div>
        </div>
      )}

      {phase === "review" && (
        failed > 0 ? (
          <div className="banner warn">
            <b>{failed} match{failed > 1 ? "es were" : " was"} not assessed by the model.</b> Their 0.50 scores are
            placeholders, not analysis. Check the server log before trusting this run.
          </div>
        ) : (
          <div className="banner warn">
            Check the proposed source on each recommendation (open a row to change it), then approve once for the whole block.
          </div>
        )
      )}
      {phase === "approved" && s.approveResult && (
        <div className="banner ok">
          Approved for the block: kept {s.approveResult.kept} of {s.approveResult.total} matches. Each recommendation's
          plan now shows its funding.{!s.goldProfile && " Next: compute gold."}
        </div>
      )}
      {phase === "earlier" && (
        <div className="banner ok">
          Funding was approved in an earlier session. Re-run the analysis to review it again or to compute gold.
        </div>
      )}

      <div className="fw-table">
        <table>
          <thead>
            <tr>
              <th>#</th><th>Recommendation</th>
              <th>{phase === "review" || phase === "running" || phase === "none" ? "Proposed source" : "Funding source"}</th>
              <th>Amount</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              const [label, cls] = STATUS[phase](r);
              const down = r.rec.direction === "Decrease";
              const isOpen = openRow === r.rec.rec_id;
              const more = r.kept.length - 1;
              return (
                <Fragment key={r.rec.rec_id}>
                  <tr className={canOpen ? "openable" + (isOpen ? " open" : "") : ""}
                    onClick={canOpen ? () => setOpenRow(isOpen ? null : r.rec.rec_id) : undefined}>
                    <td className="n">{i + 1}</td>
                    <td>
                      <span className={"pill " + (down ? "no" : "yes")}>{down ? "reduce" : "increase"}</span>{" "}
                      <b>{r.rec.rec_label}</b>
                    </td>
                    <td>
                      {r.top ? r.top.program_name : <span className="assess">—</span>}
                      {more > 0 && <span className="assess"> +{more} more</span>}
                    </td>
                    <td className="nowrap">{r.top ? amountOf(r.top) : <span className="assess">—</span>}</td>
                    <td className="nowrap">
                      <span className={"status-chip " + cls}>{label}</span>
                      {canOpen && <span className="chev" aria-hidden="true">{isOpen ? "⌃" : "›"}</span>}
                    </td>
                  </tr>
                  {isOpen && (
                    <tr className="fw-detail">
                      <td />
                      <td colSpan={4}>
                        {phase === "review" ? (
                          <FitPicker row={r} onEdit={(key, p) => actions.setEdit(id, key, p)} />
                        ) : r.kept.length ? (
                          <FundingList funding={r.kept.map((x) => x.f)} withNarrative />
                        ) : (
                          <p className="assess">No funding approved for this recommendation.</p>
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
            {!rows.length && (
              <tr><td colSpan={5} className="assess">{s.loading ? "Loading recommendations…" : "No recommendations on this block."}</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {phase === "review" && (
        <label className="fw-note">
          Optional note recorded with every match you keep
          <textarea rows={2} placeholder="approved" value={s.feedback} onChange={(e) => actions.setFeedback(id, e.target.value)} />
        </label>
      )}

      {s.goldProfile && <ProfileCard profile={s.goldProfile} title="Gold layer — block implementation profile" />}
      {!s.goldProfile && phase === "earlier" && s.profile && (
        <ProfileCard profile={s.profile} title="Block implementation profile (from an earlier run)" />
      )}
    </div>
  );
}
