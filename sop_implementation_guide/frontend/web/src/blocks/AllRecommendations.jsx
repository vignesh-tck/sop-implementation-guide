import { useState } from "react";
import { num } from "../api.js";

// Collapsed by default — this is the block's full recommendation list straight
// from block_recommendations, independent of whether funding analysis has ever
// run. It sits above the Flow card as a quick reference, not a wall of detail.
export default function AllRecommendations({ recommendations }) {
  const [open, setOpen] = useState(false);
  if (!recommendations?.length) return null;

  const toggle = () => setOpen((o) => !o);

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
        <h4 style={{ margin: 0 }}>All recommendations ({recommendations.length})</h4>
        <span className="caret">{open ? "▾" : "▸"}</span>
      </div>
      {open && (
        <ol className="all-recs-list">
          {recommendations.map((r, i) => (
            <li key={r.id}>
              <span className="rank">{i + 1}</span>
              <span className={"pill " + (r.direction === "Decrease" ? "no" : "yes")}>
                {r.direction === "Decrease" ? "reduce" : "increase"}
              </span>
              <span className="label">{r.rec_label}</span>
              <span className="dim">{r.dimension || "—"}</span>
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
