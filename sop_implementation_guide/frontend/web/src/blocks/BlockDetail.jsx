import { useEffect, useRef, useState } from "react";
import { call, money, num } from "../api.js";
import AllRecommendations, { rankRecs } from "./AllRecommendations.jsx";
import AskPanel from "./assistant/AskPanel.jsx";
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

// Feasibility across the block: how many recommendations are assessed, the best score (the block's), and a
// way to assess every recommendation that is ready. Runs go in the background; the list is re-read until done.
function FeasibilityCard({ blockId, recs, onRefresh }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const list = (recs || []).map((r) => ({ ...r, f: r.feasibility || {} }));
  const scored = list.filter((r) => r.f.score != null);
  const best = scored.reduce((b, r) => (b == null || r.f.score > b.f.score ? r : b), null);
  const running = list.filter((r) => r.f.status === "running").length;
  const review = list.filter((r) => r.f.status === "in_review").length;
  const ready = list.filter((r) => r.f.ready && r.f.score == null && !["running", "in_review"].includes(r.f.status)).length;

  useEffect(() => {
    if (!running) return undefined;
    const t = setTimeout(onRefresh, 5000);
    return () => clearTimeout(t);
  }, [running, recs, onRefresh]);

  if (!list.some((r) => r.f.ready || r.f.score != null || r.f.status)) return null;

  async function assessAll() {
    setBusy(true); setError(null);
    try { await call("POST", `/blocks/${blockId}/feasibility`, {}); onRefresh(); }
    catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  const st = running ? { cls: "run", text: `Assessing ${running}` } : review ? { cls: "prop", text: `${review} awaiting review` }
    : scored.length ? { cls: "ok", text: `${scored.length} assessed` } : { cls: "idle", text: "Not assessed" };
  return (
    <div className="funding-card feas-card">
      <span className="fc-label">Feasibility</span>
      <span className={"status-chip " + st.cls}>{st.text}</span>
      {best && <span className="assess" title="The block's feasibility score is its best approved recommendation">
        best {best.f.score} · {best.rec_label}</span>}
      <span className="spacer" />
      {ready > 0 && (
        <button type="button" className="link fc-go" onClick={assessAll} disabled={busy || running > 0}>
          {busy ? "Starting…" : `Assess ${ready} ready`} →
        </button>
      )}
      {error && <div className="banner err" style={{ width: "100%", margin: ".4rem 0 0" }}>{error}</div>}
    </div>
  );
}

// The block page has two tabs: its details, and the assistant (AskPanel), which
// `ask` carries the props for. selectedRecId (null = the block's front page) is
// held a level up so an assistant answer can open a recommendation.
export default function BlockDetail({
  block, s, zs, onOpenFunding, onOpenZoning, onRefresh, onProfile, selectedRecId, setSelectedRecId, tab, onTab, ask, onAskRec,
}) {
  // The panel scrolls as one; each move between list and recommendation starts at the top.
  const scrollRef = useRef(null);
  useEffect(() => { if (scrollRef.current) scrollRef.current.scrollTop = 0; }, [selectedRecId]);

  if (!block) {
    return (
      <section className="detail">
        <div className="empty">Select a block to begin.</div>
      </section>
    );
  }

  const ranked = rankRecs(s.recs);
  const idx = ranked.findIndex((r) => r.rec_id === selectedRecId);
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
      <div className="detail-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={tab !== "ask"} onClick={() => onTab("details")}>Details</button>
        <button type="button" role="tab" aria-selected={tab === "ask"} onClick={() => onTab("ask")}>
          <span className="spark" aria-hidden="true">✧</span> Ask about this block
        </button>
      </div>
    </div>
  );

  if (tab === "ask") {
    return (
      <section className="detail ask-mode">
        {header}
        <AskPanel {...ask} block={block} recs={s.recs} fs={s} zs={zs} onOpenFunding={onOpenFunding} onOpenZoning={onOpenZoning}
                  contextRec={idx >= 0 ? ranked[idx] : null} />
      </section>
    );
  }

  // Stepped into one recommendation: prev/next follow the list's order.
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
          onProfile={onProfile}
        />
      </section>
    );
  }

  return (
    <section className="detail" ref={scrollRef}>
      {header}
      <FundingCard s={s} onOpen={onOpenFunding} />
      <ZoningCard zs={zs} onOpen={onOpenZoning} />
      <FeasibilityCard blockId={block.id} recs={s.recs} onRefresh={onRefresh} />
      {s.loading && !s.recs && <div className="spin">Loading recommendations…</div>}
      <AllRecommendations recommendations={s.recs} onSelect={setSelectedRecId} onAsk={onAskRec} />
    </section>
  );
}
