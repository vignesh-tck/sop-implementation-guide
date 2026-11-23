import { useEffect, useRef, useState } from "react";
import { money, num } from "../api.js";
import AllRecommendations, { rankRecs } from "./AllRecommendations.jsx";
import RecommendationView from "./RecommendationView.jsx";
import { fundingStatus } from "./useBlockFunding.js";
import { zoningStatus } from "./useBlockZoning.js";

// A block in two levels. The front page is block-level only: its facts, where
// its funding and zoning reviews stand, and its recommendations. Picking a recommendation replaces
// that page with the recommendation's own view (RecommendationView), with a way
// back, so the list and a plan never compete for the same scroll.
//
// The block's data and its funding and zoning runs live in useBlockFunding and
// useBlockZoning, a level up; the reviews themselves open over the map
// (FundingWorkspace, ZoningWorkspace).

function FundingCard({ s, onOpen }) {
  const st = fundingStatus(s);
  return (
    <button type="button" className="funding-card" onClick={onOpen}>
      <span className="fc-label">Block funding</span>
      <span className={"status-chip " + { none: "idle", running: "run", review: "prop", done: "ok" }[st.key]}>{st.text}</span>
      {s?.signals?.analysis?.dataset_version != null && (
        <span className="assess" title="The Funding version this block's current funding run assessed">
          Funding v{s.signals.analysis.dataset_version}
        </span>
      )}
      <span className="spacer" />
      <span className="fc-go">{st.key === "none" ? "Run analysis" : "Open review"} →</span>
    </button>
  );
}

function ZoningCard({ zs, onOpen }) {
  const st = zoningStatus(zs);
  const cur = (zs?.analyses || []).find((a) => a.pinned && a.result);
  return (
    <button type="button" className="funding-card" onClick={onOpen}>
      <span className="fc-label">Block zoning</span>
      <span className={"status-chip " + { none: "idle", running: "run", review: "prop", done: "ok" }[st.key]}>{st.text}</span>
      {cur?.dataset_version != null && (
        <span className="assess" title="The Zoning version this block's approved zoning review was made against">
          Zoning v{cur.dataset_version}
        </span>
      )}
      <span className="spacer" />
      <span className="fc-go">{st.key === "none" ? "Run review" : "Open review"} →</span>
    </button>
  );
}

export default function BlockDetail({ block, s, zs, onOpenFunding, onOpenZoning, onRefresh }) {
  // null = the block's front page; otherwise the recommendation stepped into.
  const [selectedRecId, setSelectedRecId] = useState(null);
  // The panel scrolls as one; each move between list and recommendation starts at the top.
  const scrollRef = useRef(null);
  useEffect(() => { if (scrollRef.current) scrollRef.current.scrollTop = 0; }, [selectedRecId]);
  useEffect(() => setSelectedRecId(null), [block?.id]);

  if (!block) {
    return (
      <section className="detail">
        <div className="empty">Select a block to begin.</div>
      </section>
    );
  }

  const header = (
    <div className="block-head">
      <div className="eyebrow-sm">Block {block.id}</div>
      <h3>{block.street_name || "(unnamed)"}</h3>
      <div className="assess">{block.intersection_a || "—"} → {block.intersection_b || "—"}</div>
      <div className="chips">
        <span className="chip brand">SoP {num(block.sop_index_norm)} / 100</span>
        <span className="chip">Median HH income {money(block.median_hh_income)}</span>
        <span className="chip">Tract {block.tract_geoid || "—"}</span>
      </div>
    </div>
  );

  // Stepped into one recommendation: prev/next follow the list's order.
  const ranked = rankRecs(s.recs);
  const idx = ranked.findIndex((r) => r.rec_id === selectedRecId);
  if (selectedRecId != null && idx >= 0) {
    return (
      <section className="detail" ref={scrollRef}>
        {header}
        <RecommendationView
          blockId={block.id}
          recId={selectedRecId}
          refreshKey={`${s.version}-${zs?.version ?? 0}`}
          position={idx + 1}
          total={ranked.length}
          onBack={() => setSelectedRecId(null)}
          onPrev={idx > 0 ? () => setSelectedRecId(ranked[idx - 1].rec_id) : null}
          onNext={idx < ranked.length - 1 ? () => setSelectedRecId(ranked[idx + 1].rec_id) : null}
          onOpenFunding={onOpenFunding}
          onOpenZoning={onOpenZoning}
          onChanged={onRefresh}
        />
      </section>
    );
  }

  return (
    <section className="detail" ref={scrollRef}>
      {header}
      <FundingCard s={s} onOpen={onOpenFunding} />
      <ZoningCard zs={zs} onOpen={onOpenZoning} />
      {s.loading && !s.recs && <div className="spin">Loading recommendations…</div>}
      <AllRecommendations recommendations={s.recs} onSelect={setSelectedRecId} />
    </section>
  );
}
