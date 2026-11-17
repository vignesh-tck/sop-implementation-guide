import { Fragment, useState } from "react";
import { deadlineLabel, money, num, scoreColour } from "../api.js";
import { rankRecs } from "./AllRecommendations.jsx";
import ProfileCard from "./ProfileCard.jsx";
import { fitKey } from "./useBlockFunding.js";

// The block's funding review, shown in place of the map. Three steps: analyze
// proposes programs for every recommendation, the reviewer checks them and
// approves once for the block, then gold rolls the result into the block's
// feasibility profile.
//
// Simple by default, detail on request: a row is one line per recommendation;
// opening it lists the proposed programs with a keep box each; a program's
// fit score, reasoning and source sit behind its own "Details". Nothing the
// reviewer may need is gone, it is only further in.

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

// One program proposed for one recommendation. Editable while the review is
// open (keep box, fit score); read-only once approved.
function Match({ f, e, editable, floor, onEdit }) {
  const [open, setOpen] = useState(false);
  const dl = deadlineLabel(f.deadline);
  const score = e.fit_score;
  return (
    <li className={"match" + (editable && !e.approved ? " dropped" : "")}>
      <div className="match-main">
        {editable && (
          <input type="checkbox" checked={!!e.approved} aria-label={`Keep ${f.program_name}`}
            onChange={(ev) => onEdit({ approved: ev.target.checked })} />
        )}
        <span className="match-name">
          {f.program_name}
          {f.assessment_failed && <span className="warn-text" title="Not assessed by the model; 0.50 is a placeholder"> ⚠</span>}
        </span>
        <span className="assess match-facts">
          {amountOf(f)}{f.deadline && <> · <span className={"deadline" + (dl.soon ? " soon" : "")}>due {dl.text}</span></>}
        </span>
        <button type="button" className="link small" onClick={() => setOpen((o) => !o)}>{open ? "Hide" : "Details"}</button>
      </div>
      {open && (
        <div className="match-detail">
          <div>
            Fit{" "}
            {editable ? (
              <input type="number" className={"score-edit" + (e.edited ? " edited" : "")} min="0" max="1" step="0.05"
                value={score == null ? "" : score}
                style={{ color: e.edited ? "var(--amber)" : scoreColour(score) }}
                onChange={(ev) => {
                  const raw = ev.target.value;
                  // Rescoring a program keeps it: dropping an edited row by accident was the easy mistake.
                  onEdit({ fit_score: raw === "" ? null : parseFloat(raw), edited: true, approved: true });
                }} />
            ) : (
              <b style={{ color: scoreColour(score) }}>{num(score, 2)}</b>
            )}
            {editable && <span className="assess"> · model proposed {num(f.fit_score, 2)}, keep threshold {floor}</span>}
          </div>
          {f.narrative && <p>{f.narrative}</p>}
          {f.recommended_action && <p className="assess">Next action: {f.recommended_action}</p>}
          <div className="assess">
            Source:{" "}
            {f.source_url ? <a href={f.source_url} target="_blank" rel="noopener noreferrer">{f.source_name || "link"} ↗</a> : f.source_name || "none recorded"}
            {f.is_extracted && ` · extracted${f.extraction_confidence != null ? ` (confidence ${num(f.extraction_confidence, 2)})` : ""}`}
            {f.application_url && <> · <a href={f.application_url} target="_blank" rel="noopener noreferrer">apply ↗</a></>}
          </div>
        </div>
      )}
    </li>
  );
}

// The programs behind one row. While reviewing: the ones above the threshold or
// kept, with the weaker ones one click further. After approval: what was kept.
function MatchList({ row, editable, onEdit }) {
  const [showWeak, setShowWeak] = useState(false);
  const main = editable ? row.fits.filter((x) => x.e.approved || (x.e.fit_score ?? 0) >= row.floor) : row.kept;
  const weak = editable ? row.fits.filter((x) => !main.includes(x)) : [];
  const shown = showWeak ? [...main, ...weak] : main;
  if (!row.fits.length) return <p className="assess">No programs were assessed for this recommendation.</p>;
  return (
    <>
      {shown.length ? (
        <ul className="matches">
          {shown.map(({ f, e }) => (
            <Match key={f.program_name} f={f} e={e} editable={editable} floor={row.floor}
              onEdit={(p) => onEdit(fitKey(row.rec.rec_id, f.program_name), p)} />
          ))}
        </ul>
      ) : (
        <p className="assess">{editable ? `No program scored above ${row.floor}.` : "No funding approved for this recommendation."}</p>
      )}
      {weak.length > 0 && (
        <button type="button" className="link small" onClick={() => setShowWeak((v) => !v)}>
          {showWeak ? "Hide weaker matches" : `Show ${weak.length} weaker match${weak.length > 1 ? "es" : ""}`}
        </button>
      )}
    </>
  );
}

// The gold result in one line; the full profile on request.
function GoldSummary({ profile, title }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="gold-line">
      <span>{title}: feasibility <b>{num(profile.feasibility_score)}</b> / 100 · funding {num(profile.funding_score)} ·
        zoning {num(profile.zoning_score)}</span>{" "}
      <button type="button" className="link small" onClick={() => setOpen((o) => !o)}>{open ? "Hide profile" : "Show profile"}</button>
      {open && <ProfileCard profile={profile} title="Block implementation profile" />}
    </div>
  );
}

export default function FundingWorkspace({ block, s, actions, onClose }) {
  const [openRow, setOpenRow] = useState(null);
  const [noteOpen, setNoteOpen] = useState(false);
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
            Check the proposed source on each recommendation, then approve once for the whole block. Open a row to change what is kept.
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
              return (
                <Fragment key={r.rec.rec_id}>
                  <tr className={canOpen ? "openable" + (isOpen ? " open" : "") : ""}
                    onClick={canOpen ? () => setOpenRow(isOpen ? null : r.rec.rec_id) : undefined}>
                    <td className="n">{i + 1}</td>
                    <td>
                      <span className={"pill " + (down ? "no" : "yes")}>{down ? "reduce" : "increase"}</span>{" "}
                      <b>{r.rec.rec_label}</b>
                    </td>
                    <td>{r.top ? r.top.program_name : <span className="assess">—</span>}</td>
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
                        <MatchList row={r} editable={phase === "review"} onEdit={(key, p) => actions.setEdit(id, key, p)} />
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

      {phase === "review" && (noteOpen || s.feedback ? (
        <label className="fw-note">
          Note recorded with every match you keep
          <textarea rows={2} placeholder="approved" value={s.feedback} onChange={(e) => actions.setFeedback(id, e.target.value)} />
        </label>
      ) : (
        <button type="button" className="link small fw-note-link" onClick={() => setNoteOpen(true)}>+ Add a note to this approval</button>
      ))}

      {s.goldProfile && <GoldSummary profile={s.goldProfile} title="Gold computed" />}
      {!s.goldProfile && phase === "earlier" && s.profile && <GoldSummary profile={s.profile} title="Earlier profile" />}
    </div>
  );
}
