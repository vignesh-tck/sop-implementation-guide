import { useEffect, useState } from "react";
import { call, API_BASE } from "./api.js";
import BlockList from "./blocks/BlockList.jsx";
import BlockMap from "./blocks/BlockMap.jsx";
import BlockDetail from "./blocks/BlockDetail.jsx";
import FundingWorkspace from "./blocks/FundingWorkspace.jsx";
import useBlockFunding, { fundingStatus } from "./blocks/useBlockFunding.js";
import useBlockZoning, { zoningStatus } from "./blocks/useBlockZoning.js";
import ZoningWorkspace from "./blocks/ZoningWorkspace.jsx";
import PathwaysView from "./pathways/PathwaysView.jsx";
import PipelinesView from "./pipelines/PipelinesView.jsx";

function BlocksView() {
  const [blocks, setBlocks] = useState([]);
  const [loadError, setLoadError] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [profiles, setProfiles] = useState({}); // blockId -> gold profile, for map color overlay
  const [panelOpen, setPanelOpen] = useState(true); // list+detail float over an always-visible map

  useEffect(() => {
    call("GET", "/blocks/")
      .then((data) => {
        const sorted = [...data].sort((a, b) => a.id - b.id);
        setBlocks(sorted);
      })
      .catch((e) => setLoadError(e.message));
  }, []);

  const funding = useBlockFunding({
    onProfileComputed: (id, profile) => setProfiles((prev) => ({ ...prev, [id]: profile })),
  });
  // Approving zoning changes the block's list (its zoning dots), which the funding hook loads.
  const zoningReview = useBlockZoning({ onChanged: (id) => funding.refresh(id) });
  // A review takes the map's place in the middle: null (the map), "funding" or "zoning".
  const [workspace, setWorkspace] = useState(null);

  useEffect(() => {
    setWorkspace(null);
    if (selectedId != null) {
      funding.load(selectedId);
      zoningReview.refresh(selectedId);
    }
  }, [selectedId, funding.load, zoningReview.refresh]);

  useEffect(() => {
    if (!workspace) return;
    const onKey = (e) => { if (e.key === "Escape") setWorkspace(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [workspace]);

  const selected = blocks.find((b) => b.id === selectedId) || null;
  const fs = funding.get(selectedId);
  const zs = zoningReview.get(selectedId);
  const chipCls = (key) => ({ none: "idle", running: "run", review: "prop", done: "ok" }[key]);

  if (loadError) {
    return (
      <main className="view">
        <div className="empty">
          <div className="banner err">{loadError}</div>
          Is the API running on <code>{API_BASE}</code>?
        </div>
      </main>
    );
  }

  return (
    <main className="view map-view">
      <div className="map-canvas">
        <BlockMap blocks={blocks} selectedId={selectedId} profiles={profiles} onSelect={setSelectedId} />
      </div>

      {!panelOpen && (
        <button type="button" className="panel-reopen" onClick={() => setPanelOpen(true)}>
          Blocks ({blocks.length})
        </button>
      )}

      {panelOpen && (
        <div className="panel list-panel">
          <div className="panel-head">
            <h2>Blocks ({blocks.length})</h2>
            <button type="button" className="panel-collapse" aria-label="Collapse panel" onClick={() => setPanelOpen(false)}>
              ✕
            </button>
          </div>
          <BlockList blocks={blocks} selectedId={selectedId} onSelect={setSelectedId} />
        </div>
      )}

      {selected && (
        <div className="panel detail-panel">
          <button
            type="button"
            className="detail-close"
            aria-label="Close detail"
            onClick={() => setSelectedId(null)}
          >
            ✕
          </button>
          <BlockDetail
            block={selected}
            s={fs}
            zs={zs}
            onOpenFunding={() => setWorkspace("funding")}
            onOpenZoning={() => setWorkspace("zoning")}
            onRefresh={() => funding.refresh(selected.id)}
          />
        </div>
      )}

      {selected && workspace && (
        <div className={"panel workspace" + (panelOpen ? "" : " no-list")}>
          <div className="ws-tabs" role="tablist">
            <button type="button" role="tab" aria-selected="false" onClick={() => setWorkspace(null)}>Map</button>
            <button type="button" role="tab" aria-selected={workspace === "funding"} onClick={() => setWorkspace("funding")}>
              Funding review{" "}
              <span className={"status-chip " + chipCls(fundingStatus(fs).key)}>{fundingStatus(fs).text}</span>
            </button>
            <button type="button" role="tab" aria-selected={workspace === "zoning"} onClick={() => setWorkspace("zoning")}>
              Zoning review{" "}
              <span className={"status-chip " + chipCls(zoningStatus(zs).key)}>{zoningStatus(zs).text}</span>
            </button>
          </div>
          {workspace === "funding" ? (
            <FundingWorkspace block={selected} s={fs} actions={funding} onClose={() => setWorkspace(null)} />
          ) : (
            <ZoningWorkspace block={selected} s={zs} actions={zoningReview} onClose={() => setWorkspace(null)} />
          )}
        </div>
      )}
    </main>
  );
}

const VIEWS = ["blocks", "pipelines", "pathways"];

// The open tab lives in the URL hash (#pipelines) so a view can be linked to.
function viewFromHash() {
  const v = window.location.hash.slice(1).split("/")[0];
  return VIEWS.includes(v) ? v : "blocks";
}

export default function App() {
  const [view, setViewState] = useState(viewFromHash);

  function setView(v) {
    setViewState(v);
    window.history.replaceState(null, "", `#${v}`);
  }

  return (
    <>
      <header>
        <h1>
          <span className="eyebrow" style={{ display: "block" }}>SoP Implementation Engine</span>
          Funding<span> & </span>Feasibility
        </h1>
        <nav className="tabs">
          <button className="tab" aria-selected={view === "blocks"} onClick={() => setView("blocks")}>
            Blocks
          </button>
          <button className="tab" aria-selected={view === "pipelines"} onClick={() => setView("pipelines")}>
            Pipeline development
          </button>
          <button className="tab" aria-selected={view === "pathways"} onClick={() => setView("pathways")}>
            Playbooks
          </button>
        </nav>
      </header>

      {view === "blocks" && <BlocksView />}
      {view === "pipelines" && <PipelinesView />}
      {view === "pathways" && <PathwaysView />}
    </>
  );
}
