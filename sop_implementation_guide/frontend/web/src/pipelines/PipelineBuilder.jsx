import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import { call } from "../api.js";
import ReviewPage from "./ReviewPage.jsx";
import { usePageNav } from "./nav.js";

// The node editor library is large; load the Pipeline page only when it opens.
const PipelineWorkspace = lazy(() => import("./PipelineWorkspace.jsx"));

// Two full-width pages: the Pipeline canvas (schema on the Output node, sources
// and steps on the canvas, the agent in a drawer) and Review & publish (the
// agent on the right). Agent turns and runs happen in the background on the
// server; while the pipeline reports `busy` this polls until it clears.
export default function PipelineBuilder({ id, onBack }) {
  const [view, setView] = useState(null);
  const [error, setError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [pending, setPending] = useState(false);
  const [nodePick, setNodePick] = useState(null);       // a node opened from Review & publish
  const timer = useRef(null);
  const nav = usePageNav(view);

  const load = useCallback(async () => {
    try {
      const v = await call("GET", `/pipelines/${id}`);
      setView(v);
      setError(null);
      return v;
    } catch (e) {
      setError(e.message);
      return null;
    }
  }, [id]);

  useEffect(() => { load(); }, [load]);

  // Poll while the server is working.
  useEffect(() => {
    clearTimeout(timer.current);
    if (view?.busy) timer.current = setTimeout(load, 1500);
    return () => clearTimeout(timer.current);
  }, [view, load]);

  // Every panel action returns the fresh pipeline (or {pipeline}); a 202 means "poll".
  // With throwErrors the caller shows the error itself (a dialog, say) instead of the banner.
  const act = useCallback(async (method, path, body, { throwErrors = false } = {}) => {
    setPending(true);
    setActionError(null);
    try {
      const r = await call(method, `/pipelines/${id}${path}`, body);
      if (r && r.definition) setView(r);
      else if (r && r.pipeline) setView(r.pipeline);
      else await load();
      return r;
    } catch (e) {
      await load();
      if (throwErrors) throw e;
      setActionError(e.message);
      return null;
    } finally {
      setPending(false);
    }
  }, [id, load]);

  if (error && !view) {
    return (
      <main className="view"><div className="discovery-wrap">
        <button className="link" onClick={onBack}>← Pipelines</button>
        <div className="banner err">{error}</div>
      </div></main>
    );
  }
  if (!view) return <main className="view"><div className="empty">Loading pipeline…</div></main>;

  if (nodePick && view && nav.page !== 0) setNodePick(null);

  if (nav.page === 1) {
    return (
      <main className="view pl-ws-view">
        <ReviewPage view={view} act={act} pending={pending} nav={nav} onBack={onBack}
                    error={actionError} onClearError={() => setActionError(null)}
                    onOpenNode={(sel) => { setNodePick(sel); nav.go(0); }} />
      </main>
    );
  }
  return (
    <main className="view pl-ws-view">
      <Suspense fallback={<div className="empty">Loading the canvas…</div>}>
        <PipelineWorkspace view={view} act={act} pending={pending} nav={nav} onBack={onBack} initialSel={nodePick}
                           error={actionError} onClearError={() => setActionError(null)} />
      </Suspense>
    </main>
  );
}
