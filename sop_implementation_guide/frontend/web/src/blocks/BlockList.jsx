import { useState } from "react";
import { money, num } from "../api.js";

export default function BlockList({ blocks, selectedId, onSelect, showMap, onToggleMap }) {
  const [filter, setFilter] = useState("");
  const q = filter.trim().toLowerCase();
  const rows = blocks.filter((b) => !q || (b.street_name || "").toLowerCase().includes(q));

  return (
    <aside>
      <div className="aside-head">
        <div className="aside-head-row">
          <h2>Blocks ({blocks.length})</h2>
          <button type="button" className="map-toggle" aria-pressed={showMap} onClick={onToggleMap}>
            {showMap ? "Hide map" : "Show map"}
          </button>
        </div>
        <input placeholder="Filter by street…" value={filter} onChange={(e) => setFilter(e.target.value)} />
      </div>
      <div className="blocks">
        {rows.length === 0 && <div className="empty">No match.</div>}
        {rows.map((b) => (
          <div
            key={b.id}
            className="block-row"
            role="option"
            aria-selected={b.id === selectedId}
            onClick={() => onSelect(b.id)}
          >
            <div className="name">
              {b.street_name || "(unnamed)"} <span className="meta">#{b.id}</span>
            </div>
            <div className="meta">
              SoP {num(b.sop_index_norm)} · {money(b.median_hh_income)}
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
}
