import { useEffect, useRef, useState } from "react";
import { call, money, num } from "../api.js";
import ReviewTable, { fitKey } from "./ReviewTable.jsx";
import SignalsView from "./SignalsView.jsx";
import ProfileCard from "./ProfileCard.jsx";
import AllRecommendations, { rankRecs } from "./AllRecommendations.jsx";
import RecommendationView from "./RecommendationView.jsx";

// A block in two levels. The front page is block-level only: its facts, its
// recommendations and the block-wide funding analysis. Picking a recommendation
// replaces that page with the recommendation's own view (RecommendationView),
// with a way back, so the list and a plan never compete for the same scroll.

export default function BlockDetail({ block, onProfileComputed }) {
  const [existingProfile, setExistingProfile] = useState(null);
  const [existingSignals, setExistingSignals] = useState(null);
  const [existingLoading, setExistingLoading] = useState(false);
  const [allRecommendations, setAllRecommendations] = useState(null);
  // null = the block's front page; otherwise the recommendation stepped into.
  const [selectedRecId, setSelectedRecId] = useState(null);
  // The panel scrolls as one; each move between list and recommendation starts at the top.
  const scrollRef = useRef(null);
  useEffect(() => { if (scrollRef.current) scrollRef.current.scrollTop = 0; }, [selectedRecId]);

  const [thread, setThread] = useState(null); // { id, approved }
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeError, setAnalyzeError] = useState(null);
  const [review, setReview] = useState(null);
  const [edits, setEdits] = useState({});
  const [feedback, setFeedback] = useState("");

  const [approving, setApproving] = useState(false);
  const [approveResult, setApproveResult] = useState(null);
  const [approveError, setApproveError] = useState(null);

  const [computingGold, setComputingGold] = useState(false);
  const [goldProfile, setGoldProfile] = useState(null);
  const [goldSignals, setGoldSignals] = useState(null);
  const [goldError, setGoldError] = useState(null);

  // Reset per-block flow state and load whatever already exists for this block —
  // mirrors dev-console's selectBlock()/loadExistingState().
  useEffect(() => {
    setThread(null);
    setAnalyzeError(null);
    setReview(null);
    setEdits({});
    setFeedback("");
    setApproveResult(null);
    setApproveError(null);
    setGoldProfile(null);
    setGoldSignals(null);
    setGoldError(null);
    setExistingProfile(null);
    setExistingSignals(null);
    setAllRecommendations(null);
    setSelectedRecId(null);

    if (!block) return;
    const id = block.id;
    setExistingLoading(true);
    Promise.all([
      call("GET", `/blocks/${id}/profile`).catch(() => null),
      call("GET", `/blocks/${id}/signals`).catch(() => null),
      call("GET", `/blocks/${id}/plans`).catch(() => null),
    ]).then(([profile, signals, plans]) => {
      // A block can be reselected while these are in flight — drop a late response
      // rather than render one block's funding under another block's heading.
      if (!block || block.id !== id) return;
      setExistingProfile(profile);
      setExistingSignals(signals);
      setAllRecommendations(plans?.recommendations || null);
      setExistingLoading(false);
    });
  }, [block]);

  // Re-read the list's funding counts and plan statuses after anything changes them.
  function refreshPlans() {
    const id = block.id;
    call("GET", `/blocks/${id}/plans`)
      .then((plans) => { if (block.id === id) setAllRecommendations(plans?.recommendations || null); })
      .catch(() => {});
  }

  if (!block) {
    return (
      <section className="detail">
        <div className="empty">Select a block to begin.</div>
      </section>
    );
  }

  async function analyze() {
    setAnalyzing(true);
    setAnalyzeError(null);
    try {
      const r = await call("POST", `/blocks/${block.id}/analyze`);
      const reviewData = await call("GET", `/threads/${r.thread_id}/review`);
      setThread({ id: r.thread_id, approved: false });
      setReview(reviewData);
      const floor = reviewData.default_floor != null ? reviewData.default_floor : 0.3;
      const initial = {};
      for (const rec of reviewData.recommendations || []) {
        for (const f of rec.fits || []) {
          initial[fitKey(rec.rec_id, f.program_name)] = {
            fit_score: f.fit_score,
            approved: f.fit_score != null && f.fit_score >= floor,
            edited: false,
          };
        }
      }
      setEdits(initial);
    } catch (e) {
      setAnalyzeError(e.message);
    } finally {
      setAnalyzing(false);
    }
  }

  function onEditChange(key, patch) {
    setEdits((prev) => ({ ...prev, [key]: { ...prev[key], ...patch } }));
  }

  // Every row the reviewer sees is reported back, approved true or false, so the
  // backend never has to guess whether an absent row was rejected or just missed.
  function collectFits() {
    const out = [];
    for (const rec of review.recommendations || []) {
      for (const f of rec.fits || []) {
        const e = edits[fitKey(rec.rec_id, f.program_name)];
        const score = e?.fit_score;
        out.push({
          rec_id: rec.rec_id,
          program_name: f.program_name,
          fit_score: score == null || Number.isNaN(score) ? null : Math.max(0, Math.min(1, score)),
          approved: !!e?.approved,
        });
      }
    }
    return out;
  }

  async function approve() {
    if (!thread) return;
    setApproving(true);
    setApproveError(null);
    const fits = collectFits();
    const note = feedback.trim() || "approved";
    try {
      const r = await call("POST", `/threads/${thread.id}/approve`, { fits, feedback: note });
      setThread((t) => ({ ...t, approved: true }));
      setApproveResult({ ...r, kept: fits.filter((f) => f.approved).length, total: fits.length });
      refreshPlans();
    } catch (e) {
      setApproveError(e.message);
    } finally {
      setApproving(false);
    }
  }

  async function computeGold() {
    if (!thread) return;
    setComputingGold(true);
    setGoldError(null);
    try {
      const p = await call("POST", `/threads/${thread.id}/gold`);
      setGoldProfile(p);
      onProfileComputed?.(block.id, p);
      // Read back what was written rather than reusing the review view above it —
      // that still shows every fit assessed, including ones just rejected.
      const signals = await call("GET", `/blocks/${block.id}/signals`).catch(() => null);
      setGoldSignals(signals);
    } catch (e) {
      setGoldError(e.message);
    } finally {
      setComputingGold(false);
    }
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
  const ranked = rankRecs(allRecommendations);
  const idx = ranked.findIndex((r) => r.rec_id === selectedRecId);
  if (selectedRecId != null && idx >= 0) {
    return (
      <section className="detail" ref={scrollRef}>
        {header}
        <RecommendationView
          key={block.id}
          blockId={block.id}
          recId={selectedRecId}
          position={idx + 1}
          total={ranked.length}
          onBack={() => setSelectedRecId(null)}
          onPrev={idx > 0 ? () => setSelectedRecId(ranked[idx - 1].rec_id) : null}
          onNext={idx < ranked.length - 1 ? () => setSelectedRecId(ranked[idx + 1].rec_id) : null}
          onChanged={refreshPlans}
        />
      </section>
    );
  }

  return (
    <section className="detail" ref={scrollRef}>
      {header}

      {existingLoading && !allRecommendations && <div className="spin">Loading recommendations…</div>}
      <AllRecommendations recommendations={allRecommendations} onSelect={setSelectedRecId} />

      <div className="card">
        <h4>Funding analysis — attaches approved funding to each plan</h4>
        <div className="steps">
          <button className="primary" onClick={analyze} disabled={analyzing}>
            {analyzing ? "Analyzing…" : "1 · Analyze"}
          </button>
          <button onClick={approve} disabled={!thread || thread.approved || approving}>
            {approving ? "Approving…" : "2 · Approve"}
          </button>
          <button onClick={computeGold} disabled={!thread || !thread.approved || computingGold}>
            {computingGold ? "Computing…" : "3 · Compute gold"}
          </button>
          {thread && <span className="thread">{thread.id}</span>}
        </div>
      </div>

      {analyzing && <div className="card spin">Running FundingAgent — one LLM pass per recommendation. Takes ~15–45s…</div>}
      {analyzeError && <div className="banner err">Analyze failed — {analyzeError}</div>}

      {!thread && existingLoading && <div className="card spin">Loading existing state…</div>}
      {!thread && existingProfile && <ProfileCard profile={existingProfile} title="Existing profile (from a previous run)" />}
      {!thread && existingSignals && <SignalsView signals={existingSignals} />}

      {review && (
        <ReviewTable review={review} edits={edits} onChange={onEditChange} feedback={feedback} onFeedbackChange={setFeedback} />
      )}
      {approveError && <div className="banner err">Approve failed — {approveError}</div>}
      {approveResult && (
        <div className="banner ok">
          Kept {approveResult.kept} of {approveResult.total} fits — wrote {approveResult.signals_written} row(s) to{" "}
          <code>funding_signals</code>, ids {JSON.stringify(approveResult.written_signal_ids)}.
          {approveResult.signals_removed
            ? ` Removed ${approveResult.signals_removed} previously kept row(s), ids ${JSON.stringify(approveResult.removed_signal_ids)}.`
            : ""}
        </div>
      )}
      {goldError && <div className="banner err">Gold failed — {goldError}</div>}
      {goldProfile && <ProfileCard profile={goldProfile} title="Gold layer — block_implementation_profile" />}
      {goldSignals && <SignalsView signals={goldSignals} />}
    </section>
  );
}
