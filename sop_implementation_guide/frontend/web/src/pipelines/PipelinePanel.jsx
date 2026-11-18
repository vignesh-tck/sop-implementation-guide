import { Fragment, useState } from "react";
import { call } from "../api.js";

const KIND_TEXT = {
  extracted: "read from the source",
  derived: "computed by a step",
  inferred: "model judgement (approved step)",
  assumed: "value you stated",
  manual: "your override",
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

// ── Steps: design each one (prompt, preview, approve), in order ──────────────

function StepsCard({ view, act, disabled }) {
  const steps = view.definition.steps || [];
  const firstOpen = steps.find((s) => s.status !== "approved")?.id ?? null;
  const [open, setOpen] = useState(firstOpen);
  return (
    <div className="card">
      <h4>Steps</h4>
      <div className="muted-note" style={{ marginTop: 0, marginBottom: ".5rem" }}>
        Design each step in order: read its prompt, tune the instructions, preview what it produces, then approve it.
        Changing a step sends it and every later step back for approval.
      </div>
      {steps.length === 0 && <div className="assess">The agent proposes steps once the schema and sources are in place.</div>}
      {steps.length > 0 && view.design_gaps?.length > 0 && (
        <div className="pl-gate" style={{ marginBottom: ".5rem" }}>
          <b>The pipeline is not complete:</b> no step produces {view.design_gaps.map((g, i) => <Fragment key={g}>{i > 0 && ", "}<code>{g}</code></Fragment>)}.
          Ask the agent in the chat to add the missing steps.
        </div>
      )}
      <ol className="pl-steps">
        {steps.map((s, i) => {
          const out = view.outputs[s.id];
          const cat = view.catalog[s.type] || {};
          const isOpen = open === s.id;
          const blockedBy = steps.slice(0, i).filter((x) => x.status !== "approved").map((x) => x.id);
          return (
            <li key={s.id} className={(isOpen ? "sel " : "") + (out?.error ? "failed" : "")}>
              <div className="pl-step-head pl-step-toggle" onClick={() => setOpen(isOpen ? null : s.id)}>
                <span className="assess">{isOpen ? "▾" : "▸"}</span>
                <b className="pl-id">{s.id}</b>
                <b>{cat.label || s.type}</b>
                {cat.model && <span className="tag derived" title="Calls the model; its prompt is shown when you open the step">model</span>}
                <StatusTag status={s.status} />
                <span className="spacer" />
                {out && !out.error && <span className="assess">{out.count} record(s)</span>}
              </div>
              {s.note && <div className="pl-step-note">{s.note}</div>}
              {!isOpen && out?.error && <div className="banner err" style={{ margin: ".4rem 0 0" }}>{out.error}</div>}
              {isOpen && (
                <StepDesigner step={s} out={out} view={view} act={act} disabled={disabled}
                              isLast={i === steps.length - 1} blockedBy={blockedBy}
                              onApproved={() => setOpen(steps[i + 1]?.id ?? null)} />
              )}
              {isOpen && (
                <div className="pl-step-actions">
                  <button disabled={disabled || i === 0} title="Move up" onClick={() => act("POST", `/steps/${s.id}/move`, { direction: -1 })}>↑</button>
                  <button disabled={disabled || i === steps.length - 1} title="Move down" onClick={() => act("POST", `/steps/${s.id}/move`, { direction: 1 })}>↓</button>
                  <button disabled={disabled} onClick={() => act("DELETE", `/steps/${s.id}`)}>Remove step</button>
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// The prompt a model step sends: fixed text (locked) around the planner's instructions (editable).
function PromptEditor({ step, prompt, act, disabled }) {
  const [draft, setDraft] = useState(null);       // null = not editing
  if (!prompt) return null;
  if (prompt.error) return <div className="banner err">{prompt.error}</div>;
  const value = draft ?? prompt.value;
  const editing = draft !== null;
  const save = () => act("PATCH", `/steps/${step.id}`, { config: { ...step.config, [prompt.key]: draft.trim() || null } })
    .then((r) => r && setDraft(null));
  const slot = (first) => (
    first ? (
      editing ? (
        <textarea className="pl-slot-edit" rows={Math.min(10, Math.max(3, value.split("\n").length + 1))} value={value}
                  placeholder={prompt.default} onChange={(e) => setDraft(e.target.value)} autoFocus />
      ) : (
        <span className="pl-slot" onClick={() => !disabled && setDraft(prompt.value)} title="Click to edit">
          {prompt.value || <i>{prompt.default}</i>}
        </span>
      )
    ) : <span className="pl-slot ref">{prompt.value || prompt.default} <i>(your instructions, as above)</i></span>
  );
  return (
    <div className="pl-prompt">
      <div className="pl-prompt-head">
        <b>Prompt</b>
        <span className="assess"><span className="pl-slot-key">highlighted</span> = your instructions, editable · grey = fixed rules (they keep every value quoted and checked)</span>
      </div>
      {prompt.prompts.map((pr, i) => (
        <details key={i} open={i === 0}>
          <summary>{pr.title}</summary>
          <pre className="pl-prompt-text">{pr.before}{slot(i === 0)}{pr.after}</pre>
        </details>
      ))}
      {prompt.fields && (
        <details>
          <summary>Fields requested ({prompt.fields.length}), sent as the answer format</summary>
          <ul className="pl-prompt-fields">{prompt.fields.map((f) => <li key={f.name}><code>{f.name}</code>: {f.description}</li>)}</ul>
          <div className="assess">These come from the target schema; ask in the chat to change a field's description.</div>
        </details>
      )}
      <div className="btn-row">
        {editing ? (
          <>
            <button className="primary" disabled={disabled || value === prompt.value} onClick={save}>Save instructions</button>
            <button onClick={() => setDraft(null)}>Cancel</button>
            {step.type === "geo_tie" && <button onClick={() => setDraft(prompt.default)}>Reset to default</button>}
          </>
        ) : (
          <button disabled={disabled} onClick={() => setDraft(prompt.value)}>Edit instructions</button>
        )}
      </div>
    </div>
  );
}

function StepDesigner({ step, out, view, act, disabled, isLast, blockedBy, onApproved }) {
  const fields = view.definition.schema?.fields || [];
  const [onlyAttention, setOnlyAttention] = useState(false);
  const prompt = view.prompts?.[step.id];
  const records = out?.records || [];
  const shown = onlyAttention ? records.filter((r) => attention(r, fields).length) : records;
  const flagged = records.filter((r) => attention(r, fields).length).length;
  const preview = () => act("POST", "/run", isLast ? {} : { upto: step.id });
  return (
    <div className="pl-review">
      {prompt ? <PromptEditor step={step} prompt={prompt} act={act} disabled={disabled} /> : (
        <div className="pl-settings">
          <b>Settings</b> <span className="assess">(no model call; ask in the chat to change them)</span>
          <pre className="pl-prompt-text">{JSON.stringify(step.config, null, 2)}</pre>
        </div>
      )}
      <div className="pl-output-head">
        <b>Output</b>
        <button disabled={disabled} className={!out ? "primary" : ""} onClick={preview}>
          {out ? "Preview again" : "Preview this step"}
        </button>
        {isLast && <span className="assess">previewing the last step runs the whole pipeline</span>}
      </div>
      {!out && <div className="assess">Not previewed with the current settings yet.</div>}
      {out?.error && <div className="banner err">{out.error}</div>}
      {out && !out.error && (
        <>
          {out.warnings?.length > 0 && <div className="pl-warn">{out.warnings.join(" · ")}</div>}
          {records.length < out.count && <div className="assess">First {records.length} of {out.count} records shown.</div>}
          <label className="assess"><input type="checkbox" checked={onlyAttention} onChange={(e) => setOnlyAttention(e.target.checked)} />
            {" "}Only records needing attention ({flagged})</label>
          <RecordsTable records={shown} fields={fields} rowNote={(r) => attention(r, fields).join("; ")}
                        renderDetail={(r) => (
                          <>
                            {attention(r, fields).length > 0 && <div className="pl-bad">Check: {attention(r, fields).join("; ")}</div>}
                            <RecordDetail r={r} fields={fields} />
                          </>
                        )} />
          <div className="muted-note">Not right? Tune the instructions above (or ask the agent) and preview again. Rows are corrected on the final output, once every step is approved.</div>
        </>
      )}
      <div className="pl-review-foot">
        {step.status === "approved" ? <span className="tag ok">approved</span> : (
          <button className="primary" disabled={disabled || !out || !!out?.error || blockedBy.length > 0}
                  title={blockedBy.length ? `Approve ${blockedBy.join(", ")} first` : !out ? "Preview it first" : ""}
                  onClick={() => act("POST", `/steps/${step.id}/approve`).then((r) => r && onApproved())}>
            Approve {step.id}
          </button>
        )}
        {blockedBy.length > 0 && step.status !== "approved" && <span className="assess">Approve {blockedBy.join(", ")} first: this step works on their output.</span>}
      </div>
    </div>
  );
}

// ── Final output: the planner's overrides ────────────────────────────────────

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

function overrideCount(o) {
  return o ? (o.changed?.length || 0) + (o.removed?.length || 0) + (o.added?.length || 0) : 0;
}

function OutputCard({ view, act, disabled }) {
  const fields = view.definition.schema?.fields || [];
  const [focus, setFocus] = useState(false);
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState(null);     // row id, or "new"
  const records = view.last_records || [];
  if (!view.last_run) return null;
  const locked = view.output_locked;
  const total = view.last_run?.coverage?.records ?? 0;
  const review = view.last_run?.review;
  const marks = review?.since ? review.marks || {} : {};
  const changedBy = Object.fromEntries((review?.changed || []).map((c) => [c.id, c.fields]));
  const edit = (body) => act("POST", "/output/records", body);
  const shown = records.filter((r) =>
    (!focus || marks[r.id] || attention(r, fields).length || isEdited(r)) &&
    (!q.trim() || Object.values(r.values || {}).some((v) => String(v ?? "").toLowerCase().includes(q.trim().toLowerCase()))));
  const n = overrideCount(view.overrides);
  const removed = view.last_run?.removed || [];

  return (
    <div className="card">
      <h4>
        {locked ? (view.last_run.upto ? `Output of the preview to ${view.last_run.upto}` : "Output of the last run") : "Final output"}{" "}
        <span className="assess">({total} record(s){records.length < total ? `, first ${records.length} shown` : ""})</span>
        {n > 0 && <span className="tag manual" style={{ marginLeft: ".4rem" }}>{n} override{n > 1 ? "s" : ""} by you</span>}
      </h4>
      {locked ? (
        <div className="muted-note" style={{ marginTop: 0 }}>
          Rows can be overridden here once the design is done. {locked}
        </div>
      ) : (
        <div className="muted-note" style={{ marginTop: 0 }}>
          Every step is approved. Correct values, remove rows or add rows the sources missed. Your overrides are kept and
          re-applied each time the pipeline runs; values you enter are marked as yours.
        </div>
      )}
      {view.last_run?.override_notes?.length > 0 && <div className="pl-warn">{view.last_run.override_notes.join(" · ")}</div>}
      <div className="pl-review-bar">
        <input placeholder="Search rows" value={q} onChange={(e) => setQ(e.target.value)} />
        <label className="assess"><input type="checkbox" checked={focus} onChange={(e) => setFocus(e.target.checked)} />
          {" "}Only rows needing attention{review?.since ? ", new or changed" : ""}{n ? ", or overridden" : ""}</label>
        <span className="spacer" />
        {!locked && <button disabled={disabled || editing === "new"} onClick={() => setEditing("new")}>+ Add row</button>}
      </div>
      {editing === "new" && (
        <div className="pl-detail pl-new">
          <b>New row</b> <span className="assess">for a programme the sources missed; its values are marked as entered by you.</span>
          <RecordEditor fields={fields} values={{}} disabled={disabled} saveLabel="Add row"
                        onSave={(values) => edit({ op: "add", values }).then((r) => r && setEditing(null))}
                        onCancel={() => setEditing(null)} />
        </div>
      )}
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
            {!locked && (editing === r.id ? (
              <RecordEditor fields={fields} values={r.values} disabled={disabled}
                            onSave={(values) => edit({ op: "set", record_id: r.id, values }).then((x) => x && setEditing(null))}
                            onCancel={() => setEditing(null)} />
            ) : (
              <div className="btn-row" style={{ marginTop: 0 }}>
                <button disabled={disabled} onClick={() => setEditing(r.id)}>Override values</button>
                <button disabled={disabled} onClick={() => edit({ op: "remove", record_id: r.id })}>Remove row</button>
                {isEdited(r) && !r.added_by_you && (
                  <button disabled={disabled} onClick={() => edit({ op: "undo", record_id: r.id })}>Undo my overrides</button>
                )}
              </div>
            ))}
            {changedBy[r.id] && (
              <div className="pl-note"><b>Changed since v{review.since.dataset_version}:</b>
                <ul>{changedBy[r.id].map((d) => <li key={d.field}><code>{d.field}</code>: {short(d.old, 100) || "blank"} → {short(d.new, 100) || "blank"}</li>)}</ul>
              </div>
            )}
            {attention(r, fields).length > 0 && <div className="pl-bad">Check: {attention(r, fields).join("; ")}</div>}
            {!r.added_by_you && <RecordDetail r={r} fields={fields} />}
          </>
        )} />
      {removed.length > 0 && (
        <div className="pl-removed">
          <b>Removed by you ({removed.length})</b>
          <ul>{removed.map(({ record: r }) => (
            <li key={r.id}><span className="assess">{r.id}</span> {r.values?.[fields[0]?.name] || "(unnamed)"}
              <button className="link" disabled={disabled || !!locked} onClick={() => edit({ op: "undo", record_id: r.id })}>restore</button></li>
          ))}</ul>
        </div>
      )}
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
                  {p?.was && <div className="assess">The pipeline produced: {p.was.value == null || p.was.value === "" ? "blank" : `“${short(p.was.value, 120)}”`}</div>}
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
      <OutputCard view={view} act={act} disabled={disabled} />
      <LoadCard view={view} act={act} disabled={disabled} />
    </div>
  );
}
