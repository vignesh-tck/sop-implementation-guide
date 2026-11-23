import { Fragment, useRef, useState } from "react";
import { call } from "../api.js";
import Stepper from "./Stepper.jsx";
import OutputTabs from "./OutputTabs.jsx";
import { isMulti, outputAct, outputList, outputNode, outputView } from "./ports.js";
import { AgentTab } from "./Drawer.jsx";
import { attention, isBlank, isEdited, short } from "./records.js";

// Review & publish: where a full run waits for the planner. The status card says
// whether it can be published and what to fix; the final output is an editable
// table whose column headers say where each column lands in the catalogue; the agent and the published
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
const PAGE_SIZE = 50;

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
      buttons: ids.map((id) => <button key={id} onClick={() => h.openNode(`step:${id}`)}>Review {id}</button>),
    };
  }
  if (text.includes("on the Output node")) {
    const col = (text.match(/`([^`]+)`/) || [])[1];
    return { text: col && col !== "program_name"
               ? <>Choose which column fills <code>{col}</code>: the ▾ under its column name in the table below.</>
               : <>Choose which column holds the programme name: the ▾ under its column name in the table below.</>,
             buttons: [<button key="m" onClick={h.showTable}>Show the table</button>] };
  }
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
  if (/schema/i.test(text)) return { buttons: [<button key="g" onClick={() => h.openNode(h.outputNode || "output")}>Open the schema</button>] };
  if (/source|step/i.test(text)) return { buttons: [<button key="g" onClick={() => h.openNode(null)}>Open the pipeline</button>] };
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

function StatusCard({ view, state, handlers }) {
  const last = view.last_run;
  const review = last?.review;
  const published = review?.published;
  const kind = view.kind_label;
  const steps = view.definition.steps || [];
  const records = last?.coverage?.records ?? 0;
  const required = (view.load_map?.columns || []).filter((c) => c.required);
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
    items.push(<Check key="b" ok>{view.write ? <>Written to <code>{view.write.table}</code> in Supabase</>
      : "Blocks stay on the version they were analysed with until they’re re-run"}</Check>);
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
      items.push(<Check key="m" ok>{required.map((c) => (view.kind === "funding"
        ? <Fragment key={c.column}><code>{c.field}</code> is the programme name (<code>{c.column}</code>)</Fragment>
        : <Fragment key={c.column}><code>{c.field}</code> fills <code>{c.column}</code>{" "}</Fragment>))}</Check>);
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
          : view.write ? `A run never publishes. Nothing is written to ${view.write.table} until you approve it here.`
          : `A run never publishes. Nothing reaches the ${kind.toLowerCase()} ${view.kind === "zoning" ? "data" : "catalogue"} or the blocks until you approve it here.`}
      </div>
      {state === "ready" && review?.since && <ChangeList review={review} kind={kind} />}
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

// ── Where a value came from ──────────────────────────────────────────────────

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

// ── The final output as an editable table ────────────────────────────────────
//
// Click a cell to edit it (Enter saves, Esc cancels); the blank row at the
// bottom adds one; the bin removes one. Each save is an override, kept and
// re-applied on every run. The picker under each column name is where that
// column lands in the catalogue. The strip under the table says where the
// selected value came from.

const NEW_ROW = "__new";

function asInput(v) {
  if (v == null) return "";
  if (typeof v === "boolean") return v ? "yes" : "no";
  return String(v);
}

function CellInput({ f, value, onSave, onCancel }) {
  const start = asInput(value);
  const [v, setV] = useState(start);
  const sent = useRef(false);
  const done = (x = v) => {
    if (sent.current) return;
    sent.current = true;
    if (x === start) onCancel(); else onSave(x);
  };
  const keys = (e) => {
    if (e.key === "Escape") { e.preventDefault(); sent.current = true; onCancel(); }
    else if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); done(); }
  };
  if (f.type === "enum" || f.type === "boolean") {
    return (
      <select className="pub-input" autoFocus value={v} onKeyDown={keys} onBlur={() => done()}
              onChange={(e) => { setV(e.target.value); done(e.target.value); }}>
        <option value="">—</option>
        {(f.type === "enum" ? f.enum_values : ["yes", "no"]).map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  }
  if (start.length > 60) {
    return <textarea className="pub-input" autoFocus rows={4} value={v} onKeyDown={keys} onBlur={() => done()}
                     onChange={(e) => setV(e.target.value)} />;
  }
  return <input className="pub-input" autoFocus value={v} onKeyDown={keys} onBlur={() => done()} onChange={(e) => setV(e.target.value)}
                type={f.type === "date" ? "date" : "text"}
                placeholder={f.type === "money" ? "e.g. 50000 or $50K" : f.type === "percent" ? "e.g. 25%" : ""} />;
}

// Where a column lands in the catalogue: a catalogue column, or extra detail
// (kept with the programme; Block Funding reads it too).
function MapPicker({ view, act, disabled, f }) {
  const lm = view.load_map;
  if (!lm) return null;
  const mine = lm.columns.find((c) => c.field === f.name);
  const fits = lm.columns.filter((c) => !c.types || c.types.includes(f.type));
  async function pick(col) {
    const mapping = {};
    if (mine && mine.column !== col) mapping[mine.column] = null;
    if (col) mapping[col] = f.name;
    if (Object.keys(mapping).length) await act("PUT", "/load-map", { mapping });
  }
  return (
    <select className={"pub-map-pick" + (mine ? " on" : "") + (mine?.auto ? " auto" : "")} value={mine?.column || ""} disabled={disabled}
            aria-label={`Where ${f.name} goes in the catalogue`} onClick={(e) => e.stopPropagation()}
            title={mine ? `${mine.use}${mine.auto ? " (matched from the name; pick another to change it)" : ""}`
              : "Kept with each programme as extra detail; Block Funding reads it too"}
            onChange={(e) => pick(e.target.value)}>
      <option value="">extra detail</option>
      {fits.map((c) => (
        <option key={c.column} value={c.column}>→ {c.column}{c.field && c.field !== f.name ? ` (now ${c.field})` : ""}</option>
      ))}
    </select>
  );
}

function needsLook(r, f) {
  const v = r.values?.[f.name];
  if (isBlank(v)) return f.required && !f.accept_blank;
  return r.prov?.[f.name]?.verified === false || (r.conflicts || []).some((c) => c.field === f.name);
}

function Strip({ view, act, disabled, locked, r, f, onAsk, onClose }) {
  const p = r.prov?.[f.name];
  const v = r.values?.[f.name];
  const kind = isBlank(v) ? "missing" : p?.kind;
  const undo = !locked && p?.kind === "manual" && p.was && !r.added_by_you;
  const why = attention(r, [f]);
  return (
    <div className="pub-strip" role="region" aria-label="Where this value came from">
      <div className="pub-strip-head">
        <code>{r.id}</code><span>·</span><b>{f.name}</b>
        {kind && <span className={"pub-kind " + kind}>{KIND_LABEL[kind] || kind}</span>}
        <span className="spacer" />
        {undo && (
          <button className="link" disabled={disabled}
                  onClick={() => act("POST", "/output/records", { op: "set", record_id: r.id, values: { [f.name]: p.was.value } })}>Undo my change</button>
        )}
        <button className="link" onClick={() => onAsk(r)}>Ask the agent</button>
        <button className="link" aria-label="Close" onClick={onClose}>✕</button>
      </div>
      <div className="pub-strip-body"><Explain view={view} r={r} f={f} /></div>
      {why.length > 0 && <div className="pl-warn">Check: {why.join("; ")}</div>}
      {!r.added_by_you && r.source_url && (
        <div className="assess">
          Row from {r.source_id} · <a href={r.source_url} target="_blank" rel="noreferrer">{host(r.source_url)}</a>
          {r.excerpt && <> · excerpt {r.excerpt_verified ? "found in the source" : <span className="pl-bad">not found in the source</span>}</>}
          {r.analyst_note && <> · note: {r.analyst_note}</>}
        </div>
      )}
    </div>
  );
}

function OutputCard({ view, act, disabled, filter, setFilter, onAsk }) {
  const fields = view.definition.schema?.fields || [];
  const [q, setQ] = useState("");
  const [page, setPage] = useState(0);
  const [sel, setSel] = useState(null);          // { id, field }
  const [editing, setEditing] = useState(null);  // { id, field }
  const [saving, setSaving] = useState({});      // "id|field" -> { v }: shown at once, until the server answers
  const queue = useRef(Promise.resolve());
  const records = view.last_records || [];
  const last = view.last_run;
  if (!last) return null;
  const locked = view.output_locked;
  const total = last.coverage?.records ?? records.length;
  const review = last.review;
  const marks = review?.since ? review.marks || {} : {};
  const removed = last.removed || [];
  // One save at a time: each reads and rewrites the overrides on the server, so
  // two at once could lose one. A save takes a few seconds (several database
  // round trips), so the cell shows the new value while it is on its way.
  const edit = (body) => {
    const run = queue.current.then(() => act("POST", "/output/records", body));
    queue.current = run.catch(() => null);
    return run;
  };
  const noName = (view.load_map?.columns || []).filter((c) => c.required && !c.field);

  const needs = (r) => attention(r, fields).length > 0 || marks[r.id] === "changed";
  const shown = records.filter((r) =>
    (filter === "all" || (filter === "attention" ? needs(r) : isEdited(r))) &&
    (!q.trim() || Object.values(r.values || {}).some((v) => String(v ?? "").toLowerCase().includes(q.trim().toLowerCase()))));
  const pages = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  const pg = Math.min(page, pages - 1);
  const slice = shown.slice(pg * PAGE_SIZE, (pg + 1) * PAGE_SIZE);
  const showNew = !locked && filter === "all" && !q.trim() && pg === pages - 1;

  async function save(r, f, value) {
    const key = `${r.id}|${f.name}`;
    const mark = { v: value };
    setEditing(null);
    setSaving((s) => ({ ...s, [key]: mark }));
    await (r.id === NEW_ROW
      ? edit({ op: "add", values: { [f.name]: value } })
      : edit({ op: "set", record_id: r.id, values: { [f.name]: value } }));
    // On failure the cell goes back to the saved value; the error shows above the page.
    setSaving((s) => {
      if (s[key] !== mark) return s;   // a newer save of this cell is still on its way
      const next = { ...s };
      delete next[key];
      return next;
    });
  }
  function open(r, f) {
    setSel(r.id === NEW_ROW ? null : { id: r.id, field: f.name });
    if (!locked) setEditing({ id: r.id, field: f.name });
  }
  const selRec = sel && records.find((r) => r.id === sel.id);
  const selField = sel && fields.find((f) => f.name === sel.field);

  const row = (r) => {
    const fresh = r.id === NEW_ROW;
    return (
      <tr key={r.id} className={"pub-row" + (fresh ? " fresh" : "")}>
        <td className="pub-rowid">
          {fresh ? <span className="assess">+ new</span> : <>
            {r.id}
            {marks[r.id] && <span className={"pub-pill " + (marks[r.id] === "new" ? "new" : "chg")}>{marks[r.id]}</span>}
            {!locked && (
              <button className="pub-bin" title="Remove this row" aria-label={`Remove ${r.id}`} disabled={disabled}
                      onClick={() => edit({ op: "remove", record_id: r.id })}>🗑</button>
            )}
          </>}
        </td>
        {fields.map((f) => {
          const v = r.values?.[f.name];
          const p = r.prov?.[f.name];
          const isEd = editing?.id === r.id && editing.field === f.name;
          const isSel = sel?.id === r.id && sel.field === f.name;
          const pend = saving[`${r.id}|${f.name}`];
          const cls = ["pub-cell", (pend || (p?.kind === "manual" && !isBlank(v))) && "edited", !pend && !fresh && needsLook(r, f) && "warn",
            isSel && "sel", isEd && "editing", locked && "ro"].filter(Boolean).join(" ");
          return (
            <td key={f.name} className={cls} onClick={() => !isEd && open(r, f)} title={isBlank(v) ? "" : short(v, 400)}>
              {isEd ? <CellInput f={f} value={pend ? pend.v : v} onSave={(x) => save(r, f, x)} onCancel={() => setEditing(null)} />
                : pend ? <span className="pub-saving" title="Saving…">{short(pend.v, 80) || "blank"}</span>
                : isBlank(v) ? <span className="pub-blank">{fresh ? "" : f.required && !f.accept_blank ? "blank" : "—"}</span>
                : short(fmtValue(f, v), 80)}
            </td>
          );
        })}
      </tr>
    );
  };

  return (
    <div className="card pub-output" id="pub-output">
      <div className="pub-output-head">
        <span className="pub-output-title">{locked && last.upto ? `Preview to ${last.upto}` : "Final output"}</span>
        <span className="pub-count">{total} rows</span>
        <span className="spacer" />
        <input className="pub-search" placeholder="Search rows" value={q} onChange={(e) => { setQ(e.target.value); setPage(0); }} />
        <div className="ws-seg" role="tablist" aria-label="Rows">
          {[["all", "All"], ["attention", "Needs a look"], ["edited", "Edited by you"]].map(([k, t]) => (
            <button key={k} type="button" role="tab" aria-selected={filter === k} className={filter === k ? "on" : ""}
                    onClick={() => { setFilter(k); setPage(0); }}>{t}</button>
          ))}
        </div>
      </div>
      <div className="pub-output-note">
        {locked ? <span className="assess">Read only for now. {locked}</span> : <>
          <span>Click a cell to change it. Enter saves, Esc cancels. Your changes are kept every time the pipeline re-runs.</span>
          <span className="pub-key edited">edited by you</span><span className="pub-key warn">needs a look</span>
        </>}
      </div>
      {view.load_map && (
        <div className="assess pub-map-note">
          The ▾ under each column name is where it goes in the {view.kind_label.toLowerCase()} catalogue. It’s matched from the
          name; change any. “Extra detail” columns still go with the programme, and Block Funding reads them.
        </div>
      )}
      {noName.length > 0 && (
        <div className="pl-warn">Pick the column that holds the programme name: choose <b>→ {noName[0].column}</b> under its name.</div>
      )}
      {last.override_notes?.length > 0 && <div className="pl-warn">{last.override_notes.join(" · ")}</div>}
      <div className="table-wrap pub-table-wrap">
        <table className="pub-table">
          <thead>
            <tr>
              <th className="pub-rowid-h">row</th>
              {fields.map((f) => (
                <th key={f.name}>
                  <div className="pub-th-name">{f.name}{f.required ? "*" : ""}</div>
                  <MapPicker view={view} act={act} disabled={disabled} f={f} />
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {slice.map(row)}
            {showNew && row({ id: NEW_ROW, values: {}, prov: {} })}
          </tbody>
        </table>
        {slice.length === 0 && !showNew && <div className="assess pub-empty">{records.length ? "No rows match." : "No records."}</div>}
      </div>
      {selRec && selField && (
        <Strip view={view} act={act} disabled={disabled} locked={locked} r={selRec} f={selField} onAsk={onAsk} onClose={() => setSel(null)} />
      )}
      <div className="pub-pager">
        <span className="assess">
          {shown.length ? `Showing ${pg * PAGE_SIZE + 1}–${Math.min((pg + 1) * PAGE_SIZE, shown.length)} of ${shown.length}` : ""}
          {records.length < total ? ` (the first ${records.length} of ${total} are kept for review)` : ""}
        </span>
        {removed.length > 0 && (
          <span className="assess pub-removed">Removed by you:{" "}
            {removed.map(({ record: r }, i) => (
              <Fragment key={r.id}>{i > 0 && ", "}{short(r.values?.[fields[0]?.name], 40) || r.id}{" "}
                <button className="link" disabled={disabled || !!locked} onClick={() => edit({ op: "undo", record_id: r.id })}>restore</button></Fragment>
            ))}
          </span>
        )}
        {pages > 1 && (
          <span className="pub-pager-btns">
            <button disabled={pg === 0} onClick={() => setPage(pg - 1)}>‹ Prev</button>
            <button disabled={pg >= pages - 1} onClick={() => setPage(pg + 1)}>Next ›</button>
          </span>
        )}
      </div>
    </div>
  );
}

// ── Publish ──────────────────────────────────────────────────────────────────

// What a zoning publish would change against the zoning data in force.
function ZoningDiff({ diff }) {
  const ut = diff.use_table || {};
  const rd = diff.readings || { added: [], changed: [], removed: [] };
  return (
    <div className="pl-diff">
      {diff.problems?.length > 0 && (
        <div className="banner err">Publishing is refused until these are fixed:
          <ul>{diff.problems.map((p, i) => <li key={i}>{p}</li>)}</ul></div>
      )}
      <div>Against the zoning data in force{diff.base_version ? ` (Zoning v${diff.base_version.version})` : ""}:</div>
      {diff.outputs?.includes("use_permissions") && (
        <div className="assess">Use table: <b>{ut.cells_changed_count || 0}</b> cell(s) changed{ut.uses_added?.length ? `, ${ut.uses_added.length} use(s) added` : ""}
          {ut.old_amended_through !== ut.new_amended_through ? ` · current through ${ut.old_amended_through} → ${ut.new_amended_through}` : ` · current through ${ut.new_amended_through}`}</div>
      )}
      {diff.outputs?.includes("readings") && (
        <div className="assess">Readings: <b>{rd.added.length}</b> new · <b>{rd.changed.length}</b> changed · <b>{rd.removed.length}</b> removed
          {rd.added.length > 0 && ` (${rd.added.slice(0, 6).map((a) => `${a.rec_label}${a.governed_by === "not_a_land_use" ? ": not a land use" : ""}`).join("; ")}${rd.added.length > 6 ? " …" : ""})`}</div>
      )}
      {diff.outputs?.includes("block_zones") && (
        <div className="assess">Blocks: <b>{diff.blocks_looked_up}</b> looked up · <b>{diff.blocks_changed?.length || 0}</b> changed
          {diff.lookup_failures?.length ? ` · ${diff.lookup_failures.length} failed and keep their zones` : ""}</div>
      )}
      <div className="assess">Findings: <b>{diff.outcome_flips?.length || 0}</b> recommendation × zone outcome(s) change ·
        {" "}<b>{diff.score_changes?.length || 0}</b> block score(s) change</div>
      {diff.score_changes?.length > 0 && <div className="assess">{diff.score_changes.slice(0, 8).map((s) => `Block ${s.id}: ${s.old ?? "—"} → ${s.new ?? "—"}`).join("; ")}</div>}
      <div className="assess">{diff.note}</div>
    </div>
  );
}

// What a general publish would do to each output's table.
function GeneralDiff({ diff }) {
  return (
    <div className="pl-diff">
      {diff.problems?.length > 0 && (
        <div className="banner err">Publishing is refused until these are fixed:
          <ul>{diff.problems.map((p, i) => <li key={i}>{p}</li>)}</ul></div>
      )}
      {diff.tables.map((t) => (
        <div key={t.output}>
          <div><code>{t.table}</code> ({t.exists ? `${t.rows_now} row(s) now` : "new table"}, {t.mode} by {t.key.join(" + ")}):{" "}
            <b>{t.added_count}</b> new · <b>{t.changed_count}</b> changed · <b>{t.unchanged}</b> unchanged
            {t.mode === "replace" ? <> · <b>{t.removed_count}</b> removed</> : null}{t.kept > 0 ? ` · ${t.kept} other row(s) kept` : ""}</div>
          {t.exists && t.new_columns.length > 0 && <div className="assess">New columns: {t.new_columns.join(", ")}</div>}
          {t.changed.length > 0 && <div className="assess">Changed: {t.changed.slice(0, 8).map((c) => `${c.key} (${c.fields.join(", ")})`).join("; ")}{t.changed_count > 8 ? " …" : ""}</div>}
          {t.removed.length > 0 && <div className="assess">Removed: {t.removed.slice(0, 8).join("; ")}{t.removed_count > 8 ? " …" : ""}</div>}
        </div>
      ))}
      <div className="assess">{diff.note}</div>
    </div>
  );
}

function PublishDialog({ view, act, disabled, onClose }) {
  const kind = view.kind_label;
  const zoningKind = view.kind === "zoning";
  const generalKind = view.kind === "general";
  const tables = outputList(view).map((o) => (isMulti(view) ? o.write : view.write)?.table).filter(Boolean);
  const slug = (view.name || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
  const [name, setName] = useState(`${slug || "pipeline"}_v${(view.loads?.length || 0) + 1}`);
  const [by, setBy] = useState(() => pref("pl.publisher", ""));
  const [diff, setDiff] = useState(null);
  const [diffError, setDiffError] = useState(null);
  const lasts = outputList(view).map((o) => o.last_run);
  const records = lasts.reduce((n, l) => n + (l?.coverage?.records ?? 0), 0);
  const review = isMulti(view) ? null : view.last_run?.review;

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
          <div>Publishes <b>{records} records</b>{review ? ` (${changeText(review)})` : ""}{isMulti(view) ? ` from ${outputList(view).map((o) => o.name).join(", ")}` : ""}
            {generalKind ? <> to {tables.map((t, i) => <Fragment key={t}>{i > 0 && ", "}<code>{t}</code></Fragment>)} in Supabase,
              and keeps a copy as a new, numbered version.</>
              : <>{" "}into the {kind.toLowerCase()} {zoningKind ? "data in force" : "catalogue"} as a new, numbered version.
                Blocks stay on the version they were analysed with until they’re re-run.</>}</div>
          <label><span>Version name</span><input value={name} onChange={(e) => setName(e.target.value)} /></label>
          <label><span>Approved by</span><input value={by} placeholder="Your name" onChange={(e) => setBy(e.target.value)} /></label>
          <button className="link" onClick={compare} disabled={disabled}>Compare with {generalKind ? "what the table holds now" : `the whole ${zoningKind ? "zoning data" : "catalogue"}`} first</button>
          {diffError && <div className="banner err">{diffError}</div>}
          {diff && zoningKind && <ZoningDiff diff={diff} />}
          {diff && generalKind && <GeneralDiff diff={diff} />}
          {diff && !zoningKind && !generalKind && (
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
          <div className="assess">{l.coverage?.records ?? (l.coverage && typeof l.coverage === "object"
            ? Object.values(l.coverage).reduce((n, c) => n + (c?.records || 0), 0) : "?")} records · pipeline def v{l.definition_version}</div>
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

export default function ReviewPage({ view, act, pending, nav, onBack, error, onClearError, onOpenNode }) {
  const disabled = pending || !!view.busy;
  const [side, setSideState] = useState(() => pref("pl.review.side", true));
  const [tab, setTab] = useState("agent");
  const [compose, setCompose] = useState(null);
  const [ctx, setCtx] = useState(null);
  const [filter, setFilter] = useState("all");
  const [publishing, setPublishing] = useState(false);
  const setSide = (o) => { setSideState(o); keep("pl.review.side", o); };

  // With several outputs the page reviews one output at a time (its tab), but runs
  // and publishes them together: the header, the chip and Approve & publish are the pipeline's.
  const multi = isMulti(view);
  const [oid, setOid] = useState("output");
  const ov = outputView(view, oid);
  const oact = outputAct(act, oid);
  const lasts = outputList(view).map((o) => o.last_run);
  const published = lasts[0]?.review?.published;
  const state = lasts.some((l) => !l) ? "none" : lasts.every((l) => l.review?.published) ? "published" : view.gate.length ? "blocked" : "ready";
  const ostate = !ov.last_run ? "none" : ov.last_run.review?.published ? "published" : ov.gate.length ? "blocked" : "ready";
  const records = lasts.reduce((n, l) => n + (l?.coverage?.records ?? 0), 0);
  const run = (fresh) => act("POST", "/run", fresh ? { fresh: true } : {});

  const chip = view.busy ? ["", view.busy === "run" ? "Running…" : "Agent working…"]
    : { none: ["", "Not run yet"], published: ["ok", `Published · ${view.kind_label} v${published?.dataset_version}`],
        blocked: ["warn", "Not ready to publish"], ready: ["ready", "Ready for review"] }[state];

  const handlers = {
    act: oact, disabled, openNode: onOpenNode, run: () => run(false), outputNode: outputNode(oid),
    showTable: () => document.getElementById("pub-output")?.scrollIntoView({ behavior: "smooth" }),
    showAttention: () => { setFilter("attention"); document.getElementById("pub-output")?.scrollIntoView({ behavior: "smooth" }); },
  };
  const ask = (r) => {
    const name = r.values?.[ov.definition.schema?.fields?.[0]?.name];
    setCtx(`${r.id}${name ? ` ${short(name, 50)}` : ""}`);
    setSide(true); setTab("agent"); setCompose({ text: "", at: Date.now() });
  };

  return (
    <div className="pl-ws">
      <div className="ws-head">
        <button className="link" onClick={onBack}>‹ Pipelines</button>
        <b className="ws-name">{view.name}</b>
        <span className="ws-kind">{view.kind_label} · def v{view.definition_version}</span>
        <Stepper view={view} page={nav.page} reached={nav.reached} onGo={nav.go} onStop={() => act("POST", "/stop")} />
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
            {multi && <OutputTabs view={view} value={oid} onChange={(o) => { setOid(o); setFilter("all"); }} />}
            <StatusCard view={ov} state={multi ? ostate : state} handlers={handlers} />
            <OutputCard key={oid} view={ov} act={oact} disabled={disabled} filter={filter} setFilter={setFilter} onAsk={ask} />
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
                     compose={compose} ctx={ctx} onClearCtx={() => setCtx(null)} onCanvas={() => nav.go(0)} />
        ) : (
          <button type="button" className="pub-side-open" onClick={() => setSide(true)} title="Show the agent and history">Agent ⇤</button>
        )}
      </div>
      {publishing && <PublishDialog view={view} act={act} disabled={disabled} onClose={() => setPublishing(false)} />}
    </div>
  );
}
