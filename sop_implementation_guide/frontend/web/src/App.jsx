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
    <main className="view">
      <div className="blocks-top">
        <BlockList blocks={blocks} selectedId={selectedId} onSelect={setSelectedId} />
        <BlockMap blocks={blocks} selectedId={selectedId} profiles={profiles} onSelect={setSelectedId} />
      </div>
      <BlockDetail
        block={selected}
        onProfileComputed={(id, profile) => setProfiles((prev) => ({ ...prev, [id]: profile }))}
      />
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
