import { useCallback, useRef, useState } from "react";
import { call } from "../api.js";

// Each block's Block Zoning runs and the one being reviewed, kept per block id
// above the side panel and the zoning review (like useBlockFunding).
//
// A run is made against one Zoning version and stores one finding per
// recommendation. Findings with nothing to ask come pre-accepted; the rest wait
// for the SME. Approving completes the run and makes it the block's current
// zoning, which is all plans, the block's list and gold read.

export const REVIEWER_KEY = "sop.pathways.reviewer"; // shared with plans and the Playbooks tab

const EMPTY = {
  analyses: null, versions: null, nextVersion: null, runName: "",
  // The run shown in the review: the one awaiting review, else the current one, else one being viewed.
  run: null, runLoading: false,
  // key -> {action: accept | set | open, outcome, note}: the SME's decisions on the run awaiting review.
  edits: {}, note: "",
  running: false, runError: null, approving: false, approveError: null, approved: null, historyError: null,
  // Bumped whenever the block's approved zoning changes, so open plans re-read it.
  version: 0,
};

// The decision a finding stands at: the SME's edit, else what the run holds.
export function decisionOf(s, item) {
  return s.edits[item.key] || item.decision || null;
}

// What a decision still needs before the run can be approved, or null.
export function decisionProblem(item, d) {
  if (!d) return null;
  if (d.action === "set" && !d.outcome) return "pick an outcome";
  if (d.action === "set" && d.outcome !== item.proposed && !(d.note || "").trim()) return "say why";
  return null;
}

// Where the block's zoning stands, as one word for the chip.
export function zoningStatus(s) {
  if (!s?.analyses) return { key: "none", text: "Not run" };
  if (s.running) return { key: "running", text: "Running…" };
  const live = s.analyses.filter((a) => a.status !== "discarded");
  if (live.some((a) => a.status === "in_review")) return { key: "review", text: "Awaiting approval" };
  const cur = live.find((a) => a.pinned);
  if (cur && !cur.result) return { key: "none", text: "Needs a re-run" };
  if (cur) {
    const open = cur.result.open || 0;
    return { key: "done", text: open ? `Approved · ${open} open` : "Approved" };
  }
  return { key: "none", text: "Not run" };
}

export default function useBlockZoning({ onChanged } = {}) {
  const [byBlock, setByBlock] = useState({});
  const ref = useRef(byBlock);
  ref.current = byBlock;

  const patch = useCallback((id, p) => {
    setByBlock((prev) => {
      const cur = prev[id] || EMPTY;
      return { ...prev, [id]: { ...cur, ...(typeof p === "function" ? p(cur) : p) } };
    });
  }, []);

  const refresh = useCallback(async (id) => {
    const [analyses, versions] = await Promise.all([
      call("GET", `/blocks/${id}/analyses?kind=zoning`).catch(() => null),
      call("GET", "/datasets/zoning/versions").catch(() => null),
    ]);
    patch(id, { analyses: analyses?.analyses || [], versions: versions?.versions || [] });
    return analyses?.analyses || [];
  }, [patch]);

  // Load a run in full (its findings) into the review.
  const show = useCallback(async (id, analysisId) => {
    patch(id, { runLoading: true, historyError: null });
    try {
      const run = await call("GET", `/analyses/${analysisId}`);
      patch(id, (cur) => ({ run, edits: cur.run?.id === run.id ? cur.edits : {}, runLoading: false }));
    } catch (e) {
      patch(id, { historyError: `Could not open run #${analysisId}: ${e.message}`, runLoading: false });
    }
  }, [patch]);

  // The run the review opens on: one awaiting review, else the current one.
  const open = useCallback(async (id) => {
    const runs = await refresh(id);
    const live = runs.filter((a) => a.status !== "discarded");
    const target = live.find((a) => a.status === "in_review") || live.find((a) => a.pinned);
    if (target) await show(id, target.id);
    else patch(id, { run: null, edits: {} });
  }, [refresh, show, patch]);

  const setRunOptions = useCallback((id, p) => patch(id, p), [patch]);

  // Run Block Zoning against the chosen Zoning version (latest when none is chosen).
  const runReview = useCallback(async (id) => {
    const s = ref.current[id] || EMPTY;
    patch(id, { running: true, runError: null, approved: null, approveError: null });
    try {
      let by = "";
      try { by = localStorage.getItem(REVIEWER_KEY) || ""; } catch { /* per-viewer convenience only */ }
      const r = await call("POST", "/analyses", {
        kind: "zoning", block_ids: [id], version: s.nextVersion, name: s.runName.trim() || null, created_by: by || null,
      });
      patch(id, { runName: "", edits: {}, note: "" });
      await refresh(id);
      const a = r.analyses?.[0];
      if (a) await show(id, a.analysis_id);
    } catch (e) {
      patch(id, { runError: e.message });
    } finally {
      patch(id, { running: false });
    }
  }, [patch, refresh, show]);

  const setDecision = useCallback((id, key, p) => {
    patch(id, (cur) => {
      const item = (cur.run?.result?.findings || []).find((i) => i.key === key);
      const base = cur.edits[key] || (item?.decision ? { action: item.decision.action, outcome: item.decision.outcome, note: "" } : {});
      return { edits: { ...cur.edits, [key]: { ...base, ...p } } };
    });
  }, [patch]);

  // Accept every flagged finding that has a proposed outcome and no decision yet.
  const acceptAllProposed = useCallback((id) => {
    patch(id, (cur) => {
      const edits = { ...cur.edits };
      for (const i of cur.run?.result?.findings || []) {
        if (i.flagged && i.proposed && !edits[i.key]) edits[i.key] = { action: "accept", outcome: i.proposed, note: "" };
      }
      return { edits };
    });
  }, [patch]);

  const setNote = useCallback((id, note) => patch(id, { note }), [patch]);

  const approve = useCallback(async (id, approvedBy) => {
    const s = ref.current[id];
    if (!s?.run) return;
    const decisions = Object.entries(s.edits).map(([key, d]) => ({
      key, action: d.action, outcome: d.action === "set" ? d.outcome : null, note: (d.note || "").trim() || null,
    }));
    patch(id, { approving: true, approveError: null });
    try {
      const r = await call("POST", `/analyses/${s.run.id}/review`, {
        decisions, approved_by: approvedBy, note: s.note.trim() || null,
      });
      patch(id, (cur) => ({ approved: r, edits: {}, note: "", version: cur.version + 1 }));
      await refresh(id);
      await show(id, s.run.id);
      onChanged?.(id);
    } catch (e) {
      patch(id, { approveError: e.message });
    } finally {
      patch(id, { approving: false });
    }
  }, [patch, refresh, show, onChanged]);

  const discard = useCallback(async (id, analysisId) => {
    try {
      await call("POST", `/analyses/${analysisId}/discard`);
      patch(id, (cur) => (cur.run?.id === analysisId ? { run: null, edits: {} } : {}));
      await open(id);
    } catch (e) {
      patch(id, { historyError: e.message });
    }
  }, [patch, open]);

  // Make an earlier approved run the block's current zoning.
  const pin = useCallback(async (id, analysisId) => {
    try {
      await call("POST", `/analyses/${analysisId}/pin`);
      patch(id, (cur) => ({ version: cur.version + 1, approved: null }));
      await refresh(id);
      await show(id, analysisId);
      onChanged?.(id);
    } catch (e) {
      patch(id, { historyError: e.message });
    }
  }, [patch, refresh, show, onChanged]);

  const get = useCallback((id) => (id == null ? null : byBlock[id] || EMPTY), [byBlock]);

  return { get, refresh, open, show, runReview, setRunOptions, setDecision, acceptAllProposed, setNote,
           approve, discard, pin };
}
