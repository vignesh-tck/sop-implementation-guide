import { useEffect, useState } from "react";
import { call, num } from "../api.js";
import { OUTCOME, References, ZoningFinding } from "../zoning/ZoningParts.jsx";
import PlanView, { FundingList } from "./PlanView.jsx";

// One recommendation, stepped into from the block's list. Everything here is
// about this recommendation alone: zoning in particular, since a public garden
// and a hospital get different answers in the same zone. Prev/next walks the
// list in the same order it was shown, so recommendations can be compared
// without going back.

const TABS = [
  ["plan", "Plan"],
  ["zoning", "Zoning"],
  ["funding", "Funding"],
  ["sources", "Sources"],
];

const TILE_CLASS = { by_right: "ok", special_exception: "cond", prohibited: "no" };
const PLAN_STATUS = { template: "Template", draft: "Draft", final: "Final" };

function ZoningTile({ z, onClick }) {
  let value = "Not looked up", sub = "Run the zoning lookup", cls = "none";
  if (z && z.status !== "missing") {
    value = z.best ? OUTCOME[z.best].text : "Not resolved";
    sub = z.zones.map((f) => f.zone).join(", ") || "No zone found";
    cls = TILE_CLASS[z.best] || "none";
  }
  return (
    <button type="button" className={"tile " + cls} onClick={onClick}>
      <span className="tile-k">Zoning</span>
      <span className="tile-v">{value}</span>
      <span className="tile-s">{sub}</span>
    </button>
  );
}

function FundingTile({ funding, onClick }) {
  const n = funding?.length || 0;
  return (
    <button type="button" className={"tile " + (n ? "ok" : "cond")} onClick={onClick}>
      <span className="tile-k">Funding</span>
      <span className="tile-v">{n ? `${n} approved` : "None approved"}</span>
      <span className="tile-s">{n ? funding[0].program_name : "Needs a source"}</span>
    </button>
  );
}

function PlanTile({ plan, onClick }) {
  const cls = plan.status === "final" ? "ok" : plan.status === "draft" ? "edited" : "none";
  return (
    <button type="button" className={"tile " + cls} onClick={onClick}>
      <span className="tile-k">Plan</span>
      <span className="tile-v">{plan.template_version || "—"}</span>
      <span className="tile-s">{PLAN_STATUS[plan.status] || plan.status}{plan.reviewed_by ? ` · ${plan.reviewed_by}` : ""}</span>
    </button>
  );
}

// Every source the plan cites, once, with the steps that use it.
function AllSources({ plan }) {
  const seen = new Map();
  plan.steps.forEach((s, i) => {
    for (const r of s.references || []) {
      const key = `${r.label}|${r.url || ""}`;
      if (!seen.has(key)) seen.set(key, { ...r, steps: [] });
      const e = seen.get(key);
      if (!e.steps.includes(i + 1)) e.steps.push(i + 1);
    }
  });
  const refs = [...seen.values()].map((r) => ({
    ...r, detail: [r.detail, `step${r.steps.length > 1 ? "s" : ""} ${r.steps.join(", ")}`].filter(Boolean).join(" · "),
  }));
  if (!refs.length) return <p className="assess">This plan cites no sources yet.</p>;
  return <References refs={refs} />;
}

export default function RecommendationView({
  blockId, recId, refreshKey, position, total, onBack, onPrev, onNext, onChanged, onOpenFunding,
}) {
  const [plan, setPlan] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [tab, setTab] = useState("plan");

  useEffect(() => {
    setPlan(null);
    setLoadError(null);
    let live = true;
    call("GET", `/blocks/${blockId}/recommendations/${recId}/plan`)
      .then((p) => { if (live) setPlan(p); })
      .catch((e) => { if (live) setLoadError(e.message); });
    return () => { live = false; };
  }, [blockId, recId, refreshKey]);

  // Arrow keys step through recommendations, unless the user is typing or panning the map.
  useEffect(() => {
    function onKey(e) {
      if (e.target.closest?.("input, textarea, select, .leaflet-container")) return;
      if (e.key === "ArrowLeft" && onPrev) onPrev();
      if (e.key === "ArrowRight" && onNext) onNext();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onPrev, onNext]);

  const rec = plan?.rec;
  const down = rec?.direction === "Decrease";

  return (
    <div className="rec-view">
      <div className="rec-nav">
        <button type="button" className="link back" onClick={onBack}>‹ All {total} recommendations</button>
        <span className="spacer" />
        <button type="button" className="nav-btn" onClick={onPrev} disabled={!onPrev} aria-label="Previous recommendation">‹</button>
        <span className="kv">{position} of {total}</span>
        <button type="button" className="nav-btn" onClick={onNext} disabled={!onNext} aria-label="Next recommendation">›</button>
      </div>

      {loadError && <div className="banner err">Could not load this recommendation — {loadError}</div>}
      {!plan && !loadError && <div className="spin" style={{ padding: "1rem 0" }}>Loading…</div>}

      {plan && (
        <>
          <div className="rec-title">
            <span className={"pill " + (down ? "no" : "yes")}>{down ? "reduce" : "increase"}</span>
            <div className="rec-title-text">
              <h4>{rec.rec_label}</h4>
              <div className="assess">{rec.dimension || "—"}</div>
            </div>
            {rec.predicted_score_increase != null && (
              <div className="rec-gain-big">
                <b>{rec.predicted_score_increase >= 0 ? "+" : ""}{num(rec.predicted_score_increase, 1)}</b>
                <span>SoP gain</span>
              </div>
            )}
          </div>

          <div className="tiles">
            <ZoningTile z={plan.zoning} onClick={() => setTab("zoning")} />
            <FundingTile funding={plan.funding} onClick={() => setTab("funding")} />
            <PlanTile plan={plan} onClick={() => setTab("plan")} />
          </div>

          <div className="rec-tabs" role="tablist">
            {TABS.map(([k, label]) => (
              <button key={k} type="button" role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>{label}</button>
            ))}
          </div>

          {tab === "plan" && (
            <PlanView
              plan={plan}
              blockId={blockId}
              recId={recId}
              onSaved={(p) => { setPlan(p); onChanged?.(); }}
              onFunding={onOpenFunding}
            />
          )}

          {tab === "zoning" && (
            plan.zoning ? (
              <div className="tab-body">
                <ZoningFinding z={plan.zoning} hideSummary />
                <div className="zoning-h refs-h">Sources</div>
                <References refs={plan.zoning.references} />
              </div>
            ) : (
              <p className="assess">No zoning finding for this recommendation.</p>
            )
          )}

          {tab === "funding" && (
            <div className="tab-body">
              {plan.funding?.length ? (
                <FundingList funding={plan.funding} withNarrative />
              ) : (
                <p className="assess">
                  No approved funding for this recommendation yet. Funding is analysed for the whole block at
                  once, then approved once for the block.{" "}
                  <button type="button" className="link" onClick={onOpenFunding}>Open the block's funding review</button>
                </p>
              )}
            </div>
          )}

          {tab === "sources" && (
            <div className="tab-body"><AllSources plan={plan} /></div>
          )}
        </>
      )}
    </div>
  );
}
