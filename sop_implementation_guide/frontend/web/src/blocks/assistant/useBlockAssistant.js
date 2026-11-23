import { useCallback, useEffect, useRef, useState } from "react";
import { call } from "../../api.js";

// The Blocks tab assistant's conversations: one per block, and one across all
// blocks. Kept above the side panel (like useBlockFunding) so an answer still
// arrives while the SME looks at another block, and in this browser's storage
// so a reload doesn't lose them. Nothing here is shared with other people.
//
// A turn is {id, role: "user", text, recs: [{rec_id, label}]},
// {id, role: "assistant", answer} or {id, role: "error", text, ask}.

const STORE = "sop.blocks.assistant";
const MAX_TURNS = 30;
const POLL_MS = 900;
const POLL_FAILURES = 5;

export const threadKey = (scope, blockId) => (scope === "all" || blockId == null ? "all" : `block:${blockId}`);

function loadThreads() {
  try {
    const v = JSON.parse(localStorage.getItem(STORE) || "{}");
    return v && typeof v === "object" ? v : {};
  } catch {
    return {};
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let seq = 0;
const turnId = () => `${Date.now()}-${seq++}`;

// What the model gets back of earlier turns: each question, and each answer's headline and body.
function historyOf(turns) {
  return turns.flatMap((t) => {
    if (t.role === "user") return [{ role: "user", text: t.text }];
    if (t.role === "assistant") return [{ role: "assistant", text: [t.answer.headline, t.answer.body].filter(Boolean).join("\n\n") }];
    return [];
  });
}

export default function useBlockAssistant() {
  const [threads, setThreads] = useState(loadThreads);
  const [pending, setPending] = useState({}); // key -> what the assistant is reading now
  const threadsRef = useRef(threads);
  threadsRef.current = threads;
  const pendingRef = useRef(pending);
  pendingRef.current = pending;

  useEffect(() => {
    try {
      localStorage.setItem(STORE, JSON.stringify(threads));
    } catch {
      /* storage full or blocked: the chat still works for this visit */
    }
  }, [threads]);

  const append = useCallback((key, turn) => {
    setThreads((prev) => ({ ...prev, [key]: [...(prev[key] || []), turn].slice(-MAX_TURNS) }));
  }, []);

  const setProgress = useCallback((key, text) => {
    setPending((prev) => {
      const next = { ...prev };
      if (text == null) delete next[key];
      else next[key] = text;
      return next;
    });
  }, []);

  // Ask one question; resolves to the answer (or null on failure).
  const ask = useCallback(async ({ scope, blockId, question, recs = [] }) => {
    const key = threadKey(scope, blockId);
    const text = (question || "").trim();
    if (!text || pendingRef.current[key] != null) return null;
    const history = historyOf(threadsRef.current[key] || []);
    append(key, { id: turnId(), role: "user", text, recs });
    setProgress(key, "Reading the block's data");
    try {
      const { job } = await call("POST", "/assistant/ask", {
        question: text, scope: key === "all" ? "all" : "block", block_id: blockId ?? null,
        rec_ids: recs.map((r) => r.rec_id), history,
      });
      let failures = 0;
      for (;;) {
        await sleep(POLL_MS);
        let r;
        try {
          r = await call("GET", `/assistant/${job}`, undefined, { timeout: 20000 });
          failures = 0;
        } catch (e) {
          if (++failures >= POLL_FAILURES || e.message.startsWith("404")) throw e;
          continue;
        }
        if (r.status === "running") {
          if (r.progress) setProgress(key, r.progress);
          continue;
        }
        if (r.status === "done") {
          append(key, { id: turnId(), role: "assistant", answer: r.result });
          return r.result;
        }
        throw new Error(r.error || "The assistant failed.");
      }
    } catch (e) {
      append(key, { id: turnId(), role: "error", text: e.message, ask: { scope, blockId, question: text, recs } });
      return null;
    } finally {
      setProgress(key, null);
    }
  }, [append, setProgress]);

  // Ask a failed question again, in place of the failed attempt.
  const retry = useCallback((errorTurn) => {
    const { scope, blockId } = errorTurn.ask;
    const key = threadKey(scope, blockId);
    setThreads((prev) => {
      const turns = prev[key] || [];
      const i = turns.findIndex((t) => t.id === errorTurn.id);
      if (i < 0) return prev;
      const from = i > 0 && turns[i - 1].role === "user" ? i - 1 : i;
      return { ...prev, [key]: turns.slice(0, from) };
    });
    // After the state above is applied, so the failed question isn't in the history sent.
    setTimeout(() => ask(errorTurn.ask), 0);
  }, [ask]);

  const clear = useCallback((key) => {
    setThreads((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
  }, []);

  return { threads, pending, ask, retry, clear };
}
