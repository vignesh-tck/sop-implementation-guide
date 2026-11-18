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
    const [plans, signals, profile] = await Promise.all([
      call("GET", `/blocks/${id}/plans`).catch(() => null),
      call("GET", `/blocks/${id}/signals`).catch(() => null),
      call("GET", `/blocks/${id}/profile`).catch(() => null),
    ]);
    patch(id, { recs: plans?.recommendations || [], signals, profile, loading: false });
  }, [patch]);

  // Re-read the block's lists. A run in progress or awaiting approval is kept.
  const load = useCallback((id) => {
    patch(id, { loading: true });
    return refresh(id);
  }, [patch, refresh]);

  const analyze = useCallback(async (id) => {
    patch(id, {
      analyzing: true, analyzeError: null, thread: null, review: null, edits: {},
      approveResult: null, approveError: null, goldProfile: null, goldError: null,
    });
    try {
      const r = await call("POST", `/blocks/${id}/analyze`);
      const review = await call("GET", `/threads/${r.thread_id}/review`);
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
      patch(id, { thread: { id: r.thread_id, approved: false }, review, edits });
    } catch (e) {
      patch(id, { analyzeError: e.message });
    } finally {
      patch(id, { analyzing: false });
    }
  }, [patch]);

  const setEdit = useCallback((id, key, p) => {
    patch(id, (cur) => ({ edits: { ...cur.edits, [key]: { ...cur.edits[key], ...p } } }));
  }, [patch]);

  const setFeedback = useCallback((id, feedback) => patch(id, { feedback }), [patch]);

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

  const computeGold = useCallback(async (id) => {
    const s = ref.current[id];
    if (!s?.thread?.approved) return;
    patch(id, { computingGold: true, goldError: null });
    try {
      const p = await call("POST", `/threads/${s.thread.id}/gold`);
      patch(id, { goldProfile: p, profile: p });
      onProfileComputed?.(id, p);
    } catch (e) {
      patch(id, { goldError: e.message });
    } finally {
      patch(id, { computingGold: false });
    }
  }, [patch, onProfileComputed]);

  const get = useCallback((id) => (id == null ? null : byBlock[id] || EMPTY), [byBlock]);

  return { get, load, refresh, analyze, approve, computeGold, setEdit, setFeedback };
}
