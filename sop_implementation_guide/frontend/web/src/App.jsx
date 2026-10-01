import { useEffect, useState } from "react";
import { call, API_BASE } from "./api.js";
import BlockList from "./blocks/BlockList.jsx";
import BlockMap from "./blocks/BlockMap.jsx";
import BlockDetail from "./blocks/BlockDetail.jsx";
import DiscoveryView from "./discovery/DiscoveryView.jsx";

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

  const selected = blocks.find((b) => b.id === selectedId) || null;

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
            onProfileComputed={(id, profile) => setProfiles((prev) => ({ ...prev, [id]: profile }))}
          />
        </div>
      )}
    </main>
  );
}

export default function App() {
  const [view, setView] = useState("blocks");

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
          <button className="tab" aria-selected={view === "discovery"} onClick={() => setView("discovery")}>
            Funding discovery
          </button>
        </nav>
        <div className="spacer" />
        <span className="meta" style={{ color: "var(--faint)", fontSize: ".72rem" }}>{API_BASE}</span>
      </header>

      {view === "blocks" ? <BlocksView /> : <DiscoveryView />}
    </>
  );
}
