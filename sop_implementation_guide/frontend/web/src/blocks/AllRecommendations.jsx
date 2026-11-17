import { num } from "../api.js";

// The block's recommendations, from GET /blocks/{id}/plans, ranked by predicted
// gain. This is the block's front page: one row per recommendation and nothing
// else, so picking one is the obvious next move. Each row's dot is that
// recommendation's own zoning answer, so what can be built shows before opening.

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

export default function AllRecommendations({ recommendations, onSelect }) {
  if (!recommendations) return null;
  if (!recommendations.length) return <div className="card assess">No recommendations on this block.</div>;
  const sorted = rankRecs(recommendations);

  return (
    <div className="rec-list-wrap">
      <div className="rec-list-head">
        <h4>Recommendations ({recommendations.length})</h4>
        <span className="legend-dots" aria-hidden="true">
          <span className="zdot ok" /> by right <span className="zdot cond" /> conditional <span className="zdot none" /> not checked
        </span>
      </div>
      <ol className="rec-list">
        {sorted.map((r) => {
          const z = ZONING_DOT[r.zoning_best] || NOT_CHECKED;
          const down = r.direction === "Decrease";
          return (
            <li key={r.rec_id}>
              <button type="button" className="rec-row" onClick={() => onSelect(r.rec_id)}>
                <span className={"zdot " + z.cls} title={z.text} aria-label={z.text} />
                <span className={"pill " + (down ? "no" : "yes")}>{down ? "reduce" : "increase"}</span>
                <span className="rec-name">
                  <span className="rec-label">{r.rec_label}</span>
                  <span className="rec-dim">
                    {r.dimension || "—"}
                    {r.funding_count > 0 && <> · <span className="ok-text">{r.funding_count} funding</span></>}
                    {r.plan_status === "final" && <> · <span className="ok-text">plan final</span></>}
                    {r.plan_status === "draft" && <> · <span className="edited-text">plan draft</span></>}
                  </span>
                </span>
                {r.predicted_score_increase != null && (
                  <span className="rec-gain">{r.predicted_score_increase >= 0 ? "+" : ""}{num(r.predicted_score_increase, 1)}</span>
                )}
                <span className="chev" aria-hidden="true">›</span>
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
