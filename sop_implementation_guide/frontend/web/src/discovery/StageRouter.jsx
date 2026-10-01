import { useEffect, useState } from "react";
import { call } from "../api.js";
import ClarifyStage from "./stages/ClarifyStage.jsx";
import ResearchStage from "./stages/ResearchStage.jsx";
import ExtractStage from "./stages/ExtractStage.jsx";
import GeoTieStage from "./stages/GeoTieStage.jsx";

const STAGE_ORDER = ["load_sources", "clarify", "research", "extract", "geo_tie", "write_silver"];
const STAGE_LABELS = {
  load_sources: "Load", clarify: "Clarify", research: "Research",
  extract: "Extract", geo_tie: "Geo tie", write_silver: "Write",
};
const STAGE_COMPONENTS = { clarify: ClarifyStage, research: ResearchStage, extract: ExtractStage, geo_tie: GeoTieStage };

function Crumbs({ currentStage }) {
  const idx = STAGE_ORDER.indexOf(currentStage);
  return (
    <div className="stage-crumbs">
      {STAGE_ORDER.map((s, i) => (
        <span key={s}>
          <span className={"crumb" + (i < idx ? " done" : i === idx ? " current" : "")}>{STAGE_LABELS[s]}</span>
          {i < STAGE_ORDER.length - 1 && <span className="arrow">›</span>}
        </span>
      ))}
    </div>
  );
}

export default function StageRouter({ thread, payload, onSubmitted, onAbandon }) {
  const [value, setValue] = useState("approved");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const stage = payload.stage;
  const StageComponent = STAGE_COMPONENTS[stage];

  // A new stage payload means a fresh collector — default to "approved" until the
  // stage component (if any) reports its own value.
  useEffect(() => {
    setValue("approved");
    setError(null);
  }, [stage]);

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      const state = await call("POST", `/funding/discovery/${thread.id}/respond`, { value });
      onSubmitted(state);
    } catch (e) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  }

  function abandon() {
    if (!confirm("Abandon this session? Nothing has been written yet.")) return;
    onAbandon();
  }

  return (
    <div className="discovery-wrap">
      <h3>Funding discovery <span className="meta">· {thread.goal}</span></h3>
      <div className="thread">{thread.id}</div>
      <Crumbs currentStage={stage} />
      <div className="card">
        <div className="sub">{payload.message || ""}</div>
        {error && <div className="banner err">Submit failed — {error}</div>}
        <div>
          {StageComponent ? (
            <StageComponent payload={payload} onValueChange={setValue} />
          ) : (
            <pre style={{ whiteSpace: "pre-wrap", fontSize: ".8rem" }}>{JSON.stringify(payload, null, 2)}</pre>
          )}
        </div>
        <div className="btn-row">
          <button className="primary" onClick={submit} disabled={submitting}>
            {submitting ? "Submitting…" : `Submit ${STAGE_LABELS[stage] || stage}`}
          </button>
          <button onClick={abandon}>Abandon session</button>
        </div>
      </div>
    </div>
  );
}
