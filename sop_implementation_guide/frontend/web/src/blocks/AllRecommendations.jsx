import { useCallback, useEffect, useRef, useState } from "react";
import { num } from "../api.js";
import { BAND } from "./FeasibilityView.jsx";
import { estimateText } from "./plan/graph.js";

// The block's recommendations, from GET /blocks/{id}/plans, ranked by predicted
// gain. This is the block's front page: one row per recommendation and nothing
// else, so picking one is the obvious next move. Each row's dot is that
// recommendation's own zoning answer, so what can be built shows before opening.
// Plans show as they were approved: a draft waiting for approval is flagged,
// but its steps and time estimate don't show here until someone approves it.

export const ZONING_DOT = {
  by_right: { cls: "ok", text: "Zoning: permitted by right" },
  special_exception: { cls: "cond", text: "Zoning: needs a special exception" },
  prohibited: { cls: "no", text: "Zoning: prohibited" },
};
const NOT_CHECKED = { cls: "none", text: "Zoning: not checked or not resolved" };

// Highest predicted gain first; recommendations without a score go last.
export function rankRecs(recs) {
  return [...(recs || [])].sort(
    (a, b) => (b.predicted_score_increase ?? -Infinity) - (a.predicted_score_increase ?? -Infinity)
  );
}

// Quick questions on one recommendation, sent to the block assistant with that recommendation attached.
function quickQuestions(r) {
  const gain = r.predicted_score_increase != null ? `${r.predicted_score_increase >= 0 ? "+" : ""}${num(r.predicted_score_increase, 1)}` : null;
  return [
    gain ? `Why ${gain} for this block?` : "Why is this recommended here?",
    "What does the work involve?",
    "Who has to act, and how long?",
    "Can it share a plan with another?",
  ];
}

function AskMenu({ rec, onAsk, onClose }) {
  const ref = useRef(null);
  const [own, setOwn] = useState("");
  useEffect(() => {
    const down = (e) => { if (ref.current && !ref.current.contains(e.target)) onClose(); };
    const key = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("mousedown", down); document.removeEventListener("keydown", key); };
  }, [onClose]);
  const ask = (q) => { onAsk(rec, q); onClose(); };
  return (
    <div className="ask-menu" ref={ref} role="menu">
      {quickQuestions(rec).map((q) => (
        <button type="button" role="menuitem" key={q} onClick={() => ask(q)}>{q}</button>
      ))}
      <form onSubmit={(e) => { e.preventDefault(); if (own.trim()) ask(own.trim()); }}>
        <input autoFocus value={own} onChange={(e) => setOwn(e.target.value)} placeholder="Or ask your own…" />
      </form>
    </div>
  );
}

// The recommendation's feasibility in its row: the approved score, or that one is being assessed or reviewed.
function FeasibilityNote({ f }) {
  if (!f) return null;
  if (f.status === "running") return <> · <span className="assess">assessing feasibility…</span></>;
  if (f.status === "in_review") return <> · <span className="edited-text">feasibility awaiting review</span></>;
  if (f.score == null) return null;
  const band = BAND[f.band] || {};
  return (
    <> · <span className={"feas-note " + (band.cls || "")} title={`${band.text || f.band}${f.stale ? " · its inputs changed since" : ""}`}>
      feasibility {f.score}
    </span>{f.stale && <> · <span className="edited-text">stale, assess again</span></>}</>
  );
}

export default function AllRecommendations({ recommendations, onSelect, onAsk }) {
  const [menuFor, setMenuFor] = useState(null);
  const closeMenu = useCallback(() => setMenuFor(null), []);
  if (!recommendations) return null;
  if (!recommendations.length) return <div className="card assess">No recommendations on this block.</div>;
  const sorted = rankRecs(recommendations);
  const approved = recommendations.filter((r) => r.plan_status === "final").length;
  const waiting = recommendations.filter((r) => r.draft_pending).length;
  const playbook = recommendations.filter((r) => r.pathway_key && r.plan_status !== "final").length;

  return (
    <div className="rec-list-wrap">
      <div className="rec-list-head">
        <h4>Recommendations ({recommendations.length})</h4>
        <span className="legend-dots" aria-hidden="true">
          <span className="zdot ok" /> by right <span className="zdot cond" /> conditional <span className="zdot none" /> not checked
        </span>
      </div>
      <div className="plans-summary" title="Implementation plans on this block. Only approved plans count as reviewed.">
        <span className="ok-text"><b>{approved}</b> plan{approved === 1 ? "" : "s"} approved</span>
        {waiting > 0 && <span className="edited-text"><b>{waiting}</b> draft{waiting === 1 ? "" : "s"} waiting for approval</span>}
        {playbook > 0 && <span className="assess"><b>{playbook}</b> still the playbook's</span>}
      </div>
      <ol className="rec-list">
        {sorted.map((r) => {
          const z = ZONING_DOT[r.zoning_best] || NOT_CHECKED;
          const down = r.direction === "Decrease";
          return (
            <li key={r.rec_id} className={onAsk ? "rec-item" : undefined}>
              <button type="button" className="rec-row" onClick={() => onSelect(r.rec_id)}>
                <span className={"zdot " + z.cls} title={z.text} aria-label={z.text} />
                <span className={"pill " + (down ? "no" : "yes")}>{down ? "reduce" : "increase"}</span>
                <span className="rec-name">
                  <span className="rec-label">{r.rec_label}</span>
                  <span className="rec-dim">
                    {r.dimension || "—"}
                    {r.funding_count > 0 && <> · <span className="ok-text">{r.funding_count} funding</span></>}
                    {r.plan_status === "final" && <> · <span className="ok-text">plan approved</span></>}
                    {r.draft_pending && <> · <span className="edited-text" title={`Draft by ${r.draft_by}`}>draft waiting</span></>}
                    {estimateText(r.estimate) && <> · ~{estimateText(r.estimate)}</>}
                    <FeasibilityNote f={r.feasibility} />
                  </span>
                </span>
                {r.predicted_score_increase != null && (
                  <span className="rec-gain">{r.predicted_score_increase >= 0 ? "+" : ""}{num(r.predicted_score_increase, 1)}</span>
                )}
                <span className="chev" aria-hidden="true">›</span>
              </button>
              {onAsk && (
                <button
                  type="button"
                  className="rec-ask"
                  aria-haspopup="menu"
                  aria-expanded={menuFor === r.rec_id}
                  title={`Ask about ${r.rec_label}`}
                  onClick={() => setMenuFor(menuFor === r.rec_id ? null : r.rec_id)}
                >
                  Ask
                </button>
              )}
              {menuFor === r.rec_id && <AskMenu rec={r} onAsk={onAsk} onClose={closeMenu} />}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
