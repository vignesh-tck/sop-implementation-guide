import { Fragment, useState } from "react";
import { call } from "../api.js";

const KIND_TEXT = {
  extracted: "read from the source",
  derived: "computed by a step",
  inferred: "model judgement (approved step)",
  assumed: "value you stated",
  manual: "you entered it in review",
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

function editCount(edits) {
  return edits ? (edits.changed?.length || 0) + (edits.removed?.length || 0) + (edits.added?.length || 0) : 0;
}

function StepsCard({ view, act, disabled }) {
  const steps = view.definition.steps || [];
  const [open, setOpen] = useState(null);
  return (
    <div className="card">
      <h4>Steps</h4>
      <div className="muted-note" style={{ marginTop: 0, marginBottom: ".5rem" }}>
        Preview a step, open <b>Review</b> to check its records (correct values, remove or add records), then approve it.
        Your edits are kept on the step and re-applied every run.
      </div>
      {steps.length === 0 && <div className="assess">The agent proposes steps once the schema and sources are in place.</div>}
      <ol className="pl-steps">
        {steps.map((s, i) => {
          const out = view.outputs[s.id];
          const cat = view.catalog[s.type] || {};
          const n = editCount(s.edits);
          const isOpen = open === s.id && out && !out.error;
          return (
            <li key={s.id} className={(isOpen ? "sel " : "") + (out?.error ? "failed" : "")}>
              <div className="pl-step-head">
                <b className="pl-id">{s.id}</b>
                <b>{cat.label || s.type}</b>
                {cat.model && <span className="tag derived" title="Calls the model; output is cached on its inputs">model</span>}
                <StatusTag status={s.status} />
                {n > 0 && <span className="tag manual" title="Your corrections to this step's output">{n} edit{n > 1 ? "s" : ""} by you</span>}
                <span className="spacer" />
                {out && !out.error && <span className="assess">{out.count} record(s){out.stale ? " · out of date" : ""}</span>}
              </div>
              {s.note && <div className="pl-step-note">{s.note}</div>}
              <code className="pl-config" title={JSON.stringify(s.config, null, 2)}>{short(s.config, 160)}</code>
              {out?.error && <div className="banner err" style={{ margin: ".4rem 0 0" }}>{out.error}</div>}
              {out?.stale && <div className="pl-warn">An earlier step's output was edited: preview again to update this one.</div>}
              {out?.warnings?.length > 0 && !isOpen && <div className="pl-warn">{short(out.warnings.join(" · "), 220)}</div>}
              <div className="pl-step-actions">
                {out && !out.error ? (
                  <button className={s.status !== "approved" ? "primary" : ""} onClick={() => setOpen(isOpen ? null : s.id)}>
                    {isOpen ? "Close review ▴" : `Review ${out.count} record(s) ▾`}
                  </button>
                ) : (
                  <button className={s.status !== "approved" ? "primary" : ""} disabled={disabled}
                          onClick={() => act("POST", "/run", { upto: s.id }).then((r) => r && setOpen(s.id))}>
                    Preview to review
                  </button>
                )}
                {out && !out.error && <button disabled={disabled} onClick={() => act("POST", "/run", { upto: s.id })}>Preview again</button>}
                {s.status !== "approved" && !isOpen && (
                  <button disabled={disabled} title="Approve without reviewing the output"
                          onClick={() => act("POST", `/steps/${s.id}/approve`)}>Approve</button>
                )}
                <button disabled={disabled || i === 0} title="Move up" onClick={() => act("POST", `/steps/${s.id}/move`, { direction: -1 })}>↑</button>
                <button disabled={disabled || i === steps.length - 1} title="Move down" onClick={() => act("POST", `/steps/${s.id}/move`, { direction: 1 })}>↓</button>
                <button disabled={disabled} onClick={() => act("DELETE", `/steps/${s.id}`)}>Remove step</button>
              </div>
              {isOpen && <StepReview step={s} out={out} view={view} act={act} disabled={disabled} onDone={() => setOpen(null)} />}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// ── Reviewing a step's output ────────────────────────────────────────────────

function isEdited(r) {
  return r.added_by_you || Object.values(r.prov || {}).some((p) => p.kind === "manual");
}

function attention(r, fields) {
  const why = [];
  if (r.excerpt_verified === false) why.push("excerpt not found in source");
  const unq = fields.filter((f) => r.prov?.[f.name]?.verified === false).map((f) => f.name);
  if (unq.length) why.push(`no confirmed quote: ${unq.join(", ")}`);
  if (r.conflicts?.length) why.push(`conflict: ${r.conflicts.map((c) => c.field).join(", ")}`);
  const blank = fields.filter((f) => f.required && !f.accept_blank && (r.values?.[f.name] == null || r.values[f.name] === "")).map((f) => f.name);
  if (blank.length) why.push(`required blank: ${blank.join(", ")}`);
  return why;
}

function asInput(v) {
  if (v == null) return "";
  if (typeof v === "boolean") return v ? "yes" : "no";
  return String(v);
}

function RecordEditor({ fields, values, onSave, onCancel, disabled, saveLabel = "Save" }) {
  const [draft, setDraft] = useState(() => Object.fromEntries(fields.map((f) => [f.name, asInput(values?.[f.name])])));
  const changed = fields.filter((f) => draft[f.name] !== asInput(values?.[f.name]));
  return (
    <div className="pl-editor">
      {fields.map((f) => (
        <label key={f.name} className={changed.includes(f) ? "changed" : ""}>
          <span><code>{f.name}</code>{f.required ? "*" : ""} <span className="assess">{f.type}</span></span>
          {f.type === "enum" || f.type === "boolean" ? (
            <select value={draft[f.name]} onChange={(e) => setDraft({ ...draft, [f.name]: e.target.value })}>
              <option value="">—</option>
              {(f.type === "enum" ? f.enum_values : ["yes", "no"]).map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          ) : f.name.includes("notes") || (draft[f.name] || "").length > 70 ? (
            <textarea rows={3} value={draft[f.name]} onChange={(e) => setDraft({ ...draft, [f.name]: e.target.value })} />
          ) : (
            <input value={draft[f.name]} placeholder={f.type === "money" ? "e.g. 50000 or $50K" : f.type === "date" ? "YYYY-MM-DD" : f.type === "percent" ? "e.g. 25%" : ""}
                   onChange={(e) => setDraft({ ...draft, [f.name]: e.target.value })} />
          )}
        </label>
      ))}
      <div className="btn-row">
        <button className="primary" disabled={disabled || changed.length === 0}
                onClick={() => onSave(Object.fromEntries(changed.map((f) => [f.name, draft[f.name]])))}>{saveLabel}</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

function StepReview({ step, out, view, act, disabled, onDone }) {
  const fields = view.definition.schema?.fields || [];
  const [onlyAttention, setOnlyAttention] = useState(false);
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState(null);     // record id, or "new"
  const records = out.records || [];
  const partial = records.length < out.count;
  const edit = (body) => act("POST", `/steps/${step.id}/records`, body);
  const shown = records.filter((r) =>
    (!onlyAttention || attention(r, fields).length || isEdited(r)) &&
    (!q.trim() || Object.values(r.values || {}).some((v) => String(v ?? "").toLowerCase().includes(q.trim().toLowerCase()))));
  const flagged = records.filter((r) => attention(r, fields).length).length;
  const n = editCount(step.edits);

  return (
    <div className="pl-review">
      {out.warnings?.length > 0 && <div className="pl-warn">{out.warnings.join(" · ")}</div>}
      {out.edit_notes?.length > 0 && <div className="pl-warn">{out.edit_notes.join(" · ")}</div>}
      {partial && <div className="banner err">Only the first {records.length} of {out.count} records are kept for review; add a filter step to narrow the output.</div>}
      <div className="pl-review-bar">
        <input placeholder="Search records" value={q} onChange={(e) => setQ(e.target.value)} />
        <label className="assess"><input type="checkbox" checked={onlyAttention} onChange={(e) => setOnlyAttention(e.target.checked)} />
          {" "}Only records needing attention ({flagged}) or edited</label>
        <span className="spacer" />
        <button disabled={disabled || partial || editing === "new"} onClick={() => setEditing("new")}>+ Add record</button>
      </div>
      {editing === "new" && (
        <div className="pl-detail pl-new">
          <b>New record</b> <span className="assess">for a programme the sources missed; its values are marked as entered by you.</span>
          <RecordEditor fields={fields} values={{}} disabled={disabled} saveLabel="Add record"
                        onSave={(values) => edit({ op: "add", values }).then((r) => r && setEditing(null))}
                        onCancel={() => setEditing(null)} />
        </div>
      )}
      <RecordsTable records={shown} fields={fields}
        rowNote={(r) => attention(r, fields).join("; ")}
        renderDetail={(r) => (
          <>
            {editing === r.id ? (
              <RecordEditor fields={fields} values={r.values} disabled={disabled}
                            onSave={(values) => edit({ op: "set", record_id: r.id, values }).then((x) => x && setEditing(null))}
                            onCancel={() => setEditing(null)} />
            ) : (
              <div className="btn-row" style={{ marginTop: 0 }}>
                <button disabled={disabled || partial} onClick={() => setEditing(r.id)}>Edit values</button>
                <button disabled={disabled || partial} onClick={() => edit({ op: "remove", record_id: r.id })}>Remove record</button>
                {isEdited(r) && !r.added_by_you && (
                  <button disabled={disabled} onClick={() => edit({ op: "undo", record_id: r.id })}>Undo my edits</button>
                )}
              </div>
            )}
            {attention(r, fields).length > 0 && <div className="pl-bad">Check: {attention(r, fields).join("; ")}</div>}
            {!r.added_by_you && <RecordDetail r={r} fields={fields} />}
          </>
        )} />
      {out.removed?.length > 0 && (
        <div className="pl-removed">
          <b>Removed by you ({out.removed.length})</b>
          <ul>{out.removed.map(({ record: r }) => (
            <li key={r.id}><span className="assess">{r.id}</span> {r.values?.[fields[0]?.name] || "(unnamed)"}
              <button className="link" disabled={disabled} onClick={() => edit({ op: "undo", record_id: r.id })}>restore</button></li>
          ))}</ul>
        </div>
      )}
      <div className="pl-review-foot">
        {step.status !== "approved" ? (
          <button className="primary" disabled={disabled}
                  onClick={() => act("POST", `/steps/${step.id}/approve`).then((r) => r && onDone())}>
            Approve {step.id}{n ? ` with your ${n} edit${n > 1 ? "s" : ""}` : ""}
          </button>
        ) : <span className="tag ok">approved</span>}
        <span className="assess">
          {out.count} record(s) · {flagged} need attention{n ? ` · ${n} edit(s) by you, kept on re-runs` : ""}
        </span>
      </div>
    </div>
  );
}

// ── Output preview ───────────────────────────────────────────────────────────

// Everything behind one record: where it came from, the quote behind each value
// (and whether it was found), what research read, contradictions and caveats.
function RecordDetail({ r, fields }) {
  const host = (u) => { try { return new URL(u).pathname.replace(/\/$/, "") || u; } catch { return u; } };
  return (
    <div className="pl-detail">
      <div><b>Source:</b> <a href={r.source_url} target="_blank" rel="noreferrer">{r.source_url}</a>
        {r.confidence != null && <span className="assess"> · confidence {Number(r.confidence).toFixed(2)}</span>}</div>
      {r.analyst_note && <div className="pl-note"><b>Analyst note:</b> {r.analyst_note}</div>}
      {r.conflicts?.length > 0 && (
        <div className="pl-conflicts">
          <b>Conflicts (values not changed):</b>
          <ul>{r.conflicts.map((c, i) => (
            <li key={i}><code>{c.field}</code> is “{short(c.current, 80)}”, but <a href={c.url} target="_blank" rel="noreferrer">{host(c.url)}</a> says {c.page_says}
              <blockquote>{c.quote}</blockquote></li>
          ))}</ul>
        </div>
      )}
      <div><b>Excerpt</b> {r.excerpt_verified ? <span className="tag ok">found in source</span> : <span className="tag gone">not found in source</span>}</div>
      <blockquote>{r.excerpt || "(none)"}</blockquote>
      {r.excerpt_unmatched?.length > 0 && (
        <div className="pl-bad">Not found in the source: {r.excerpt_unmatched.map((m, i) => <q key={i}>{m}</q>)}</div>
      )}
      <table className="pl-evidence">
        <thead><tr><th>Field</th><th>Value</th><th>Evidence</th></tr></thead>
        <tbody>
          {fields.map((f) => {
            const p = r.prov?.[f.name];
            const v = r.values?.[f.name];
            if (v == null || v === "") return null;
            return (
              <tr key={f.name}>
                <td><code>{f.name}</code></td>
                <td>{short(v, 120)}</td>
                <td>
                  <span className={"pl-cell " + (p?.kind || "")}>{KIND_TEXT[p?.kind] || p?.kind}</span>{" "}
                  {p?.quote && (p.verified ? <span className="tag ok">quote found</span> : <span className="tag gone">quote not found</span>)}
                  {p?.source_url && <> · <a href={p.source_url} target="_blank" rel="noreferrer">{host(p.source_url)}</a></>}
                  {p?.note && <div className="assess">{p.note}</div>}
                  {p?.was && <div className="assess">The step had: {p.was.value == null || p.was.value === "" ? "blank" : `“${short(p.was.value, 120)}”`}</div>}
                  {p?.quote && <blockquote>{p.quote}</blockquote>}
                  {p?.unmatched?.length > 0 && <div className="pl-bad">Not found: {p.unmatched.map((m, i) => <q key={i}>{m}</q>)}</div>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {r.research?.length > 0 && (
        <div className="assess"><b>Research read:</b>{" "}
          {r.research.map((x, i) => (
            <span key={i}>{i > 0 && " · "}<a href={x.url} target="_blank" rel="noreferrer">{host(x.url)}</a>
              {x.error ? " (unreadable)" : ` (${[x.filled?.length && `filled ${x.filled.join(", ")}`,
                x.replaced?.length && `improved ${x.replaced.join(", ")}`].filter(Boolean).join("; ") || "nothing new"})`}</span>
          ))}
        </div>
      )}
      {r.leads?.length > 0 && (
        <div className="assess"><b>Leads noted:</b> {r.leads.map((l) => `${(l.fields || []).join(", ")} → ${l.where}`).join(" · ")}</div>
      )}
    </div>
  );
}

// A table of records; clicking a row opens renderDetail(r) beneath it.
function RecordsTable({ records, fields, renderDetail, rowNote, rowTag }) {
  const [open, setOpen] = useState(null);
  return (
    <>
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
                  <tr onClick={() => setOpen(open === r.id ? null : r.id)} className={"pl-rec" + (open === r.id ? " open" : "")}>
                    <td className="assess" title={rowNote?.(r) || (r.excerpt_verified === false ? "Excerpt not found in the source" : "")}>
                      {open === r.id ? "▾ " : "▸ "}{r.id}
                      {r.excerpt_verified === false && <span className="pl-bad"> ⚠</span>}
                      {r.added_by_you && <span className="tag manual" style={{ marginLeft: ".3rem" }}>added</span>}
                      {rowTag?.(r)}
                    </td>
                    {fields.map((f) => {
                      const p = r.prov?.[f.name];
                      const v = r.values?.[f.name];
                      const blank = v == null || v === "";
                      return (
                        <td key={f.name} className={"pl-cell " + (blank ? (f.required ? "missing" : "") : p?.kind || "")}
                            title={p ? `${KIND_TEXT[p.kind] || p.kind} (${p.step})${p.note ? ": " + p.note : ""}${p.quote ? `\n“${p.quote}”` : ""}` : ""}>
                          {blank ? "—" : short(v, 60)}
                          {!blank && p?.verified === false && <span className="pl-bad" title="Quote not found in the source"> ⚠</span>}
                          {(r.conflicts || []).some((c) => c.field === f.name) && <span className="pl-bad" title="A source page contradicts this value"> ≠</span>}
                        </td>
                      );
                    })}
                  </tr>
                  {open === r.id && (
                    <tr className="pl-rec-detail">
                      <td colSpan={fields.length + 1}>
                        <div className="pl-detail-sticky">{renderDetail(r)}</div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function OutputCard({ view }) {
  const [focus, setFocus] = useState(false);
  const fields = view.definition.schema?.fields || [];
  const records = view.last_records || [];
  if (!records.length && !view.last_run) return null;
  const total = view.last_run?.coverage?.records ?? 0;
  const review = view.last_run?.review;
  const marks = review?.since ? review.marks || {} : {};
  const changedBy = Object.fromEntries((review?.changed || []).map((c) => [c.id, c.fields]));
  const shown = focus ? records.filter((r) => marks[r.id] || attention(r, fields).length) : records;
  return (
    <div className="card">
      <h4>
        {view.last_run?.upto ? `Output of the preview to ${view.last_run.upto}` : "Output of the last run"}{" "}
        <span className="assess">({total} record(s){records.length < total ? `, first ${records.length} shown` : ""})</span>
      </h4>
      <div className="muted-note" style={{ marginTop: 0, marginBottom: ".5rem" }}>
        What publishing would write. To correct records, review the step that produced them above.
      </div>
      <label className="assess"><input type="checkbox" checked={focus} onChange={(e) => setFocus(e.target.checked)} />
        {" "}Only {review?.since ? "new or changed records, and records " : "records "}needing attention</label>
      <RecordsTable records={shown} fields={fields}
        rowNote={(r) => attention(r, fields).join("; ")}
        rowTag={(r) => marks[r.id] && (
          <span className={"tag " + (marks[r.id] === "new" ? "ok" : "edited")} style={{ marginLeft: ".3rem" }}
                title={marks[r.id] === "changed" ? (changedBy[r.id] || []).map((d) => `${d.field}: ${short(d.old, 60) || "blank"} → ${short(d.new, 60) || "blank"}`).join("\n") : "Not in the last published version"}>
            {marks[r.id]}
          </span>
        )}
        renderDetail={(r) => (
          <>
            {changedBy[r.id] && (
              <div className="pl-note"><b>Changed since v{review.since.dataset_version}:</b>
                <ul>{changedBy[r.id].map((d) => <li key={d.field}><code>{d.field}</code>: {short(d.old, 100) || "blank"} → {short(d.new, 100) || "blank"}</li>)}</ul>
              </div>
            )}
            <RecordDetail r={r} fields={fields} />
          </>
        )} />
    </div>
  );
}

// What a full run changes since this pipeline last published, for the reviewer.
function ReviewSummary({ review, kindLabel }) {
  if (!review) return null;
  if (!review.since) return <div>{review.added.length} record(s), all new: this pipeline has not published before.</div>;
  return (
    <div className="pl-changes">
      <div>Since <b>{kindLabel} v{review.since.dataset_version}</b>{review.since.name ? ` “${review.since.name}”` : ""}, the last version this pipeline published:{" "}
        <b>{review.added.length}</b> new · <b>{review.removed.length}</b> gone · <b>{review.changed.length}</b> changed · <b>{review.unchanged}</b> unchanged</div>
      {review.added.length > 0 && <details><summary>New ({review.added.length})</summary><ul>{review.added.map((a) => <li key={a.id}>{a.name}</li>)}</ul></details>}
      {review.removed.length > 0 && <details open><summary>Gone ({review.removed.length}): publishing drops them from this pipeline's output, but the catalogue keeps them</summary>
        <ul>{review.removed.map((a, i) => <li key={i}>{a.name}</li>)}</ul></details>}
      {review.changed.length > 0 && <details><summary>Changed ({review.changed.length})</summary>
        <ul>{review.changed.map((c) => <li key={c.id}>{c.name}: {c.fields.map((d) => `${d.field} ${short(d.old, 40) || "blank"} → ${short(d.new, 40) || "blank"}`).join("; ")}</li>)}</ul></details>}
    </div>
  );
}

// ── Run and load ─────────────────────────────────────────────────────────────

function LoadCard({ view, act, disabled }) {
  const [name, setName] = useState("");
  const [by, setBy] = useState("");
  const [diff, setDiff] = useState(null);
  const [diffError, setDiffError] = useState(null);
  const review = view.last_run?.review;
  const published = review?.published;
  const blocked = view.gate.length > 0;
  const fields = view.definition.schema?.fields || [];
  const flagged = (view.last_records || []).filter((r) => attention(r, fields).length).length;

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
      <h4>Run, review and publish</h4>
      <div className="muted-note" style={{ marginTop: 0, marginBottom: ".5rem" }}>
        A run never publishes. It ends ready for review; nothing reaches the {view.kind_label.toLowerCase()} catalogue or
        the blocks until you approve it here.
      </div>
      <div className="btn-row" style={{ marginTop: 0 }}>
        <button className="primary" disabled={disabled} onClick={() => act("POST", "/run", {})}>Run pipeline</button>
        <button disabled={disabled} onClick={() => act("POST", "/run", { fresh: true })}
                title="Refetch every source and redo model steps instead of using cached results">Re-run fresh</button>
        {view.last_run && <span className="assess" style={{ alignSelf: "center" }}>
          Last run {(view.last_run.at || "").slice(0, 16).replace("T", " ")}{view.last_run.upto ? ` (preview to ${view.last_run.upto})` : ""}
        </span>}
      </div>
      {published ? (
        <div className="banner ok" style={{ marginTop: ".75rem" }}>
          Approved and published as <b>{view.kind_label} v{published.dataset_version}</b>{published.name ? ` “${published.name}”` : ""}
          {published.by ? ` by ${published.by}` : ""}{published.at ? ` on ${published.at.slice(0, 10)}` : ""}.
          Run again to pick up changes; the next run will wait here for review.
        </div>
      ) : blocked ? (
        <div className="pl-gate">
          <b>Not ready to publish:</b>
          <ul>{view.gate.map((g, i) => <li key={i}>{g}</li>)}</ul>
        </div>
      ) : (
        <div className="pl-ready">
          <div className="pl-ready-head"><span className="tag edited">ready for review</span>
            <span className="assess">run {(view.last_run?.at || "").slice(0, 16).replace("T", " ")}</span></div>
          <ReviewSummary review={review} kindLabel={view.kind_label} />
          {flagged > 0 && <div className="pl-warn">{flagged} record(s) need attention (unconfirmed quotes, conflicts or required blanks): check them in the output below.</div>}
          <div className="pl-add">
            <input placeholder="Version name (optional)" value={name} onChange={(e) => setName(e.target.value)} />
            <input placeholder="Your name (optional)" value={by} onChange={(e) => setBy(e.target.value)} />
          </div>
          <div className="btn-row">
            <button onClick={checkDiff} disabled={disabled} title="Compare with the whole catalogue, including programmes from other sources">Compare with the catalogue</button>
            <button className="primary" onClick={load} disabled={disabled}>Approve and publish as a new {view.kind_label} version</button>
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
        </div>
      )}
      {view.loads.length > 0 && (
        <>
          <h4 style={{ marginTop: "1rem" }}>Published versions</h4>
          <table>
            <thead><tr><th>Version</th><th>Name</th><th>Pipeline def</th><th>Records</th><th>Approved</th></tr></thead>
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
  const disabled = pending || !!view.busy;
  return (
    <div className="pl-panel-inner">
      <div className="pl-panel-head">
        <h3>{view.name}</h3>
        <span className="assess">{view.kind_label} · def v{view.definition_version}</span>
      </div>
      <SchemaCard view={view} act={act} disabled={disabled} />
      <SourcesCard view={view} act={act} disabled={disabled} />
      <StepsCard view={view} act={act} disabled={disabled} />
      <OutputCard view={view} />
      <LoadCard view={view} act={act} disabled={disabled} />
    </div>
  );
}
