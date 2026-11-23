import { useEffect, useState } from "react";
import { call, API_BASE } from "./api.js";
import BlockList from "./blocks/BlockList.jsx";
import BlockMap from "./blocks/BlockMap.jsx";
import BlockDetail from "./blocks/BlockDetail.jsx";
import FundingWorkspace from "./blocks/FundingWorkspace.jsx";
import useBlockFunding, { fundingStatus } from "./blocks/useBlockFunding.js";
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
  // The funding review takes the map's place in the middle; closing it brings the map back.
  const [fundingOpen, setFundingOpen] = useState(false);

  useEffect(() => {
    setFundingOpen(false);
    if (selectedId != null) funding.load(selectedId);
  }, [selectedId, funding.load]);

  useEffect(() => {
    if (!fundingOpen) return;
    const onKey = (e) => { if (e.key === "Escape") setFundingOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fundingOpen]);

  const selected = blocks.find((b) => b.id === selectedId) || null;
  const fs = funding.get(selectedId);

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
            onOpenFunding={() => setFundingOpen(true)}
            onRefresh={() => funding.refresh(selected.id)}
          />
        </div>
      )}

      {selected && fundingOpen && (
        <div className={"panel workspace" + (panelOpen ? "" : " no-list")}>
          <div className="ws-tabs" role="tablist">
            <button type="button" role="tab" aria-selected="false" onClick={() => setFundingOpen(false)}>Map</button>
            <button type="button" role="tab" aria-selected="true">
              Funding review{" "}
              <span className={"status-chip " + { none: "idle", running: "run", review: "prop", done: "ok" }[fundingStatus(fs).key]}>
                {fundingStatus(fs).text}
              </span>
            </button>
          </div>
          <FundingWorkspace
            block={selected}
            s={fs}
            actions={funding}
            onClose={() => setFundingOpen(false)}
          />
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
        <div className="spacer" />
        <span className="meta" style={{ color: "var(--faint)", fontSize: ".72rem" }}>{API_BASE}</span>
      </header>

      {view === "blocks" && <BlocksView />}
      {view === "pipelines" && <PipelinesView />}
      {view === "pathways" && <PathwaysView />}
    </>
  );
}
