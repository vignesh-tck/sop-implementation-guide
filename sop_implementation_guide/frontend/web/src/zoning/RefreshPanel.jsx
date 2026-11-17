import { useCallback, useEffect, useState } from "react";
import { call, num } from "../api.js";
import { Outcome } from "./ZoningParts.jsx";

// Refresh the zoning data from sources the user names. Nothing changes until
// the result is reviewed and applied: a refresh runs on the server, produces a
// diff against the data in force, and waits. Applying stores a new data
// version; every finding, plan and score uses it from then on.

const NAME_KEY = "sop.zoning.requester";
const readName = () => { try { return localStorage.getItem(NAME_KEY) || ""; } catch { return ""; } };
const date = (iso) => (iso ? iso.slice(0, 16).replace("T", " ") : "—");

const RUN_STATUS = {
  running: { text: "running", cls: "edited" },
  ready: { text: "ready for review", cls: "derived" },
  failed: { text: "failed", cls: "gone" },
  applied: { text: "applied", cls: "ok" },
  discarded: { text: "discarded", cls: "" },
};

function RawValue({ v }) {
  const map = { P: "by_right", A: "by_right", SE: "special_exception", X: "prohibited" };
  return v ? <Outcome outcome={map[v] || "unknown"} /> : <span className="assess">not listed</span>;
}

function inputsSummary(inputs) {
  if (!inputs) return "";
  const parts = [];
  if (inputs.use_table?.mode === "extract") parts.push("use table re-extracted");
  if (inputs.map?.mode === "lookup") {
    const b = inputs.map.blocks?.length ? `blocks ${inputs.map.blocks.join(", ")}` : "all blocks";
    parts.push(`zones looked up (${b}, ${inputs.map.buffer_m} m)`);
  }
  return parts.join(" · ");
}

function Diff({ diff }) {
  const ut = diff.use_table;
  const nothing = !ut.cells_changed_count && !ut.uses_added.length && !ut.uses_removed.length &&
    !diff.settings_changed.length && !diff.blocks_changed.length && !diff.outcome_flips.length &&
    !diff.score_changes.length && !diff.lookup_failures.length;
  return (
    <div className="refresh-diff">
      <div className="stat-row">
        {ut.refreshed && <span>Use table: <b>{ut.old_amended_through}</b> → <b>{ut.new_amended_through}</b>, <b>{ut.cells_changed_count}</b> cell(s) changed</span>}
        <span><b>{diff.outcome_flips.length}</b> recommendation outcome(s) flip</span>
        <span><b>{diff.blocks_looked_up}</b> block(s) looked up, <b>{diff.blocks_changed.length}</b> changed{diff.lookup_failures.length ? `, ${diff.lookup_failures.length} failed` : ""}</span>
        <span><b>{diff.score_changes.length}</b> block score(s) change</span>
      </div>
      {nothing && <div className="banner ok" style={{ marginTop: ".6rem" }}>No differences: applying would only record that the data was re-checked.</div>}

      {diff.outcome_flips.length > 0 && (
        <>
          <div className="zoning-h">Recommendation outcomes that flip</div>
          <table className="facts"><tbody>
            {diff.outcome_flips.map((f, i) => (
              <tr key={i}><td>{f.rec_label}</td><td>{f.zone}</td><td><Outcome outcome={f.old} /> → <Outcome outcome={f.new} /></td></tr>
            ))}
          </tbody></table>
        </>
      )}
      {diff.blocks_changed.length > 0 && (
        <>
          <div className="zoning-h">Blocks whose zoning changes</div>
          <table className="facts"><tbody>
            {diff.blocks_changed.map((b) => (
              <tr key={b.id}>
                <td>Block {b.id} <span className="assess">{b.street_name}</span></td>
                <td>
                  {b.old.zones.join(", ") || "none"} → <b>{b.new.zones.join(", ") || "none"}</b>
                  {b.old.cases !== b.new.cases && <div className="assess">cases {b.old.cases} → {b.new.cases}</div>}
                  {b.old.overlays.join() !== b.new.overlays.join() && <div className="assess">overlays {b.old.overlays.join(", ") || "none"} → {b.new.overlays.join(", ") || "none"}</div>}
                </td>
              </tr>
            ))}
          </tbody></table>
        </>
      )}
      {diff.score_changes.length > 0 && (
        <>
          <div className="zoning-h">Zoning scores that change</div>
          <table className="facts"><tbody>
            {diff.score_changes.map((s) => (
              <tr key={s.id}><td>Block {s.id} <span className="assess">{s.street_name}</span></td><td>{num(s.old, 0)} → <b>{num(s.new, 0)}</b></td></tr>
            ))}
          </tbody></table>
        </>
      )}
      {ut.cells_changed_count > 0 && (
        <>
          <div className="zoning-h">Use-table cells that change ({ut.cells_changed_count})</div>
          <table className="facts"><tbody>
            {ut.cells_changed.slice(0, 60).map((c, i) => (
              <tr key={i}><td>{c.use}</td><td>{c.zone}</td><td><RawValue v={c.old} /> → <RawValue v={c.new} /></td></tr>
            ))}
          </tbody></table>
          {ut.cells_changed_count > 60 && <div className="assess">…and {ut.cells_changed_count - 60} more.</div>}
        </>
      )}
      {(ut.uses_added.length > 0 || ut.uses_removed.length > 0) && (
        <p className="assess">
          {ut.uses_added.length > 0 && <>Uses added: {ut.uses_added.join("; ")}. </>}
          {ut.uses_removed.length > 0 && <>Uses removed: {ut.uses_removed.join("; ")}.</>}
        </p>
      )}
      {diff.settings_changed.length > 0 && (
        <>
          <div className="zoning-h">Settings that change</div>
          <table className="facts"><tbody>
            {diff.settings_changed.map((s) => (
              <tr key={s.key}><td>{s.key}</td><td className="cases">{String(s.old)} → <b>{String(s.new)}</b></td></tr>
            ))}
          </tbody></table>
        </>
      )}
      {diff.lookup_failures.length > 0 && (
        <>
          <div className="zoning-h">Lookups that failed (these blocks keep their current zones)</div>
          <ul className="assess">{diff.lookup_failures.map((f) => <li key={f.id}>Block {f.id} {f.street_name}: {f.error}</li>)}</ul>
        </>
      )}
    </div>
  );
}

export default function RefreshPanel({ onApplied }) {
  const [info, setInfo] = useState(null);
  const [runs, setRuns] = useState([]);
  const [loadError, setLoadError] = useState(null);
  const [form, setForm] = useState(null);
  const [name, setName] = useState(readName);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [run, setRun] = useState(null);
  const [message, setMessage] = useState(null);

  const loadInfo = useCallback(() => {
    Promise.all([call("GET", "/zoning/settings"), call("GET", "/zoning/refresh")])
      .then(([s, r]) => {
        setInfo(s);
        setRuns(r.runs);
        setForm((f) => f || {
          utMode: "keep", utUrl: s.use_table.url || "",
          mapMode: "keep", ...s.settings, blocks: "",
        });
        const open = r.runs.find((x) => x.status === "running" || x.status === "ready");
        if (open) setRun((cur) => cur || open);
      })
      .catch((e) => setLoadError(e.message));
  }, []);

  useEffect(loadInfo, [loadInfo]);
  useEffect(() => { try { localStorage.setItem(NAME_KEY, name); } catch { /* per-viewer convenience only */ } }, [name]);

  // Poll a running refresh until it is ready or failed.
  const runId = run?.id;
  const running = run?.status === "running";
  useEffect(() => {
    if (!runId || !running) return undefined;
    const t = setInterval(() => {
      call("GET", `/zoning/refresh/${runId}`).then((r) => {
        setRun(r);
        if (r.status !== "running") loadInfo();
      }).catch((e) => setError(e.message));
    }, 2000);
    return () => clearInterval(t);
  }, [runId, running, loadInfo]);

  if (loadError) return <div className="card"><div className="banner err">Could not load refresh settings — {loadError}</div></div>;
  if (!info || !form) return <div className="card spin">Loading refresh settings…</div>;

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  async function start() {
    setBusy(true); setError(null); setMessage(null);
    try {
      const blocks = form.blocks.trim()
        ? form.blocks.split(/[\s,]+/).filter(Boolean).map(Number)
        : null;
      if (blocks && blocks.some((b) => !Number.isInteger(b))) throw new Error("Blocks must be ids like 15, 16.");
      const r = await call("POST", "/zoning/refresh", {
        use_table: { mode: form.utMode, url: form.utUrl },
        map: {
          mode: form.mapMode, zoning_layer_url: form.zoning_layer_url, zone_field: form.zone_field,
          prior_layer_url: form.prior_layer_url, prior_field: form.prior_field,
          buffer_m: Number(form.buffer_m), blocks,
        },
        requested_by: name, note,
      });
      setRun(r);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function review(action) {
    setBusy(true); setError(null);
    try {
      const r = await call("POST", `/zoning/refresh/${run.id}/${action}`, { reviewed_by: name });
      setMessage(action === "apply"
        ? `Applied as data version ${r.version}; ${r.blocks_updated} block(s) updated. Findings, plans and scores now use it.`
        : `Refresh #${run.id} discarded.`);
      setRun(null);
      loadInfo();
      if (action === "apply") onApplied?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const active = info.active;
  return (
    <div className="card refresh-panel">
      <h4>Refresh zoning data</h4>
      <p className="assess" style={{ marginTop: 0 }}>
        In force: <b>data version {active.version}</b>
        {active.version ? ` · applied by ${active.applied_by} on ${date(active.created_at)}` : " (the use table committed in the repo and the default county layers)"}
        {" · "}use table current through <b>{info.use_table.amended_through}</b>.
        Point a refresh at a newer source, review what would change, then apply it.
      </p>
      {!info.storage_ready && (
        <div className="banner warn">
          Refresh is off until its tables exist. Paste the ZONING DATA VERSIONS AND REFRESH RUNS section of{" "}
          <code>backend/db/schema.sql</code> into the Supabase SQL editor and run it. Everything else works.
        </div>
      )}

      {run && (run.status === "running" || run.status === "ready" || run.status === "failed") ? (
        <div className="refresh-run">
          <div className="zl-top">
            <b>Refresh #{run.id}</b>
            <span className={"tag " + (RUN_STATUS[run.status]?.cls || "")}>{RUN_STATUS[run.status]?.text}</span>
            <span className="assess">{inputsSummary(run.inputs)} · by {run.requested_by}</span>
          </div>
          {run.status === "running" && (
            <div className="refresh-progress">
              <div className="assess">{run.progress?.step || "Working"}…{run.progress?.total ? ` (${run.progress.done}/${run.progress.total})` : ""}</div>
              {run.progress?.total > 0 && <div className="bar"><div style={{ width: `${(100 * run.progress.done) / run.progress.total}%` }} /></div>}
            </div>
          )}
          {run.status === "failed" && <div className="banner err">{run.error}</div>}
          {run.status === "ready" && run.diff && <Diff diff={run.diff} />}
          {run.status !== "running" && (
            <div className="plan-edit-row">
              <label>
                Reviewer
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name (required to apply)" />
              </label>
              <div className="btn-row" style={{ alignSelf: "flex-end" }}>
                {run.status === "ready" && (
                  <button className="primary" onClick={() => review("apply")} disabled={busy || !name.trim()}>Apply as new data version</button>
                )}
                <button onClick={() => review("discard")} disabled={busy}>Discard</button>
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="refresh-form">
          <fieldset>
            <legend>Use table</legend>
            <label className="radio"><input type="radio" checked={form.utMode === "keep"} onChange={() => setForm((f) => ({ ...f, utMode: "keep" }))} /> Keep the current table</label>
            <label className="radio"><input type="radio" checked={form.utMode === "extract"} onChange={() => setForm((f) => ({ ...f, utMode: "extract" }))} /> Re-extract from this county code link</label>
            {form.utMode === "extract" && (
              <label className="field">
                Link to Section 27-5101 on enCodePlus
                <input value={form.utUrl} onChange={set("utUrl")} placeholder="https://online.encodeplus.com/regs/princegeorgescounty-md/doc-viewer.aspx#secid-649" />
              </label>
            )}
          </fieldset>
          <fieldset>
            <legend>Zoning map</legend>
            <label className="radio"><input type="radio" checked={form.mapMode === "keep"} onChange={() => setForm((f) => ({ ...f, mapMode: "keep" }))} /> Keep the current block zones</label>
            <label className="radio"><input type="radio" checked={form.mapMode === "lookup"} onChange={() => setForm((f) => ({ ...f, mapMode: "lookup" }))} /> Look up block zones again</label>
            {form.mapMode === "lookup" && (
              <div className="refresh-grid">
                <label className="field wide">Zoning layer (ArcGIS REST URL)<input value={form.zoning_layer_url} onChange={set("zoning_layer_url")} /></label>
                <label className="field">Zone field<input value={form.zone_field} onChange={set("zone_field")} /></label>
                <label className="field wide">Prior zoning layer<input value={form.prior_layer_url} onChange={set("prior_layer_url")} /></label>
                <label className="field">Prior zone field<input value={form.prior_field} onChange={set("prior_field")} /></label>
                <label className="field">Buffer around the street (m)<input type="number" min="5" max="100" value={form.buffer_m} onChange={set("buffer_m")} /></label>
                <label className="field">Blocks (blank = all)<input value={form.blocks} onChange={set("blocks")} placeholder="e.g. 15, 16" /></label>
              </div>
            )}
          </fieldset>
          <div className="plan-edit-row">
            <label>Requested by<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name" /></label>
            <label className="grow">Reason<input value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. Council passed CB-012-2027; SME asked for a wider buffer" /></label>
          </div>
          <div className="btn-row" style={{ marginTop: ".6rem" }}>
            <button className="primary" onClick={start} disabled={busy || !info.storage_ready || !name.trim()}>
              {busy ? "Starting…" : "Run refresh (review before anything changes)"}
            </button>
          </div>
        </div>
      )}
      {error && <div className="banner err" style={{ marginTop: ".6rem" }}>{error}</div>}
      {message && <div className="banner ok" style={{ marginTop: ".6rem" }}>{message}</div>}

      {(runs.length > 0 || info.history.length > 0) && (
        <details className="refresh-history">
          <summary>History ({info.history.length} version(s), {runs.length} recent refresh(es))</summary>
          {info.history.length > 0 && (
            <table className="facts"><tbody>
              {info.history.map((v) => (
                <tr key={v.version}><td>Version {v.version}</td><td>{v.applied_by} · {date(v.created_at)}{v.note ? ` · ${v.note}` : ""}{v.run_id ? ` · from refresh #${v.run_id}` : ""}</td></tr>
              ))}
            </tbody></table>
          )}
          <table className="facts"><tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td>Refresh #{r.id} <span className={"tag " + (RUN_STATUS[r.status]?.cls || "")}>{RUN_STATUS[r.status]?.text}</span></td>
                <td>{inputsSummary(r.inputs)} · {r.requested_by} · {date(r.created_at)}{r.error ? ` · ${r.error}` : ""}</td>
              </tr>
            ))}
          </tbody></table>
        </details>
      )}
    </div>
  );
}
