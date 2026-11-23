import { useCallback, useRef, useState } from "react";
import { call } from "../api.js";

// Everything the Blocks view knows about each block's recommendations and
// funding, kept per block id above both the side panel and the funding review.
// Living here, not in either component, is what lets an analysis keep running
// while the user goes back to the map, opens a recommendation or another block,
// and still be there when they come back.

export const fitKey = (recId, programName) => `${recId}::${programName}`;

// The program's award as one figure, when it publishes one (floor equal to
// ceiling, or only one of them). A range is not an amount: the reviewer names
// the figure. Mirrors published_amount in backend/agents/funding.py.
export function publishedAmount(f) {
  const lo = f.award_amount_min, hi = f.award_amount_max;
  if (lo != null && hi != null) return Math.abs(hi - lo) < 0.005 ? Number(hi) : null;
  const v = hi ?? lo;
  return v != null ? Number(v) : null;
}

export const hasAmount = (v) => v != null && !Number.isNaN(v) && v > 0;

const EMPTY = {
  recs: null, signals: null, profile: null, loading: false,
  thread: null, review: null, edits: {}, feedback: "",
  analyzing: false, analyzeError: null,
  approving: false, approveError: null, approveResult: null,
  computingGold: false, goldProfile: null, goldError: null,
  // Bumped whenever approved funding changes, so open plans re-read it.
  version: 0,
  // Versions: the block's funding runs, the Funding dataset versions, what the
  // next run is against (null = latest) and its optional name, and an earlier
  // run being looked at read-only (null = the current, pinned one).
  analyses: null, versions: null, nextVersion: null, runName: "",
  viewing: null, viewSignals: null, historyError: null,
};

// Where the block's funding stands, as one word for the chip.
export function fundingStatus(s) {
  if (!s) return { key: "none", text: "Not run" };
  if (s.analyzing) return { key: "running", text: "Analyzing…" };
  if (s.thread && !s.thread.approved) return { key: "review", text: "Awaiting approval" };
  if (s.goldProfile) return { key: "done", text: "Gold computed" };
  if (s.thread?.approved) return { key: "done", text: "Approved" };
  const n = s.signals?.recommendations?.length || 0;
  if (n) return { key: "done", text: `${n} funded` };
  return { key: "none", text: "Not run" };
}

export default function useBlockFunding({ onProfileComputed } = {}) {
  const [byBlock, setByBlock] = useState({});
  // The latest state, for async steps that need to read it after an await.
  const ref = useRef(byBlock);
  ref.current = byBlock;

  const patch = useCallback((id, p) => {
    setByBlock((prev) => {
      const cur = prev[id] || EMPTY;
      return { ...prev, [id]: { ...cur, ...(typeof p === "function" ? p(cur) : p) } };
    });
  }, []);

  const refresh = useCallback(async (id) => {
    const [plans, signals, profile, analyses, versions] = await Promise.all([
      call("GET", `/blocks/${id}/plans`).catch(() => null),
      call("GET", `/blocks/${id}/signals`).catch(() => null),
      call("GET", `/blocks/${id}/profile`).catch(() => null),
      call("GET", `/blocks/${id}/analyses?kind=funding`).catch(() => null),
      call("GET", "/datasets/funding/versions").catch(() => null),
    ]);
    patch(id, {
      recs: plans?.recommendations || [], signals, profile, loading: false,
      analyses: analyses?.analyses || [], versions: versions?.versions || [],
    });
  }, [patch]);

  // Re-read the block's lists. A run in progress or awaiting approval is kept.
  const load = useCallback((id) => {
    patch(id, { loading: true });
    return refresh(id);
  }, [patch, refresh]);

  // Load a thread's proposal into the review, pre-ticking fits above the floor.
  const openReview = useCallback(async (id, threadId, run) => {
    const review = await call("GET", `/threads/${threadId}/review`);
    const floor = review.default_floor != null ? review.default_floor : 0.3;
    const edits = {};
    for (const rec of review.recommendations || []) {
      for (const f of rec.fits || []) {
        edits[fitKey(rec.rec_id, f.program_name)] = {
          fit_score: f.fit_score,
          approved: f.fit_score != null && f.fit_score >= floor,
          edited: false,
          amount: publishedAmount(f),
        };
      }
    }
    patch(id, { thread: { id: threadId, approved: false, ...run }, review, edits, viewing: null, viewSignals: null });
  }, [patch]);

  // Start a run against the chosen Funding version (latest when none is chosen).
  const analyze = useCallback(async (id) => {
    const s = ref.current[id] || EMPTY;
    const version = s.nextVersion ?? s.versions?.[0]?.version ?? null;
    const name = s.runName.trim() || null;
    patch(id, {
      analyzing: true, analyzeError: null, thread: null, review: null, edits: {},
      approveResult: null, approveError: null, goldProfile: null, goldError: null,
      viewing: null, viewSignals: null,
    });
    try {
      const r = await call("POST", `/blocks/${id}/analyze`, { version: s.nextVersion, name });
      await openReview(id, r.thread_id, { analysis_id: r.analysis_id, dataset_version: version, name });
      patch(id, { runName: "" });
    } catch (e) {
      patch(id, { analyzeError: e.message });
    } finally {
      patch(id, { analyzing: false });
      refresh(id);
    }
  }, [patch, openReview, refresh]);

  // Reopen a run whose review is still held by the server (e.g. after a page reload).
  const resume = useCallback(async (id, a) => {
    patch(id, { analyzeError: null, approveResult: null, approveError: null, goldProfile: null, goldError: null });
    try {
      await openReview(id, a.thread_id, { analysis_id: a.id, dataset_version: a.dataset_version, name: a.name });
    } catch (e) {
      patch(id, { analyzeError: `Could not reopen run #${a.id}: ${e.message}` });
      refresh(id);
    }
  }, [patch, openReview, refresh]);

  const setRunOptions = useCallback((id, p) => patch(id, p), [patch]);

  // Look at an earlier run read-only; null goes back to the current one.
  const view = useCallback(async (id, analysisId) => {
    if (analysisId == null) {
      patch(id, { viewing: null, viewSignals: null, historyError: null });
      return;
    }
    try {
      const sig = await call("GET", `/blocks/${id}/signals?analysis_id=${analysisId}`);
      patch(id, { viewing: analysisId, viewSignals: sig, historyError: null, thread: null, review: null });
    } catch (e) {
      patch(id, { historyError: e.message });
    }
  }, [patch]);

  // Make an earlier run the block's current funding: plans and gold read it from now on.
  const pin = useCallback(async (id, analysisId) => {
    try {
      await call("POST", `/analyses/${analysisId}/pin`);
      patch(id, (cur) => ({ viewing: null, viewSignals: null, historyError: null, thread: null, review: null,
                            goldProfile: null, approveResult: null, version: cur.version + 1 }));
      await refresh(id);
    } catch (e) {
      patch(id, { historyError: e.message });
    }
  }, [patch, refresh]);

  const discard = useCallback(async (id, analysisId) => {
    try {
      await call("POST", `/analyses/${analysisId}/discard`);
      patch(id, (cur) => (cur.thread?.analysis_id === analysisId ? { thread: null, review: null } : {}));
      await refresh(id);
    } catch (e) {
      patch(id, { historyError: e.message });
    }
  }, [patch, refresh]);

  const setEdit = useCallback((id, key, p) => {
    patch(id, (cur) => ({ edits: { ...cur.edits, [key]: { ...cur.edits[key], ...p } } }));
  }, [patch]);

  const setFeedback = useCallback((id, feedback) => patch(id, { feedback }), [patch]);

  // A source the reviewer knows of that the run did not propose. It joins the
  // recommendation's matches, kept, and is sent with its details on approve.
  // Held only in this open review until then.
  const addSource = useCallback((id, recId, src) => {
    patch(id, (cur) => {
      if (!cur.review) return {};
      const recommendations = cur.review.recommendations.map((r) =>
        r.rec_id === recId ? { ...r, fits: [...(r.fits || []), { ...src, added_by_reviewer: true }] } : r);
      return {
        review: { ...cur.review, recommendations },
        edits: { ...cur.edits, [fitKey(recId, src.program_name)]: {
          fit_score: src.fit_score, approved: true, edited: false, amount: src.amount } },
      };
    });
  }, [patch]);

  const removeSource = useCallback((id, recId, programName) => {
    patch(id, (cur) => {
      if (!cur.review) return {};
      const recommendations = cur.review.recommendations.map((r) =>
        r.rec_id === recId ? { ...r, fits: r.fits.filter((f) => !(f.added_by_reviewer && f.program_name === programName)) } : r);
      const edits = { ...cur.edits };
      delete edits[fitKey(recId, programName)];
      return { review: { ...cur.review, recommendations }, edits };
    });
  }, [patch]);

  // Every row the reviewer saw is reported back, kept or not, so the backend
  // never has to guess whether an absent row was rejected or just missed.
  const approve = useCallback(async (id) => {
    const s = ref.current[id];
    if (!s?.thread || !s.review) return;
    const fits = [];
    for (const rec of s.review.recommendations || []) {
      for (const f of rec.fits || []) {
        const e = s.edits[fitKey(rec.rec_id, f.program_name)];
        const score = e?.fit_score;
        fits.push({
          rec_id: rec.rec_id,
          program_name: f.program_name,
          fit_score: score == null || Number.isNaN(score) ? null : Math.max(0, Math.min(1, score)),
          approved: !!e?.approved,
          amount: hasAmount(e?.amount) ? e.amount : null,
          ...(f.added_by_reviewer ? {
            added: true,
            program_type: f.program_type, source_agency: f.source_agency, deadline: f.deadline,
            application_url: f.application_url, source_url: f.source_url, source_name: f.source_name,
            narrative: f.narrative,
          } : {}),
        });
      }
    }
    // The button is disabled in this case; this is the backstop. The API refuses it too.
    const missing = fits.filter((f) => f.approved && f.amount == null).length;
    if (missing) {
      patch(id, { approveError: `${missing} kept match(es) still need an amount.` });
      return;
    }
    patch(id, { approving: true, approveError: null });
    try {
      const r = await call("POST", `/threads/${s.thread.id}/approve`, { fits, feedback: s.feedback.trim() || "approved" });
      patch(id, (cur) => ({
        thread: { ...cur.thread, approved: true },
        approveResult: { ...r, kept: fits.filter((f) => f.approved).length, total: fits.length },
        version: cur.version + 1,
      }));
      await refresh(id);
    } catch (e) {
      patch(id, { approveError: e.message });
    } finally {
      patch(id, { approving: false });
    }
  }, [patch, refresh]);

  // From the run just approved, or (no live run) from whichever run is current.
  const computeGold = useCallback(async (id) => {
    const s = ref.current[id];
    if (!s) return;
    patch(id, { computingGold: true, goldError: null });
    try {
      const p = s.thread?.approved
        ? await call("POST", `/threads/${s.thread.id}/gold`)
        : await call("POST", `/blocks/${id}/gold`);
      patch(id, { goldProfile: p, profile: p });
      onProfileComputed?.(id, p);
    } catch (e) {
      patch(id, { goldError: e.message });
    } finally {
      patch(id, { computingGold: false });
    }
  }, [patch, onProfileComputed]);

  const get = useCallback((id) => (id == null ? null : byBlock[id] || EMPTY), [byBlock]);

  return { get, load, refresh, analyze, approve, computeGold, setEdit, setFeedback, addSource, removeSource,
           resume, view, pin, discard, setRunOptions };
}
