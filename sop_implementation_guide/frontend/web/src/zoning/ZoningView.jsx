import { useEffect, useState } from "react";
import { call, num } from "../api.js";
import RefreshPanel from "./RefreshPanel.jsx";
import { OUTCOME, Outcome, References, ZoningFinding } from "./ZoningParts.jsx";

// The data behind every zoning finding, in the open: which zones each block
// fronts and why, what the ordinance allows for each recommendation type in each
// zone, and every source with its date. Nothing here is computed in the browser;
// it is what GET /zoning/overview and GET /blocks/{id}/zoning return.

const STATUS = {
  attached: { text: "resolved", cls: "ok" },
  partial: { text: "resolved · SME to confirm", cls: "derived" },
  needs_sme: { text: "needs SME", cls: "derived" },
  missing: { text: "not looked up", cls: "gone" },
};

const TABS = [["blocks", "Blocks"], ["table", "Use table"], ["sources", "Sources and method"]];

const date = (iso) => (iso ? iso.slice(0, 10) : "—");

function ZoneChips({ zones, legacy }) {
  return zones.map((z) => (
    <span key={z} className={"zone-chip" + (legacy.has(z) ? " legacy" : "")}>{z}</span>
  ));
}

// ---------------------------------------------------------------- Blocks

function BlockFacts({ data, block, zoneNames }) {
  const lookup = data.lookup;
  if (!lookup) {
    return (
      <div className="banner warn">
        Block {block.id} has not been looked up. Run <code>python -m scripts.sync_block_zoning --block {block.id}</code>.
      </div>
    );
  }
  return (
    <div className="card">
      <h4>What the county map says</h4>
      <table className="facts">
        <tbody>
          <tr>
            <th>Zones within {lookup.buffer_m} m</th>
            <td>
              {lookup.zones.length ? lookup.zones.map((z) => (
                <div key={z}><b>{z}</b> {zoneNames[z] ? `— ${zoneNames[z]}` : ""}</div>
              )) : "No zoning polygon found"}
            </td>
          </tr>
          <tr>
            <th>Prior zones (before Apr 2022)</th>
            <td>{lookup.prior_zones?.length ? lookup.prior_zones.join(", ") : "—"}</td>
          </tr>
          <tr>
            <th>Overlays</th>
            <td>
              {lookup.overlays?.length
                ? lookup.overlays.map((o) => <div key={o.layer}>{o.layer} {o.values?.filter((v) => v !== "present").join(", ")}</div>)
                : <span className="assess">None found (checked: {(lookup.overlays_checked || []).join(", ")})</span>}
            </td>
          </tr>
          <tr>
            <th>Approved plans on file</th>
            <td>
              {Object.keys(lookup.cases || {}).length ? Object.entries(lookup.cases).map(([layer, nums]) => (
                <div key={layer} className="cases"><b>{layer}</b> ({nums.length}): {nums.join(", ")}</div>
              )) : <span className="assess">None within {lookup.buffer_m} m</span>}
            </td>
          </tr>
          <tr>
            <th>Looked up</th>
            <td>
              {date(lookup.fetched_at)} from{" "}
              <a href={lookup.layer_url} target="_blank" rel="noopener noreferrer">county zoning layer ↗</a>
            </td>
          </tr>
          <tr>
            <th>Zoning score</th>
            <td>
              {data.zoning_score == null ? "Not scorable (placeholder 50 in the gold layer)" : <b>{num(data.zoning_score, 0)}/100</b>}
              <span className="assess"> · {data.score_detail?.scored ?? 0} recommendation(s) scored, {data.score_detail?.unscorable ?? 0} unresolved</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

function BlockFindings({ findings }) {
  const [open, setOpen] = useState(null);
  // One row per recommendation type and direction; the same type can appear
  // under several SoP dimensions, and its zoning answer is the same.
  const seen = new Map();
  for (const f of findings) {
    const key = `${f.rec_label}|${f.direction}`;
    if (seen.has(key)) seen.get(key).count += 1;
    else seen.set(key, { ...f, key, count: 1 });
  }
  const rows = [...seen.values()];
  if (!rows.length) return <div className="card assess">No land-use or parks recommendations on this block.</div>;
  return (
    <div className="card">
      <h4>Findings per recommendation ({rows.length})</h4>
      <ul className="zfindings">
        {rows.map((f) => {
          const st = STATUS[f.status] || STATUS.missing;
          const isOpen = open === f.key;
          return (
            <li key={f.key}>
              <button type="button" className="zfinding-head" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : f.key)}>
                <span className="caret">{isOpen ? "▾" : "▸"}</span>
                <span className={"pill " + (f.direction === "Decrease" ? "no" : "yes")}>{f.direction === "Decrease" ? "reduce" : "increase"}</span>
                <span className="zf-label">{f.rec_label}{f.count > 1 && <span className="assess"> ×{f.count}</span>}</span>
                <span className="assess zf-summary">{f.summary}</span>
                <span className={"tag " + st.cls}>{st.text}</span>
              </button>
              {isOpen && (
                <div className="zfinding-body">
                  <ZoningFinding z={f} />
                  <div className="zoning-h" style={{ marginTop: ".6rem" }}>References</div>
                  <References refs={f.references} />
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function BlocksPanel({ ov }) {
  const [zoneFilter, setZoneFilter] = useState("");
  const [selectedId, setSelectedId] = useState(ov.blocks[0]?.id ?? null);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const legacy = new Set(ov.zones.filter((z) => z.legacy).map((z) => z.code));
  const zoneNames = Object.fromEntries(ov.zones.map((z) => [z.code, z.name]));

  useEffect(() => {
    if (selectedId == null) return undefined;
    let live = true;
    setDetail(null);
    setError(null);
    call("GET", `/blocks/${selectedId}/zoning`)
      .then((d) => { if (live) setDetail(d); })
      .catch((e) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [selectedId]);

  const shown = ov.blocks.filter((b) => !zoneFilter || b.zones.includes(zoneFilter));
  const selected = ov.blocks.find((b) => b.id === selectedId);

  return (
    <div className="zoning-grid">
      <div className="card zoning-list">
        <label className="zone-filter">
          Zone
          <select value={zoneFilter} onChange={(e) => setZoneFilter(e.target.value)}>
            <option value="">All zones ({ov.blocks.length} blocks)</option>
            {ov.zones.map((z) => (
              <option key={z.code} value={z.code}>{z.code} — {z.name} ({z.blocks.length})</option>
            ))}
          </select>
        </label>
        <ul>
          {shown.map((b) => {
            const sme = (b.statuses.partial || 0) + (b.statuses.needs_sme || 0);
            return (
              <li key={b.id} aria-selected={b.id === selectedId} onClick={() => setSelectedId(b.id)}>
                <div className="zl-top">
                  <b>Block {b.id}</b> <span className="assess">{b.street_name}</span>
                  <span className="spacer" />
                  <span className="zl-score" title="Zoning sub-score">{b.zoning_score == null ? "—" : num(b.zoning_score, 0)}</span>
                </div>
                <div className="zl-meta">
                  <ZoneChips zones={b.zones} legacy={legacy} />
                  {b.overlays.length > 0 && <span className="zone-chip overlay" title={b.overlays.map((o) => o.layer).join(", ")}>overlay</span>}
                  {sme > 0 && <span className="assess"> · {sme} to confirm</span>}
                </div>
              </li>
            );
          })}
        </ul>
      </div>
      <div className="zoning-detail">
        {selected && <h3 className="zd-title">Block {selected.id} · {selected.street_name}</h3>}
        {error && <div className="banner err">Could not load block zoning — {error}</div>}
        {!detail && !error && <div className="card spin">Loading…</div>}
        {detail && selected && (
          <>
            <BlockFacts data={detail} block={selected} zoneNames={zoneNames} />
            <BlockFindings findings={detail.findings} />
          </>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Use table

function CellDetail({ row, zone, zoneName, ov }) {
  const cell = row.cells[zone];
  return (
    <div className="card cell-detail">
      <h4>{row.rec_label} in {zone}{zoneName ? ` (${zoneName})` : ""}</h4>
      <p><Outcome outcome={cell.outcome} /> <b>{(OUTCOME[cell.outcome] || OUTCOME.unknown).text}</b>
        {row.mode === "all" ? " — every part must be allowed" : row.uses.length > 1 ? " — the best of the uses below" : ""}</p>
      {cell.outcome === "legacy" ? (
        <p className="assess">
          {ov.legacy_rule.text}{" "}
          <a href={ov.legacy_rule.ref.url} target="_blank" rel="noopener noreferrer">{ov.legacy_rule.ref.label} ↗</a>
        </p>
      ) : (
        <table className="facts">
          <thead><tr><th>Ordinance use (Table 27-5101 row)</th><th>Value</th><th>Conditions</th></tr></thead>
          <tbody>
            {cell.uses.map((u) => (
              <tr key={u.row}>
                <td>{u.row}<div className="assess">read as: {u.label}{u.table ? ` · Table ${u.table}` : ""}</div></td>
                <td>{u.value ? <Outcome outcome={u.outcome} /> : <span className="assess">not listed</span>}</td>
                <td>
                  {u.standards?.length ? u.standards.map((s) => (
                    <div key={s.url}><a href={s.url} target="_blank" rel="noopener noreferrer">§{s.text} ↗</a></div>
                  )) : <span className="assess">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {row.note && <p className="assess">Our reading: {row.note}</p>}
      <p className="assess">
        Source: <a href={ov.use_table.url} target="_blank" rel="noopener noreferrer">Zoning Ordinance §27-5101 ↗</a>, current through {ov.use_table.amended_through}.
      </p>
    </div>
  );
}

function UseTablePanel({ ov }) {
  const [pick, setPick] = useState(null);
  const zones = ov.matrix.zones;
  const zoneInfo = Object.fromEntries(ov.zones.map((z) => [z.code, z]));
  const rows = [...ov.matrix.rows].sort((a, b) => b.rec_count - a.rec_count);
  const pickedRow = pick && rows.find((r) => r.rec_label === pick.rec);

  return (
    <>
      <div className="card">
        <h4>What the ordinance allows, by recommendation type and zone</h4>
        <p className="assess" style={{ marginTop: 0 }}>
          Rows are the SoP recommendation types that have a zoning step, with how many times each appears on our blocks.
          Columns are the zones found on our blocks, with how many blocks each fronts. Click a cell to see the ordinance
          rows, values and conditions behind it.
        </p>
        <div className="legend-row">
          {["by_right", "special_exception", "prohibited", "legacy"].map((o) => (
            <span key={o}><Outcome outcome={o} /> {OUTCOME[o].text}</span>
          ))}
          <span><span className="tag derived">low</span> our reading is a stretch; asked of the SME</span>
        </div>
        <div className="table-wrap">
          <table className="use-matrix">
            <thead>
              <tr>
                <th>Recommendation type</th>
                {zones.map((z) => (
                  <th key={z} title={zoneInfo[z]?.name}>{z}<div className="assess">{zoneInfo[z]?.blocks.length} blk</div></th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.rec_label}>
                  <td className="um-label">
                    <div>
                      {r.rec_label} <span className="assess">×{r.rec_count}</span>{" "}
                      {r.confidence === "low" && <span className="tag derived" title={r.note}>low</span>}
                    </div>
                    <div className="assess">{r.uses.map((u) => u.row).join(r.mode === "all" ? " + " : " | ")}</div>
                  </td>
                  {zones.map((z) => (
                    <td key={z} className="um-cell">
                      <button
                        type="button"
                        className={"um-btn" + (pick?.rec === r.rec_label && pick?.zone === z ? " picked" : "")}
                        onClick={() => setPick({ rec: r.rec_label, zone: z })}
                        title={`${r.rec_label} in ${z}: ${(OUTCOME[r.cells[z].outcome] || OUTCOME.unknown).text}`}
                      >
                        <Outcome outcome={r.cells[z].outcome} />
                      </button>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      {pickedRow && <CellDetail row={pickedRow} zone={pick.zone} zoneName={zoneInfo[pick.zone]?.name} ov={ov} />}
    </>
  );
}

// ---------------------------------------------------------------- Sources

function SourcesPanel({ ov, onApplied }) {
  return (
    <div className="sources-grid">
      <div className="wide"><RefreshPanel onApplied={onApplied} /></div>
      <div className="card">
        <h4>Sources</h4>
        <References refs={ov.sources} />
      </div>
      <div className="card">
        <h4>How a finding is decided</h4>
        <ol className="how-list">
          <li>The zones within {ov.buffer_m} m of the block's centerline are read from the county zoning map.</li>
          <li>
            Each recommendation type is read as one or more ordinance uses. That reading is ours, version {ov.rules_version},
            and shown on every finding.
          </li>
          <li>
            The use table gives each use in each zone: {Object.entries(ov.use_table.key).map(([k, v]) => `${k} = ${v}`).join(" ")}{" "}
            {ov.use_table.unlisted_rule}
          </li>
          <li>{ov.legacy_rule.text}</li>
          <li>
            Overlays checked: {ov.overlays_checked.join(", ")}. {ov.overlays_not_checked}
          </li>
          <li>Approved-plan case numbers are read from: {ov.case_layers.join(", ")}.</li>
          <li>
            Zoning score: each "Increase" recommendation scores its best fronting zone (by right {ov.score_points.by_right},
            special exception {ov.score_points.special_exception}, prohibited {ov.score_points.prohibited}); the block score is
            the average. Legacy and unresolved zones are left out, never scored as prohibited, and "Decrease"
            recommendations are not scored.
          </li>
        </ol>
        <p className="assess">{ov.rules_status}</p>
      </div>
      <div className="card wide">
        <h4>Why the zones look the way they do</h4>
        {ov.zones.filter((z) => z.context.length).map((z) => (
          <div key={z.code} className="zone-context">
            <b>{z.code}</b> {z.name} <span className="assess">· fronts {z.blocks.length} block(s)</span>
            <ul>
              {z.context.map((c, i) => (
                <li key={i}>
                  {c.text}{" "}
                  <a href={c.ref.url} target="_blank" rel="noopener noreferrer">{c.ref.label} ↗</a>
                  {c.ref.detail && <span className="assess"> · {c.ref.detail}</span>}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- View

export default function ZoningView() {
  const [ov, setOv] = useState(null);
  const [error, setError] = useState(null);
  const [tab, setTabState] = useState(() => {
    const t = window.location.hash.split("/")[1];
    return TABS.some(([k]) => k === t) ? t : "blocks";
  });

  function setTab(t) {
    setTabState(t);
    window.history.replaceState(null, "", `#zoning/${t}`);
  }

  // Reloaded after a refresh is applied, so every view shows the new data version.
  function load() {
    call("GET", "/zoning/overview").then(setOv).catch((e) => setError(e.message));
  }

  useEffect(load, []);

  if (error) return <main className="view"><div className="pathways-wrap"><div className="banner err">Could not load zoning data — {error}</div></div></main>;
  if (!ov) return <main className="view"><div className="pathways-wrap"><div className="card spin">Loading zoning data…</div></div></main>;

  const st = ov.stats;
  const f = st.findings || {};
  return (
    <main className="view">
      <div className="pathways-wrap">
        <div className="card zoning-head">
          <h4>Zoning data</h4>
          <p className="assess" style={{ margin: ".2rem 0 .6rem" }}>
            Everything behind the zoning findings on the implementation plans: the county zoning map, the Zoning
            Ordinance's use tables, and our reading of each recommendation type. Nothing here is generated by a model.
          </p>
          <div className="stat-row">
            <span><b>{st.looked_up}/{st.blocks}</b> blocks looked up · {date(st.last_lookup)}</span>
            <span><b>{ov.zones.length}</b> zones found</span>
            <span><b>{st.legacy_blocks}</b> blocks touch a legacy zone</span>
            <span><b>{st.overlay_blocks}</b> blocks in an overlay</span>
            <span><b>{f.attached || 0}</b> findings resolved · <b>{f.partial || 0}</b> resolved, SME to confirm · <b>{f.needs_sme || 0}</b> need the SME</span>
            <span>Use table current through <b>{ov.use_table.amended_through}</b></span>
            <span>
              Data version <b>{ov.data_version.version}</b>
              {ov.data_version.version ? ` · applied by ${ov.data_version.applied_by} ${date(ov.data_version.created_at)}` : " · committed defaults"}
            </span>
          </div>
        </div>
        <nav className="subtabs">
          {TABS.map(([k, label]) => (
            <button key={k} className="tab" aria-selected={tab === k} onClick={() => setTab(k)}>{label}</button>
          ))}
        </nav>
        {tab === "blocks" && <BlocksPanel ov={ov} />}
        {tab === "table" && <UseTablePanel ov={ov} />}
        {tab === "sources" && <SourcesPanel ov={ov} onApplied={load} />}
      </div>
    </main>
  );
}
