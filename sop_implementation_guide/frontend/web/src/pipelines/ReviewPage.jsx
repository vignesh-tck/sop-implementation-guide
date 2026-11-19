import { Fragment, useState } from "react";
import { call } from "../api.js";
import Stepper from "./Stepper.jsx";
import LoadMap from "./LoadMap.jsx";
import { AgentTab } from "./Drawer.jsx";
import { attention, isBlank, isEdited, short } from "./records.js";

// Review & publish: where a full run waits for the planner. The status card says
// whether it can be published and what to fix; the final output shows why each
// value is there and takes the planner's overrides; the agent and the published
// versions sit on the right. A run never publishes: only Approve & publish does.

function pref(key, fallback) {
  try { const v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; }
}
function keep(key, v) {
  try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* storage blocked: the setting lasts this visit */ }
}

const LEGEND = [
  ["extracted", "read from the source"],
  ["derived", "computed by a step"],
  ["inferred", "model judgement"],
  ["assumed", "value you stated"],
  ["manual", "your override"],
  ["missing", "blank"],
];
const KIND_LABEL = Object.fromEntries(LEGEND);
const PAGE_SIZE = 25;

const when = (at) => (at || "").slice(0, 16).replace("T", " ");
const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u || ""; } };

// Gate texts quote names in backticks; show them as code.
function Ticks({ text }) {
  return (text || "").split(/(`[^`]+`)/).map((part, i) =>
    part.startsWith("`") && part.endsWith("`") ? <code key={i}>{part.slice(1, -1)}</code> : part);
}

function fmtValue(f, v) {
  if (isBlank(v)) return "";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (f?.type === "money" && typeof v === "number") return "$" + v.toLocaleString();
  if (f?.type === "percent" && typeof v === "number") return `${+(v * 100).toFixed(2)}%`;
  return short(v, 400);
}

function stepLabel(view, id) {
  const s = (view.definition.steps || []).find((x) => x.id === id);
  return s ? `${id} ${view.catalog[s.type]?.label || s.type}` : id;
}

// ── Status: ready, not ready (with what to fix), or published ────────────────

function gateActions(text, view, h) {
  const m = text.match(/^Steps not approved: (.+)\.$/);
  if (m) {
    const ids = m[1].split(/,\s*/);
    return {
      text: <>Steps not approved: <b>{ids.map((id) => stepLabel(view, id)).join(", ")}</b></>,
      buttons: ids.map((id) => <button key={id} onClick={() => h.reviewStep(id)}>Review {id}</button>),
    };
  }
  if (text.includes("on the Output node")) return { buttons: [<button key="m" onClick={h.openMap}>Map a field</button>] };
  const req = text.match(/^Required field `([^`]+)` is blank/);
  if (req) {
    return {
      buttons: [<button key="s" onClick={h.showAttention}>Show the rows</button>,
        <button key="a" disabled={h.disabled} onClick={() => h.act("POST", `/fields/${req[1]}/accept-blank`, { accept: true })}>Accept blanks</button>],
    };
  }
  if (/record\(s\) have no `/.test(text)) return { buttons: [<button key="s" onClick={h.showAttention}>Show the rows</button>] };
  if (/not been run|run it again|preview up to|had errors|produced no records/.test(text)) {
    return { buttons: [<button key="r" disabled={h.disabled} onClick={h.run}>Run pipeline</button>] };
  }
  if (/schema/i.test(text)) return { buttons: [<button key="g" onClick={() => h.go(0)}>Open the schema</button>] };
  if (/source/i.test(text)) return { buttons: [<button key="g" onClick={() => h.go(1)}>Open the sources</button>] };
  if (/step/i.test(text)) return { buttons: [<button key="g" onClick={() => h.go(2)}>Open the pipeline</button>] };
  return { buttons: [] };
}

function Check({ ok, children, buttons }) {
  return (
    <li className={"pub-check " + (ok ? "ok" : "warn")}>
      <span className="pub-check-mark" aria-hidden="true">{ok ? "✓" : "!"}</span>
      <div className="pub-check-body">
        <div>{children}</div>
        {buttons?.length > 0 && <div className="pub-check-actions">{buttons}</div>}
      </div>
    </li>
  );
}

function changeText(review) {
  if (!review) return "";
  if (!review.since) return "all new";
  const parts = [review.added.length && `${review.added.length} new`, review.changed.length && `${review.changed.length} changed`,
    review.removed.length && `${review.removed.length} gone`].filter(Boolean);
  return (parts.join(", ") || "no changes") + ` since v${review.since.dataset_version}`;
}

function StatusCard({ view, act, disabled, state, mapOpen, setMapOpen, handlers }) {
  const last = view.last_run;
  const review = last?.review;
  const published = review?.published;
  const kind = view.kind_label;
  const steps = view.definition.steps || [];
  const records = last?.coverage?.records ?? 0;
  const required = (view.load_map?.columns || []).filter((c) => c.required);
  const mapped = view.load_map?.columns.filter((c) => c.field) || [];
  const overrides = view.overrides ? ["changed", "removed", "added"].reduce((n, k) => n + (view.overrides[k]?.length || 0), 0) : 0;

  let icon = "✓", title, sub;
  if (state === "published") {
    title = `Published as ${kind} v${published.dataset_version}`;
    sub = [published.name && `“${published.name}”`, `approved${published.by ? ` by ${published.by}` : ""}${published.at ? ` on ${published.at.slice(0, 10)}` : ""}`,
      `${records} records`].filter(Boolean).join(" · ");
  } else if (state === "none") {
    icon = "▷"; title = "Not run yet";
    sub = "Run the pipeline to produce the final output. The run stops here and waits for your review.";
  } else {
    if (state === "blocked") icon = "!";
    title = state === "ready" ? "Ready for review" : "Not ready to publish";
    const reused = last.reused?.length ? `, started after ${last.reused[last.reused.length - 1]} and reused its saved output` : "";
    sub = `Run ${when(last.at)}${last.upto ? ` · preview up to ${last.upto}` : ""} · ${records} records${review ? `, ${changeText(review)}` : ""}${reused}`
      + (review && !review.since ? " · nothing from this pipeline is published yet" : "");
  }

  const items = [];
  if (state === "published") {
    items.push(<Check key="b" ok>Blocks stay on the version they were analysed with until they’re re-run</Check>);
    if (overrides) items.push(<Check key="o" ok>Your {overrides} override{overrides > 1 ? "s are" : " is"} kept and re-applied on every run</Check>);
  } else if (state !== "none") {
    if (state === "blocked") {
      for (const g of view.gate) {
        const a = gateActions(g, view, handlers);
        items.push(<Check key={g} buttons={a.buttons}>{a.text || <Ticks text={g} />}</Check>);
      }
    }
    if (steps.length && steps.every((s) => s.status === "approved")) items.push(<Check key="s" ok>All steps approved</Check>);
    if (required.length && required.every((c) => c.field)) {
      items.push(<Check key="m" ok>{required.map((c) => <Fragment key={c.column}><code>{c.field}</code> is mapped to <code>{c.column}</code></Fragment>)} on Output</Check>);
    }
    if (records) items.push(<Check key="r" ok>{records} records {state === "ready" ? `ready · ${changeText(review)}` : "produced"}</Check>);
  }

  return (
    <div className="card pub-status">
      <div className="pub-status-head">
        <span className={"pub-status-icon " + state} aria-hidden="true">{icon}</span>
        <div>
          <div className="pub-status-title">{title}</div>
          <div className="assess">{sub}</div>
        </div>
      </div>
      {items.length > 0 && <ul className="pub-checks">{items}</ul>}
      <div className="pub-status-note">
        {state === "published" ? "Run again to pick up changes. The next run stops here and waits for your review."
          : state === "blocked" ? "Fix the items above, then run again. Approve unlocks once every check passes."
          : `A run never publishes. Nothing reaches the ${kind.toLowerCase()} catalogue or the blocks until you approve it here.`}
      </div>
      {state === "ready" && review?.since && <ChangeList review={review} kind={kind} />}
      {view.load_map && (
        <div className="pub-map">
          <span className="pub-map-label">Maps onto the {kind.toLowerCase()} catalogue</span>
          {mapped.length === 0 && <span className="assess">nothing mapped yet</span>}
          {mapped.map((c) => <span key={c.column} className="pub-map-chip">{c.field} → <b>{c.column}</b></span>)}
          <button className="link" onClick={() => setMapOpen(!mapOpen)}>{mapOpen ? "Done" : "Edit mapping"}</button>
          {mapOpen && <div className="pub-map-edit"><LoadMap view={view} act={act} disabled={disabled} /></div>}
        </div>
      )}
    </div>
  );
}

function ChangeList({ review, kind }) {
  const { added, removed, changed } = review;
  if (!added.length && !removed.length && !changed.length) return null;
  return (
    <details className="pub-changes">
      <summary>What changed since {kind} v{review.since.dataset_version}{review.since.name ? ` “${review.since.name}”` : ""}</summary>
      {added.length > 0 && <div><b>New ({added.length}):</b> {added.map((a) => a.name).join("; ")}</div>}
      {changed.length > 0 && (
        <div><b>Changed ({changed.length}):</b>
          <ul>{changed.map((c) => <li key={c.id}>{c.name}: {c.fields.map((d) => `${d.field} ${short(d.old, 40) || "blank"} → ${short(d.new, 40) || "blank"}`).join("; ")}</li>)}</ul>
        </div>
      )}
      {removed.length > 0 && <div><b>Gone ({removed.length}):</b> {removed.map((a) => a.name).join("; ")}
        <div className="assess">Publishing drops them from this pipeline’s output; the catalogue keeps them.</div></div>}
    </details>
  );
}

// ── Editing values ───────────────────────────────────────────────────────────

function asInput(v) {
  if (v == null) return "";
  if (typeof v === "boolean") return v ? "yes" : "no";
  return String(v);
}

function FieldInput({ f, value, onChange, autoFocus }) {
  if (f.type === "enum" || f.type === "boolean") {
    return (
      <select value={value} autoFocus={autoFocus} onChange={(e) => onChange(e.target.value)}>
        <option value="">—</option>
        {(f.type === "enum" ? f.enum_values : ["yes", "no"]).map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  }
  if (f.name.includes("notes") || f.name.includes("details") || value.length > 70) {
    return <textarea rows={3} value={value} autoFocus={autoFocus} onChange={(e) => onChange(e.target.value)} />;
  }
  return <input value={value} autoFocus={autoFocus} onChange={(e) => onChange(e.target.value)}
                placeholder={f.type === "money" ? "e.g. 50000 or $50K" : f.type === "date" ? "YYYY-MM-DD" : f.type === "percent" ? "e.g. 25%" : ""} />;
}

function OneField({ f, value, disabled, onSave, onCancel }) {
  const [v, setV] = useState(asInput(value));
  return (
    <div className="pub-edit-one">
      <FieldInput f={f} value={v} onChange={setV} autoFocus />
      <button className="primary" disabled={disabled || v === asInput(value)} onClick={() => onSave(v)}>Save</button>
      <button onClick={onCancel}>Cancel</button>
    </div>
  );
}

function NewRow({ fields, disabled, onSave, onCancel }) {
  const [draft, setDraft] = useState(() => Object.fromEntries(fields.map((f) => [f.name, ""])));
  const filled = fields.filter((f) => draft[f.name].trim());
  return (
    <div className="pub-newrow">
      <div><b>New row</b> <span className="assess">for a programme the sources missed. Its values are marked as yours.</span></div>
      <div className="pub-newrow-grid">
        {fields.map((f) => (
          <label key={f.name}>
            <span><code>{f.name}</code>{f.required ? "*" : ""} <span className="assess">{f.type}</span></span>
            <FieldInput f={f} value={draft[f.name]} onChange={(v) => setDraft({ ...draft, [f.name]: v })} />
          </label>
        ))}
      </div>
      <div className="btn-row">
        <button className="primary" disabled={disabled || !filled.length}
                onClick={() => onSave(Object.fromEntries(filled.map((f) => [f.name, draft[f.name]])))}>Add row</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

// ── Why each value is here ───────────────────────────────────────────────────

function Explain({ view, r, f }) {
  const p = r.prov?.[f.name];
  const v = r.values?.[f.name];
  if (isBlank(v)) {
    if (r.added_by_you) return <>Not entered</>;
    return <>{r.research?.length ? "No step found a value for it on the linked pages" : "No step filled it"}
      {f.required && !f.accept_blank && <span className="pl-bad"> · required</span>}</>;
  }
  if (!p) return null;
  const conflicts = (r.conflicts || []).filter((c) => c.field === f.name);
  let main;
  if (p.kind === "manual") {
    main = r.added_by_you || !p.was ? "You entered this"
      : <>You changed this from the pipeline’s value <b>{isBlank(p.was.value) ? "blank" : fmtValue(f, p.was.value)}</b></>;
  } else if (p.kind === "extracted") {
    const url = p.source_url || r.source_url;
    main = <>{p.step} · quoted from {url ? <a href={url} target="_blank" rel="noreferrer">{host(url)}</a> : "the source"}</>;
  } else {
    main = <>{stepLabel(view, p.step)}{p.note ? ` · ${p.note}` : ""}</>;
  }
  return (
    <>
      {main}
      {p.quote && (
        <blockquote className={"pub-quote" + (p.verified === false ? " bad" : "")} title={p.quote}>
          {p.verified === false && <b>Quote not found in the source: </b>}{p.quote}
        </blockquote>
      )}
      {conflicts.map((c, i) => (
        <div key={i} className="pl-bad">Contradicted: <a href={c.url} target="_blank" rel="noreferrer">{host(c.url)}</a> says {c.page_says}</div>
      ))}
    </>
  );
}

function WhyPanel({ view, act, disabled, locked, r, fields, review, onAsk }) {
  const [editing, setEditing] = useState(null);
  const edit = (body) => act("POST", "/output/records", body);
  const save = async (name, value) => { if (await edit({ op: "set", record_id: r.id, values: { [name]: value } })) setEditing(null); };
  const changed = (review?.changed || []).find((c) => c.id === r.id);
  const why = attention(r, fields);

  return (
    <div className="pub-why">
      <div className="pub-why-head">
        <b>Why each value is here</b>
        {r.source_url && !r.added_by_you && (
          <span className="assess">from {r.source_id} · <a href={r.source_url} target="_blank" rel="noreferrer">{host(r.source_url)}</a>
            {r.confidence != null && ` · confidence ${Number(r.confidence).toFixed(2)}`}</span>
        )}
        <span className="spacer" />
        <button className="link" onClick={() => onAsk(r)}>💬 Ask the agent</button>
        {!locked && isEdited(r) && !r.added_by_you && (
          <button className="link" disabled={disabled} onClick={() => edit({ op: "undo", record_id: r.id })}>Undo all my overrides</button>
        )}
        {!locked && <button className="link danger" disabled={disabled} onClick={() => edit({ op: "remove", record_id: r.id })}>Remove row</button>}
      </div>
      {changed && (
        <div className="pl-note"><b>Changed since v{review.since.dataset_version}:</b>{" "}
          {changed.fields.map((d) => `${d.field} ${short(d.old, 60) || "blank"} → ${short(d.new, 60) || "blank"}`).join("; ")}</div>
      )}
      {why.length > 0 && <div className="pl-warn">Check: {why.join("; ")}</div>}
      {r.analyst_note && <div className="pl-note"><b>Analyst note:</b> {r.analyst_note}</div>}
      <div className="pub-why-rows">
        {fields.map((f) => {
          const p = r.prov?.[f.name];
          const v = r.values?.[f.name];
          const blank = isBlank(v);
          const kind = blank ? "missing" : p?.kind || "";
          return (
            <div key={f.name} className="pub-why-row">
              <code className="pub-why-field">{f.name}</code>
              <div className={"pub-why-val" + (blank ? " blank" : "")}>
                {editing === f.name
                  ? <OneField f={f} value={v} disabled={disabled} onSave={(x) => save(f.name, x)} onCancel={() => setEditing(null)} />
                  : blank ? "blank" : fmtValue(f, v)}
              </div>
              <div>{kind && <span className={"pub-kind " + kind}>{KIND_LABEL[kind] || kind}</span>}</div>
              <div className="pub-why-explain"><Explain view={view} r={r} f={f} /></div>
              <div className="pub-why-act">
                {!locked && editing !== f.name && (
                  p?.kind === "manual" && p.was && !r.added_by_you
                    ? <button className="link" disabled={disabled} onClick={() => edit({ op: "set", record_id: r.id, values: { [f.name]: p.was.value } })}>Undo override</button>
                    : <button className="link" disabled={disabled} onClick={() => setEditing(f.name)}>{blank ? "Enter value" : "Override"}</button>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {!r.added_by_you && (r.excerpt || r.research?.length > 0 || r.leads?.length > 0) && (
        <details className="pub-more">
          <summary>Source excerpt and research</summary>
          {r.excerpt && (
            <>
              <div>Excerpt {r.excerpt_verified ? <span className="tag ok">found in source</span> : <span className="tag gone">not found in source</span>}</div>
              <blockquote>{r.excerpt}</blockquote>
              {r.excerpt_unmatched?.length > 0 && <div className="pl-bad">Not found in the source: {r.excerpt_unmatched.map((m, i) => <q key={i}>{m}</q>)}</div>}
            </>
          )}
          {r.research?.length > 0 && (
            <div className="assess"><b>Research read:</b>{" "}
              {r.research.map((x, i) => (
                <span key={i}>{i > 0 && " · "}<a href={x.url} target="_blank" rel="noreferrer">{host(x.url)}</a>
                  {x.error ? " (unreadable)" : ` (${[x.filled?.length && `filled ${x.filled.join(", ")}`,
                    x.replaced?.length && `improved ${x.replaced.join(", ")}`].filter(Boolean).join("; ") || "nothing new"})`}</span>
              ))}
            </div>
          )}
          {r.leads?.length > 0 && <div className="assess"><b>Leads noted:</b> {r.leads.map((l) => `${(l.fields || []).join(", ")} → ${l.where}`).join(" · ")}</div>}
        </details>
      )}
    </div>
  );
}

// ── Final output ─────────────────────────────────────────────────────────────

function OutputCard({ view, act, disabled, filter, setFilter, onAsk }) {
  const fields = view.definition.schema?.fields || [];
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(null);
  const [adding, setAdding] = useState(false);
  const [page, setPage] = useState(0);
  const records = view.last_records || [];
  const last = view.last_run;
  if (!last) return null;
  const locked = view.output_locked;
  const total = last.coverage?.records ?? records.length;
  const review = last.review;
  const marks = review?.since ? review.marks || {} : {};
  const removed = last.removed || [];
  const edit = (body) => act("POST", "/output/records", body);

  const needs = (r) => attention(r, fields).length > 0 || marks[r.id] === "changed";
  const shown = records.filter((r) => (filter !== "attention" || needs(r)) &&
    (!q.trim() || Object.values(r.values || {}).some((v) => String(v ?? "").toLowerCase().includes(q.trim().toLowerCase()))));
  const pages = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  const pg = Math.min(page, pages - 1);
  const slice = shown.slice(pg * PAGE_SIZE, (pg + 1) * PAGE_SIZE);
  const nNeeds = records.filter(needs).length;

  return (
    <div className="card pub-output" id="pub-output">
      <div className="pub-output-head">
        <span className="pub-output-title">{locked && last.upto ? `Preview to ${last.upto}` : "Final output"}</span>
        <span className="pub-count">{total} records</span>
        <span className="spacer" />
        <input className="pub-search" placeholder="Search rows" value={q} onChange={(e) => { setQ(e.target.value); setPage(0); }} />
        <div className="ws-seg" role="tablist" aria-label="Rows">
          {[["all", "All"], ["attention", `Needs attention${nNeeds ? ` · ${nNeeds}` : ""}`]].map(([k, t]) => (
            <button key={k} type="button" role="tab" aria-selected={filter === k} className={filter === k ? "on" : ""}
                    onClick={() => { setFilter(k); setPage(0); }}>{t}</button>
          ))}
        </div>
        {!locked && <button disabled={disabled || adding} onClick={() => setAdding(true)}>+ Add row</button>}
      </div>
      <div className="assess pub-output-note">
        {locked ? `Rows can be overridden once the design is done. ${locked}`
          : "Correct values, remove rows or add rows the sources missed. Your overrides are kept and re-applied every run."}
      </div>
      {last.override_notes?.length > 0 && <div className="pl-warn">{last.override_notes.join(" · ")}</div>}
      <div className="pub-legend">{LEGEND.map(([k, t]) => <span key={k} className={"pub-kind " + k}>{t}</span>)}</div>
      {adding && (
        <NewRow fields={fields} disabled={disabled} onCancel={() => setAdding(false)}
                onSave={async (values) => { if (await edit({ op: "add", values })) setAdding(false); }} />
      )}
      {slice.length === 0 ? <div className="assess pub-empty">{records.length ? "No rows match." : "No records."}</div> : (
        <div className="table-wrap pub-table-wrap">
          <table className="pub-table">
            <thead><tr><th>row</th>{fields.map((f) => <th key={f.name}>{f.name}{f.required ? "*" : ""}</th>)}</tr></thead>
            <tbody>
              {slice.map((r) => {
                const isOpen = open === r.id;
                return (
                  <Fragment key={r.id}>
                    <tr className={"pub-row" + (isOpen ? " open" : "")} onClick={() => setOpen(isOpen ? null : r.id)}>
                      <td className="pub-rowid">
                        <span className="pub-chev" aria-hidden="true">{isOpen ? "⌄" : "›"}</span>{r.id}
                        {r.added_by_you && <span className="pub-pill manual">added</span>}
                        {marks[r.id] && <span className={"pub-pill " + (marks[r.id] === "new" ? "new" : "chg")}>{marks[r.id]}</span>}
                        {attention(r, fields).length > 0 && <span className="pl-bad" title={attention(r, fields).join("; ")}> ⚠</span>}
                      </td>
                      {fields.map((f) => {
                        const v = r.values?.[f.name];
                        const p = r.prov?.[f.name];
                        const blank = isBlank(v);
                        const conflict = (r.conflicts || []).some((c) => c.field === f.name);
                        return (
                          <td key={f.name} className={"pub-cell " + (blank ? "missing" + (f.required && !f.accept_blank ? " req" : "") : p?.kind || "")}
                              title={blank ? "" : short(v, 400)}>
                            {blank ? "blank" : short(fmtValue(f, v), 60)}
                            {!blank && p?.kind === "manual" && <span className="pub-pill manual">yours</span>}
                            {!blank && p?.verified === false && <span className="pl-bad" title="Quote not found in the source"> ⚠</span>}
                            {conflict && <span className="pl-bad" title="A source page contradicts this value"> ≠</span>}
                          </td>
                        );
                      })}
                    </tr>
                    {isOpen && (
                      <tr className="pub-detail-row">
                        <td colSpan={fields.length + 1}>
                          <WhyPanel view={view} act={act} disabled={disabled} locked={locked} r={r} fields={fields} review={review} onAsk={onAsk} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="pub-pager">
        <span className="assess">
          {shown.length ? `Showing ${pg * PAGE_SIZE + 1}–${Math.min((pg + 1) * PAGE_SIZE, shown.length)} of ${shown.length}` : ""}
          {records.length < total ? ` (the first ${records.length} of ${total} are kept for review)` : ""}
        </span>
        {pages > 1 && (
          <span className="pub-pager-btns">
            <button disabled={pg === 0} onClick={() => setPage(pg - 1)}>‹ Prev</button>
            <button disabled={pg >= pages - 1} onClick={() => setPage(pg + 1)}>Next ›</button>
          </span>
        )}
      </div>
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

// ── Publish ──────────────────────────────────────────────────────────────────

function PublishDialog({ view, act, disabled, onClose }) {
  const kind = view.kind_label;
  const slug = (view.name || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
  const [name, setName] = useState(`${slug || "pipeline"}_v${(view.loads?.length || 0) + 1}`);
  const [by, setBy] = useState(() => pref("pl.publisher", ""));
  const [diff, setDiff] = useState(null);
  const [diffError, setDiffError] = useState(null);
  const records = view.last_run?.coverage?.records ?? 0;
  const review = view.last_run?.review;

  async function compare() {
    setDiffError(null);
    try { setDiff(await call("GET", `/pipelines/${view.id}/diff`)); } catch (e) { setDiffError(e.message); }
  }
  async function publish() {
    keep("pl.publisher", by.trim());
    if (await act("POST", "/load", { name: name.trim() || null, created_by: by.trim() || null })) onClose();
  }

  return (
    <div className="pl-modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="pl-modal narrow" role="dialog" aria-modal="true" aria-label="Approve and publish">
        <div className="pl-modal-head"><b>Approve & publish as {kind} v{view.next_dataset_version}</b><span className="spacer" />
          <button className="link" onClick={onClose} aria-label="Close">✕</button></div>
        <div className="pub-dialog-body">
          <div>Publishes <b>{records} records</b>{review ? ` (${changeText(review)})` : ""} into the {kind.toLowerCase()} catalogue as a new, numbered version.
            Blocks stay on the version they were analysed with until they’re re-run.</div>
          <label><span>Version name</span><input value={name} onChange={(e) => setName(e.target.value)} /></label>
          <label><span>Approved by</span><input value={by} placeholder="Your name" onChange={(e) => setBy(e.target.value)} /></label>
          <button className="link" onClick={compare} disabled={disabled}>Compare with the whole catalogue first</button>
          {diffError && <div className="banner err">{diffError}</div>}
          {diff && (
            <div className="pl-diff">
              <div>Against {diff.base_version ? `${kind} v${diff.base_version.version}` : "an empty catalogue"} ({diff.catalogue_size} programme(s)):{" "}
                <b>{diff.added.length}</b> new · <b>{diff.changed.length}</b> changed · <b>{diff.unchanged}</b> unchanged</div>
              {diff.added.length > 0 && <div className="assess">New: {diff.added.map((a) => a.program_name).join("; ")}</div>}
              {diff.changed.length > 0 && <div className="assess">Changed: {diff.changed.map((c) => `${c.program_name} (${c.fields.join(", ")})`).join("; ")}</div>}
              <div className="assess">{diff.note}</div>
            </div>
          )}
          <div className="btn-row">
            <button className="primary" disabled={disabled} onClick={publish}>✓ Approve & publish</button>
            <button onClick={onClose}>Cancel</button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Agent and history ────────────────────────────────────────────────────────

function History({ view }) {
  const kind = view.kind_label;
  if (!view.loads?.length) return <div className="assess pub-history-empty">Nothing from this pipeline is published yet.</div>;
  return (
    <ul className="pub-history">
      {view.loads.map((l) => (
        <li key={l.id}>
          <div><b>{kind} v{l.dataset_version}</b>{l.name ? ` “${l.name}”` : ""}</div>
          <div className="assess">{l.coverage?.records ?? "?"} records · pipeline def v{l.definition_version}</div>
          <div className="assess">approved {when(l.created_at)}{l.created_by ? ` by ${l.created_by}` : ""}</div>
        </li>
      ))}
    </ul>
  );
}

function SidePanel({ view, act, disabled, tab, setTab, onCollapse, compose, ctx, onClearCtx, onCanvas }) {
  return (
    <aside className="pub-side">
      <div className="pub-side-tabs" role="tablist">
        {[["agent", "Agent"], ["history", "History"]].map(([k, t]) => (
          <button key={k} type="button" role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{t}</button>
        ))}
        <span className="spacer" />
        <button type="button" className="link" title="Hide the panel" aria-label="Hide the panel" onClick={onCollapse}>⇥</button>
      </div>
      <div className="pub-side-body">
        {tab === "agent" ? (
          <AgentTab view={view} act={act} disabled={disabled} compose={compose} context={ctx} contextLabel={ctx || ""}
                    onClearContext={onClearCtx} onFocus={onCanvas} onShowProposal={() => {}} />
        ) : <History view={view} />}
      </div>
    </aside>
  );
}

// ── The page ─────────────────────────────────────────────────────────────────

export default function ReviewPage({ view, act, pending, nav, onBack, error, onClearError, onReviewStep }) {
  const disabled = pending || !!view.busy;
  const [side, setSideState] = useState(() => pref("pl.review.side", true));
  const [tab, setTab] = useState("agent");
  const [compose, setCompose] = useState(null);
  const [ctx, setCtx] = useState(null);
  const [filter, setFilter] = useState("all");
  const [mapOpen, setMapOpen] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const setSide = (o) => { setSideState(o); keep("pl.review.side", o); };

  const last = view.last_run;
  const published = last?.review?.published;
  const state = !last ? "none" : published ? "published" : view.gate.length ? "blocked" : "ready";
  const records = last?.coverage?.records ?? 0;
  const run = (fresh) => act("POST", "/run", fresh ? { fresh: true } : {});

  const chip = view.busy ? ["", view.busy === "run" ? "Running…" : "Agent working…"]
    : { none: ["", "Not run yet"], published: ["ok", `Published · ${view.kind_label} v${published?.dataset_version}`],
        blocked: ["warn", "Not ready to publish"], ready: ["ready", "Ready for review"] }[state];

  const handlers = {
    act, disabled, go: nav.go, reviewStep: onReviewStep, run: () => run(false),
    openMap: () => setMapOpen(true),
    showAttention: () => { setFilter("attention"); document.getElementById("pub-output")?.scrollIntoView({ behavior: "smooth" }); },
  };
  const ask = (r) => {
    const name = r.values?.[view.definition.schema?.fields?.[0]?.name];
    setCtx(`${r.id}${name ? ` ${short(name, 50)}` : ""}`);
    setSide(true); setTab("agent"); setCompose({ text: "", at: Date.now() });
  };

  return (
    <div className="pl-ws">
      <div className="ws-head">
        <button className="link" onClick={onBack}>‹ Pipelines</button>
        <b className="ws-name">{view.name}</b>
        <span className="ws-kind">{view.kind_label} · def v{view.definition_version}</span>
        <Stepper view={view} page={nav.page} reached={nav.reached} onGo={nav.go} />
      </div>
      <div className="ws-bar">
        <span className={"ws-chip pub-chip " + chip[0]}>{chip[1]}</span>
        <button disabled={disabled} onClick={() => run(true)} title="Refetch every source and redo model steps instead of using saved results">↻ Re-run fresh</button>
        <button className={state === "published" || state === "none" ? "primary" : ""} disabled={disabled} onClick={() => run(false)}>Run pipeline</button>
        {state !== "published" && (
          <button className="primary" disabled={disabled || state !== "ready"} onClick={() => setPublishing(true)}
                  title={state === "ready" ? `Publish as ${view.kind_label} v${view.next_dataset_version}` : "Fix what the status card lists first"}>✓ Approve & publish</button>
        )}
      </div>
      {error && <div className="banner err ws-error" onClick={onClearError} role="alert">{error}</div>}
      <div className={"pub-body" + (side ? " with-side" : "")}>
        <div className="pub-main">
          <div className="pub-scroll">
            <StatusCard view={view} act={act} disabled={disabled} state={state} mapOpen={mapOpen} setMapOpen={setMapOpen} handlers={handlers} />
            <OutputCard view={view} act={act} disabled={disabled} filter={filter} setFilter={setFilter} onAsk={ask} />
          </div>
          {state === "ready" && (
            <div className="pub-publish-bar">
              <span>Approving publishes <b>{records} records</b> as <b>{view.kind_label} v{view.next_dataset_version}</b>.</span>
              <button className="primary" disabled={disabled} onClick={() => setPublishing(true)}>✓ Approve & publish</button>
            </div>
          )}
        </div>
        {side ? (
          <SidePanel view={view} act={act} disabled={disabled} tab={tab} setTab={setTab} onCollapse={() => setSide(false)}
                     compose={compose} ctx={ctx} onClearCtx={() => setCtx(null)} onCanvas={() => nav.go(2)} />
        ) : (
          <button type="button" className="pub-side-open" onClick={() => setSide(true)} title="Show the agent and history">Agent ⇤</button>
        )}
      </div>
      {publishing && <PublishDialog view={view} act={act} disabled={disabled} onClose={() => setPublishing(false)} />}
    </div>
  );
}
