import { useState } from "react";
import { num } from "../api.js";

// The block's full recommendation list, from GET /blocks/{id}/plans. Picking one
// opens its implementation plan. Open by default: choosing a recommendation is
// where the planner starts, so the list is the entry point, not reference detail.
const STATUS = { final: "final", draft: "draft" };
const ZONING = {
  by_right: { text: "zoning: by right", cls: "ok" },
  special_exception: { text: "zoning: special exception", cls: "derived" },
  prohibited: { text: "zoning: prohibited", cls: "gone" },
};

export default function AllRecommendations({ recommendations, selectedRecId, onSelect }) {
  const [open, setOpen] = useState(true);
  if (!recommendations?.length) return null;

  const toggle = () => setOpen((o) => !o);

  // Highest predicted score increase first; recs without a score go last.
  const sorted = [...recommendations].sort(
    (a, b) => (b.predicted_score_increase ?? -Infinity) - (a.predicted_score_increase ?? -Infinity)
  );

  return (
    <div className="card">
      <div
        className="all-recs-head"
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle();
          }
        }}
      >
        <h4 style={{ margin: 0 }}>Recommendations ({recommendations.length}) — pick one to see its plan</h4>
        <span className="caret">{open ? "▾" : "▸"}</span>
      </div>
      {open && (
        <ol className="all-recs-list">
          {sorted.map((r, i) => (
            <li
              key={r.rec_id}
              className={"pick" + (r.rec_id === selectedRecId ? " selected" : "")}
              role="button"
              tabIndex={0}
              onClick={() => onSelect(r.rec_id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelect(r.rec_id);
                }
              }}
            >
              <span className="rank">{i + 1}</span>
              <span className={"pill " + (r.direction === "Decrease" ? "no" : "yes")}>
                {r.direction === "Decrease" ? "reduce" : "increase"}
              </span>
              <span className="label">
                {r.rec_label}
                <span className="dim"> · {r.pathway_name || "no pathway"}</span>
              </span>
              {r.funding_count > 0 && <span className="tag ok" title="Approved funding matches">funding {r.funding_count}</span>}
              {ZONING[r.zoning_best] && (
                <span className={"tag " + ZONING[r.zoning_best].cls} title="Best zoning outcome on this block, from the use tables">
                  {ZONING[r.zoning_best].text}
                </span>
              )}
              {STATUS[r.plan_status] && (
                <span className={"tag " + (r.plan_status === "final" ? "ok" : "edited")}>{STATUS[r.plan_status]}</span>
              )}
              {r.predicted_score_increase != null && (
                <span className="kv">+{num(r.predicted_score_increase, 1)}</span>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
