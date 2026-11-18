import { Fragment, useState } from "react";
import { call } from "../api.js";

const KIND_TEXT = {
  extracted: "read from the source",
  derived: "computed by a step",
  inferred: "model judgement (approved step)",
  assumed: "value you stated",
};

function StatusTag({ status }) {
  if (status === "approved") return <span className="tag ok">approved</span>;
  if (status === "proposed") return <span className="tag edited">needs approval</span>;
  return <span className="tag">not set</span>;
}

function short(v, n = 80) {
  if (v == null) return "";
  const s = typeof v === "object" ? JSON.stringify(v) : String(v);
  return s.length > n ? s.slice(0, n) + "…" : s;
}

// ── Target schema ────────────────────────────────────────────────────────────

function SchemaCard({ view, act, disabled }) {
  const schema = view.definition.schema || {};
  const fields = schema.fields || [];
  const cov = view.last_run?.coverage?.fields || {};
  const total = view.last_run?.coverage?.records;
  return (
    <div className="card">
      <h4>Target schema <StatusTag status={schema.status} /></h4>
      <div className="muted-note" style={{ marginTop: 0, marginBottom: ".5rem" }}>
        What every record must contain. Required fields gate the load: the agent asks for more data when sources can't fill them.
      </div>
      {fields.length === 0 ? (
        <div className="btn-row" style={{ marginTop: 0 }}>
          <button onClick={() => act("POST", "/schema/default")} disabled={disabled}>Use default {view.kind_label.toLowerCase()} fields</button>
          <span className="assess" style={{ alignSelf: "center" }}>or describe your fields in the chat.</span>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="pl-fields">
            <thead><tr><th>Field</th><th>Type</th><th>Req.</th><th>Filled</th><th title="Load even though some records leave this blank">Accept blanks</th></tr></thead>
            <tbody>
              {fields.map((f) => {
                const c = cov[f.name];
                const gap = f.required && c && c.missing > 0;
                return (
                  <tr key={f.name} className={gap && !f.accept_blank ? "gap" : ""}>
                    <td><code>{f.name}</code>
                      {f.extract === false && <span className="tag derived" style={{ marginLeft: ".35rem" }}
                        title="A judgement, not a stated fact: extraction leaves it blank and a model step fills it, marked inferred">judgement</span>}
                      <div className="assess">{f.description}{f.enum_values ? ` (${f.enum_values.join(", ")})` : ""}</div></td>
                    <td className="assess">{f.type}</td>
                    <td>{f.required ? "yes" : ""}</td>
                    <td className="assess">{c ? `${c.filled}/${total}` : "—"}</td>
                    <td>
                      {f.required && (
                        <input type="checkbox" checked={!!f.accept_blank} disabled={disabled}
                               onChange={(e) => act("POST", `/fields/${f.name}/accept-blank`, { accept: e.target.checked })} />
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {fields.length > 0 && schema.status !== "approved" && (
        <div className="btn-row">
          <button className="primary" onClick={() => act("POST", "/schema/approve")} disabled={disabled}>Approve schema</button>
        </div>
      )}
    </div>
  );
}

// ── Sources ──────────────────────────────────────────────────────────────────

function SourcesCard({ view, act, disabled }) {
  const [url, setUrl] = useState("");
  const [query, setQuery] = useState("");
  const sources = view.definition.sources || [];
  async function addUrl() {
    if (!url.trim()) return;
    if (await act("POST", "/sources", { kind: "url", url: url.trim() })) setUrl("");
  }
  async function addQuery() {
    if (!query.trim()) return;
    if (await act("POST", "/sources", { kind: "grants_gov", query: query.trim() })) setQuery("");
  }
  return (
    <div className="card">
      <h4>Sources</h4>
      {sources.length === 0 && <div className="assess">None yet. Paste URLs in the chat or below, or attach files in the chat.</div>}
      <ul className="sources-list">
        {sources.map((s) => {
          const meta = view.sources[s.id] || {};
          return (
            <li key={s.id}>
              <b className="pl-id">{s.id}</b>
              <span className="url" title={s.url || s.query}>{s.label}</span>
              <span className={"assess" + (meta.error ? " pl-bad" : "")} title={meta.error || meta.preview}>
                {meta.error ? "unreadable" : meta.chars != null ? `${meta.chars.toLocaleString()} chars${meta.truncated ? " (truncated)" : ""}` : ""}
              </span>
              <button className="rm" title="Remove" disabled={disabled} onClick={() => act("DELETE", `/sources/${s.id}`)}>×</button>
            </li>
          );
        })}
      </ul>
      <div className="pl-add">
        <input placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addUrl()} />
        <button onClick={addUrl} disabled={disabled || !url.trim()}>Add URL</button>
      </div>
      {view.kind === "funding" && (
        <div className="pl-add">
          <input placeholder="Grants.gov keywords, e.g. pedestrian safety" value={query} onChange={(e) => setQuery(e.target.value)}
                 onKeyDown={(e) => e.key === "Enter" && addQuery()} />
          <button onClick={addQuery} disabled={disabled || !query.trim()}>Add search</button>
        </div>
      )}
    </div>
  );
}

// ── Steps ────────────────────────────────────────────────────────────────────

function StepsCard({ view, act, disabled, selected, onSelect }) {
  const steps = view.definition.steps || [];
  return (
    <div className="card">
      <h4>Steps</h4>
      {steps.length === 0 && <div className="assess">The agent proposes steps once the schema and sources are in place.</div>}
      <ol className="pl-steps">
        {steps.map((s, i) => {
          const out = view.outputs[s.id];
          const cat = view.catalog[s.type] || {};
          return (
            <li key={s.id} className={(selected === s.id ? "sel " : "") + (out?.error ? "failed" : "")}>
              <div className="pl-step-head">
                <b className="pl-id">{s.id}</b>
                <b>{cat.label || s.type}</b>
                {cat.model && <span className="tag derived" title="Calls the model; output is cached on its inputs">model</span>}
                <StatusTag status={s.status} />
                <span className="spacer" />
                {out && !out.error && (
                  <button className="link" onClick={() => onSelect(s.id)}>{out.count} record(s)</button>
                )}
              </div>
              {s.note && <div className="pl-step-note">{s.note}</div>}
              <code className="pl-config" title={JSON.stringify(s.config, null, 2)}>{short(s.config, 160)}</code>
              {out?.error && <div className="banner err" style={{ margin: ".4rem 0 0" }}>{out.error}</div>}
              {out?.warnings?.length > 0 && <div className="pl-warn">{out.warnings.join(" · ")}</div>}
              <div className="pl-step-actions">
                {s.status !== "approved" && (
                  <button className="primary" disabled={disabled} onClick={() => act("POST", `/steps/${s.id}/approve`)}>Approve</button>
                )}
                <button disabled={disabled} onClick={() => act("POST", "/run", { upto: s.id })}>Preview to here</button>
                <button disabled={disabled || i === 0} title="Move up" onClick={() => act("POST", `/steps/${s.id}/move`, { direction: -1 })}>↑</button>
                <button disabled={disabled || i === steps.length - 1} title="Move down" onClick={() => act("POST", `/steps/${s.id}/move`, { direction: 1 })}>↓</button>
                <button disabled={disabled} onClick={() => act("DELETE", `/steps/${s.id}`)}>Remove</button>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// ── Output preview ───────────────────────────────────────────────────────────

function OutputCard({ view, selected, onSelect }) {
  const [open, setOpen] = useState(null);
  const fields = view.definition.schema?.fields || [];
  const out = selected ? view.outputs[selected] : null;
  const records = out ? out.records || [] : view.last_records || [];
  const title = out ? `Output of ${selected}` : "Output of the last run";
  if (!records.length && !view.last_run) return null;
  return (
    <div className="card">
      <h4>
        {title} <span className="assess">({out ? out.count : view.last_run?.coverage?.records ?? 0} record(s){records.length < (out?.count ?? 0) ? `, first ${records.length} shown` : ""})</span>
        {selected && <button className="link" style={{ marginLeft: ".5rem" }} onClick={() => onSelect(null)}>show final</button>}
      </h4>
      <div className="pl-legend">
        {Object.entries(KIND_TEXT).map(([k, t]) => <span key={k} className={"pl-cell " + k}>{t}</span>)}
        <span className="pl-cell missing">blank</span>
      </div>
      {records.length === 0 ? <div className="assess">No records.</div> : (
        <div className="table-wrap pl-records">
          <table>
            <thead><tr><th></th>{fields.map((f) => <th key={f.name}>{f.name}{f.required ? "*" : ""}</th>)}</tr></thead>
            <tbody>
              {records.map((r) => (
                <Fragment key={r.id}>
                  <tr onClick={() => setOpen(open === r.id ? null : r.id)} className="pl-rec">
                    <td className="assess" title={r.excerpt_verified === false ? "Excerpt not found in the source" : ""}>
                      {r.id}{r.excerpt_verified === false && <span className="pl-bad"> ⚠</span>}
                    </td>
                    {fields.map((f) => {
                      const p = r.prov?.[f.name];
                      const v = r.values?.[f.name];
                      const blank = v == null || v === "";
                      return (
                        <td key={f.name} className={"pl-cell " + (blank ? (f.required ? "missing" : "") : p?.kind || "")}
                            title={p ? `${KIND_TEXT[p.kind] || p.kind} (${p.step})${p.note ? ": " + p.note : ""}` : ""}>
                          {blank ? "—" : short(v, 60)}
                        </td>
                      );
                    })}
                  </tr>
                  {open === r.id && (
                    <tr className="pl-rec-detail">
                      <td colSpan={fields.length + 1}>
                        <div><b>Source:</b> <a href={r.source_url} target="_blank" rel="noreferrer">{r.source_url}</a>
                          {r.confidence != null && <span className="assess"> · confidence {Number(r.confidence).toFixed(2)}</span>}</div>
                        <div><b>Excerpt</b> {r.excerpt_verified ? <span className="tag ok">found in source</span> : <span className="tag gone">not found in source</span>}</div>
                        <blockquote>{r.excerpt || "(none)"}</blockquote>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Run and load ─────────────────────────────────────────────────────────────

function LoadCard({ view, act, disabled }) {
  const [name, setName] = useState("");
  const [by, setBy] = useState("");
  const [diff, setDiff] = useState(null);
  const [diffError, setDiffError] = useState(null);
  const blocked = view.gate.length > 0;

  async function checkDiff() {
    setDiffError(null);
    try { setDiff(await call("GET", `/pipelines/${view.id}/diff`)); } catch (e) { setDiffError(e.message); }
  }
  async function load() {
    const r = await act("POST", "/load", { name: name.trim() || null, created_by: by.trim() || null });
    if (r) { setName(""); setDiff(null); }
  }

  return (
    <div className="card">
      <h4>Run and load</h4>
      <div className="btn-row" style={{ marginTop: 0 }}>
        <button className="primary" disabled={disabled} onClick={() => act("POST", "/run", {})}>Run pipeline</button>
        <button disabled={disabled} onClick={() => act("POST", "/run", { fresh: true })}
                title="Refetch every source and redo model steps instead of using cached results">Re-run fresh</button>
        {view.last_run && <span className="assess" style={{ alignSelf: "center" }}>
          Last run {(view.last_run.at || "").slice(0, 16).replace("T", " ")}{view.last_run.upto ? ` (preview to ${view.last_run.upto})` : ""}
        </span>}
      </div>
      {blocked ? (
        <div className="pl-gate">
          <b>Not ready to load:</b>
          <ul>{view.gate.map((g, i) => <li key={i}>{g}</li>)}</ul>
        </div>
      ) : (
        <>
          <div className="banner ok" style={{ marginTop: ".75rem" }}>Ready to load. Loading saves a new {view.kind_label} version.</div>
          <div className="pl-add">
            <input placeholder="Version name (optional)" value={name} onChange={(e) => setName(e.target.value)} />
            <input placeholder="Your name (optional)" value={by} onChange={(e) => setBy(e.target.value)} />
          </div>
          <div className="btn-row">
            <button onClick={checkDiff} disabled={disabled}>Check what changes</button>
            <button className="primary" onClick={load} disabled={disabled}>Load as new version</button>
          </div>
          {diffError && <div className="banner err">{diffError}</div>}
          {diff && (
            <div className="pl-diff">
              <div>Against {diff.base_version ? `${view.kind_label} v${diff.base_version.version}` : "an empty catalogue"} ({diff.catalogue_size} programme(s)):</div>
              <div><b>{diff.added.length}</b> new · <b>{diff.changed.length}</b> changed · <b>{diff.unchanged}</b> unchanged</div>
              {diff.added.length > 0 && <div className="assess">New: {diff.added.map((a) => a.program_name).join("; ")}</div>}
              {diff.changed.length > 0 && <div className="assess">Changed: {diff.changed.map((c) => `${c.program_name} (${c.fields.join(", ")})`).join("; ")}</div>}
              <div className="assess">{diff.note}</div>
            </div>
          )}
        </>
      )}
      {view.loads.length > 0 && (
        <>
          <h4 style={{ marginTop: "1rem" }}>Loads</h4>
          <table>
            <thead><tr><th>Version</th><th>Name</th><th>Pipeline def</th><th>Records</th><th>When</th></tr></thead>
            <tbody>
              {view.loads.map((l) => (
                <tr key={l.id}>
                  <td><b>{view.kind_label} v{l.dataset_version}</b></td>
                  <td className="assess">{l.name || "—"}</td>
                  <td className="assess">def v{l.definition_version}</td>
                  <td className="assess">{l.coverage?.records}</td>
                  <td className="assess">{(l.created_at || "").slice(0, 16).replace("T", " ")}{l.created_by ? ` · ${l.created_by}` : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

export default function PipelinePanel({ view, act, pending }) {
  const [selected, setSelected] = useState(null);
  const disabled = pending || !!view.busy;
  const sel = selected && view.outputs[selected] ? selected : null;
  return (
    <div className="pl-panel-inner">
      <div className="pl-panel-head">
        <h3>{view.name}</h3>
        <span className="assess">{view.kind_label} · def v{view.definition_version}</span>
      </div>
      <SchemaCard view={view} act={act} disabled={disabled} />
      <SourcesCard view={view} act={act} disabled={disabled} />
      <StepsCard view={view} act={act} disabled={disabled} selected={sel} onSelect={setSelected} />
      <OutputCard view={view} selected={sel} onSelect={setSelected} />
      <LoadCard view={view} act={act} disabled={disabled} />
    </div>
  );
}
