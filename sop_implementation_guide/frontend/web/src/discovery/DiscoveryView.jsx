import { useEffect, useState } from "react";
import { call } from "../api.js";
import StartForm from "./StartForm.jsx";
import StageRouter from "./StageRouter.jsx";

const DISCOVERY_KEY = "sop.discovery.thread";
const STAGE_ORDER = ["load_sources", "clarify", "research", "extract", "geo_tie", "write_silver"];

function DoneView({ thread, state, onNew }) {
  const written = state.written || [];
  return (
    <div className="discovery-wrap">
      <h3>Discovery complete</h3>
      <div className="thread">{thread.id}</div>
      <div className="stage-crumbs">
        {STAGE_ORDER.map((s, i) => (
          <span key={s}>
            <span className="crumb done">{s}</span>
            {i < STAGE_ORDER.length - 1 && <span className="arrow">›</span>}
          </span>
        ))}
      </div>
      <div className="card">
        <div className="banner ok">
          Wrote <b>{written.length}</b> program(s) to <code>funding_programs</code> for goal: <i>{state.goal || thread.goal}</i>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Program</th><th>Geo scope</th><th>Program key</th></tr></thead>
            <tbody>
              {written.length === 0 ? (
                <tr><td colSpan={3} className="assess">Nothing written.</td></tr>
              ) : (
                written.map((w, i) => (
                  <tr key={i}>
                    <td><b>{w.program_name}</b></td>
                    <td className="assess">{w.geo_scope}</td>
                    <td className="assess"><code>{w.program_key}</code></td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        <div className="btn-row">
          <button className="primary" onClick={onNew}>Start another session</button>
        </div>
      </div>
    </div>
  );
}

export default function DiscoveryView() {
  // "start" | "resuming" | "active" | "done"
  const [mode, setMode] = useState("start");
  const [thread, setThread] = useState(null); // { id, goal }
  const [payload, setPayload] = useState(null);
  const [doneState, setDoneState] = useState(null);
  const [resumeBanner, setResumeBanner] = useState(null);

  useEffect(() => {
    const stored = localStorage.getItem(DISCOVERY_KEY);
    if (!stored) return;
    let parsed;
    try {
      parsed = JSON.parse(stored);
    } catch {
      localStorage.removeItem(DISCOVERY_KEY);
      return;
    }
    setThread(parsed);
    setMode("resuming");
    call("GET", `/funding/discovery/${parsed.id}`)
      .then((state) => {
        if (state.done) {
          setDoneState(state);
          setMode("done");
        } else {
          setPayload(state.stage_payload);
          setMode("active");
        }
      })
      .catch((e) => {
        localStorage.removeItem(DISCOVERY_KEY);
        setThread(null);
        setMode("start");
        setResumeBanner(
          <div className="banner warn">
            Could not resume the previous session — <code>{e.message}</code>. The discovery agent's checkpointer
            is in-memory, so a server restart loses paused sessions. Starting fresh.
          </div>
        );
      });
  }, []);

  function persist(t) {
    localStorage.setItem(DISCOVERY_KEY, JSON.stringify(t));
  }

  function handleStarted(state, goal) {
    const t = { id: state.thread_id, goal };
    setThread(t);
    persist(t);
    if (state.done) {
      setDoneState(state);
      setMode("done");
    } else {
      setPayload(state.stage_payload);
      setMode("active");
    }
  }

  function handleSubmitted(state) {
    if (state.done) {
      localStorage.removeItem(DISCOVERY_KEY);
      setDoneState(state);
      setMode("done");
      return;
    }
    setPayload(state.stage_payload);
  }

  function handleAbandon() {
    localStorage.removeItem(DISCOVERY_KEY);
    setThread(null);
    setPayload(null);
    setResumeBanner(null);
    setMode("start");
  }

  function handleNew() {
    setThread(null);
    setPayload(null);
    setDoneState(null);
    setResumeBanner(null);
    setMode("start");
  }

  if (mode === "resuming") {
    return <div className="discovery-wrap"><div className="card spin">Resuming session {thread?.id}…</div></div>;
  }
  if (mode === "done" && doneState) {
    return <DoneView thread={thread} state={doneState} onNew={handleNew} />;
  }
  if (mode === "active" && thread && payload) {
    return (
      <StageRouter
        thread={thread}
        payload={payload}
        onSubmitted={handleSubmitted}
        onAbandon={handleAbandon}
      />
    );
  }
  return <StartForm onStarted={handleStarted} banner={resumeBanner} />;
}
