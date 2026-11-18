import { useCallback, useEffect, useRef, useState } from "react";
import { call } from "../api.js";
import ChatPane from "./ChatPane.jsx";
import PipelinePanel from "./PipelinePanel.jsx";

// Chat on the left, the pipeline it builds on the right. Agent turns and runs
// happen in the background on the server; while the pipeline reports `busy`
// this polls until it clears.
export default function PipelineBuilder({ id, onBack }) {
  const [view, setView] = useState(null);
  const [error, setError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [pending, setPending] = useState(false);
  const timer = useRef(null);

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

  return (
    <main className="view pl-view">
      <section className="pl-chat">
        <ChatPane view={view} act={act} onBack={onBack} />
      </section>
      <section className="pl-panel">
        {actionError && <div className="banner err">{actionError}</div>}
        <PipelinePanel view={view} act={act} pending={pending} />
      </section>
    </main>
  );
}
