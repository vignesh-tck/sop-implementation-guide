import { useState } from "react";
import SourceReader from "./SourceReader.jsx";
import Stepper from "./Stepper.jsx";
import { PAGES } from "./nav.js";
import { stepsModel } from "./ports.js";

function StatusTag({ status }) {
  if (status === "approved") return <span className="tag ok">approved</span>;
  if (status === "proposed") return <span className="tag edited">needs approval</span>;
  return <span className="tag">not set</span>;
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
        What every record must contain, named as you like. Required fields gate the load: the agent asks for more data when sources
        can't fill them. At Load the fields map onto the {view.kind_label.toLowerCase()} catalogue (shown on the Output node); only a name is needed.
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

function SourcesCard({ view, act, disabled, picked, onPick }) {
  const [url, setUrl] = useState("");
  const [query, setQuery] = useState("");
  const sources = stepsModel(view).sources;       // the draft's while one is open
  const goingAway = view.draft?.removed_sources || [];
  async function addUrl() {
    if (!url.trim()) return;
    if (await act("POST", "/sources", { kind: "url", url: url.trim() })) setUrl("");
  }
  async function addQuery() {
    if (!query.trim()) return;
    if (await act("POST", "/sources", { kind: "grants_gov", query: query.trim() })) setQuery("");
  }
  const final = view.definition.sources_status === "final";
  const schemaOk = view.definition.schema?.status === "approved";
  const unreadable = sources.filter((s) => view.sources[s.id]?.error).map((s) => s.id);
  return (
    <div className="card">
      <h4>Sources {final ? <span className="tag ok">final</span> : sources.length > 0 && <span className="tag edited">open</span>}</h4>
      <div className="muted-note" style={{ marginTop: 0, marginBottom: ".5rem" }}>
        {final
          ? "These sources are final. Adding or removing one now goes into the draft (Pipeline page): wire a new source to an Extract step, then Apply. Nothing resets until then."
          : "Add every source the pipeline should read, then mark them final: the agent takes a quick look at each and outlines all the steps."}
      </div>
      {sources.length === 0 && <div className="assess">None yet. Paste URLs in the chat or below, or attach files in the chat.</div>}
      <ul className="sources-list">
        {sources.map((s) => {
          const meta = view.sources[s.id] || {};
          return (
            <li key={s.id} className={"pl-src" + (picked === s.id ? " sel" : "")} onClick={() => onPick(s.id)} title="Read this source">
              <b className="pl-id">{s.id}</b>
              <span className="url" title={s.url || s.query}>{s.label}</span>
              {s.draft_state === "new" && <span className="tag edited">new in draft</span>}
              <span className={"assess" + (meta.error ? " pl-bad" : "")} title={meta.error || meta.preview}>
                {meta.error ? "unreadable" : meta.chars != null ? `${meta.chars.toLocaleString()} chars${meta.truncated ? " (truncated)" : ""}` : ""}
              </span>
              <button className="rm" title="Remove" disabled={disabled}
                      onClick={(e) => { e.stopPropagation(); act("DELETE", `/sources/${s.id}`); }}>×</button>
            </li>
          );
        })}
      </ul>
      {goingAway.length > 0 && <div className="assess">Removed in the draft (gone when you Apply): {goingAway.join(", ")}</div>}
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
      {!final && sources.length > 0 && (
        <div className="btn-row">
          <button className="primary" disabled={disabled || !schemaOk || unreadable.length > 0}
                  onClick={() => act("POST", "/sources/final")}>Sources are final</button>
          <span className="assess" style={{ alignSelf: "center" }}>
            {!schemaOk ? "Approve the target schema first."
              : unreadable.length ? `${unreadable.join(", ")} could not be read: remove or replace first.`
              : "The agent then takes a quick look at each source and outlines the steps."}
          </span>
        </div>
      )}
    </div>
  );
}

// ── The wizard: one page per design stage, in the order the user set ────────

function SourcesPage({ view, act, disabled, initialPick }) {
  const sources = stepsModel(view).sources;
  const [picked, setPicked] = useState(initialPick ?? sources[0]?.id ?? null);
  const src = sources.find((s) => s.id === picked) || sources[sources.length - 1] || null;
  return (
    <div className="pl-sources-page">
      <SourcesCard view={view} act={act} disabled={disabled} picked={src?.id} onPick={setPicked} />
      <SourceReader pipelineId={view.id} source={src} meta={src ? view.sources[src.id] : null} />
    </div>
  );
}

// The schema and sources pages (Pipeline and Review & publish have their own
// full-width pages). `nav` is the page state from usePageNav.
export default function PipelinePanel({ view, act, pending, nav, sourcePick }) {
  const disabled = pending || !!view.busy;
  const { page, reached, go } = nav;

  let body;
  if (page === 0) body = <SchemaCard view={view} act={act} disabled={disabled} />;
  else body = <SourcesPage key={sourcePick || "-"} view={view} act={act} disabled={disabled} initialPick={sourcePick} />;

  return (
    <div className="pl-panel-inner">
      <div className="pl-panel-head">
        <h3>{view.name}</h3>
        <span className="assess">{view.kind_label} · def v{view.definition_version}</span>
      </div>
      <Stepper view={view} page={page} reached={reached} onGo={go} />
      <div className="pl-pages">
        <div key={PAGES[page].key} className={"pl-page " + nav.dir}>
          {body}
          {page < reached && (
            <div className="pl-page-foot">
              <span className="assess">This stage is done. Changing it may send later stages back for approval.</span>
              <button className="primary" onClick={() => go(reached)}>Back to {PAGES[reached].label.toLowerCase()} →</button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
