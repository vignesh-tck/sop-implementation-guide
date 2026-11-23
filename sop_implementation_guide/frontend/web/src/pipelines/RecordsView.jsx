import { useState } from "react";
import { EvidencePane } from "./Drawer.jsx";
import { KIND_TEXT, RERUN_TYPES, cellState, isBlank, matchesFilter, recordName, recordStatus, short } from "./records.js";

// The records of the last run, one at a time: every field with its evidence and
// the step that produced it, the quote behind the selected value, and what to
// do about the record (accept it, flag it, re-run a step on it alone).

const FILTERS = [["all", "All"], ["review", "To review"], ["unquoted", "Unquoted"], ["flagged", "Flagged"]];
const EVIDENCE = { quoted: ["ok", "Quoted"], unquoted: ["bad", "No quote found"], conflict: ["bad", "Contradicted"], blank: ["warn", "Blank"] };

function RawExtract({ r }) {
  const host = (u) => { try { return new URL(u).host; } catch { return u; } };
  return (
    <div className="rv-raw">
      <div><b>Excerpt</b> {r.excerpt_verified ? <span className="tag ok">found in source</span> : <span className="tag gone">not found in source</span>}</div>
      <blockquote>{r.excerpt || "(none)"}</blockquote>
      {r.confidence != null && <div className="assess">Extraction confidence {Number(r.confidence).toFixed(2)}</div>}
      {r.analyst_note && <div className="pl-note"><b>Analyst note:</b> {r.analyst_note}</div>}
      {r.research?.length > 0 && (
        <div><b>Research read</b><ul>{r.research.map((x, i) => (
          <li key={i}><a href={x.url} target="_blank" rel="noreferrer">{host(x.url)}</a>
            <span className="assess"> {x.error ? "unreadable" : x.filled?.length ? `filled ${x.filled.join(", ")}` : "nothing new"}</span></li>
        ))}</ul></div>
      )}
      {r.leads?.length > 0 && <div className="assess"><b>Leads:</b> {r.leads.map((l) => `${(l.fields || []).join(", ")} → ${l.where}`).join(" · ")}</div>}
    </div>
  );
}

export default function RecordsView({ view, act, disabled, selected, onSelect, onShowOnCanvas, onAsk }) {
  const fields = view.definition.schema?.fields || [];
  const records = view.last_records || [];
  const marks = view.review_marks || {};
  const [q, setQ] = useState("");
  const [filt, setFilt] = useState("all");
  const [field, setField] = useState(null);
  const [side, setSide] = useState("source");
  const shown = records.filter((r) => matchesFilter(r, fields, marks[r.id], filt, q));
  const r = records.find((x) => x.id === selected) || shown[0] || records[0];
  const rerunnable = (view.definition.steps || []).filter((s) => RERUN_TYPES.includes(s.type));
  const [rerunPick, setRerunPick] = useState(null);

  if (!r) return <div className="rv-empty assess">No records yet: run the pipeline from the canvas.</div>;
  const mark = marks[r.id];
  const st = recordStatus(r, fields, mark);
  const accepted = new Set(mark?.accepted || []);
  const open = field && fields.some((f) => f.name === field) ? field : (fields.find((f) => cellState(r, f) !== "quoted") || fields[0])?.name;
  const producer = r.prov?.[open]?.step;
  const rerunStep = rerunPick && rerunnable.some((s) => s.id === rerunPick) ? rerunPick
    : rerunnable.find((s) => s.id === producer)?.id || rerunnable[0]?.id;
  const src = (view.definition.sources || []).find((s) => s.id === r.source_id);
  const allAccepted = fields.every((f) => accepted.has(f.name));

  const flag = () => {
    if (mark?.flag) return act("POST", `/records/${r.id}/flag`, { flag: false });
    const note = window.prompt("Flag this record for follow-up. Note (optional):", "");
    if (note !== null) act("POST", `/records/${r.id}/flag`, { flag: true, note });
  };
  return (
    <div className="rv">
      <aside className="rv-list">
        <input className="ws-search" placeholder="Search records" value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="rv-filters">{FILTERS.map(([k, t]) => (
          <button key={k} type="button" className={"ws-filter" + (filt === k ? " on" : "")} onClick={() => setFilt(k)}>{t}</button>
        ))}</div>
        <ul>
          {shown.map((x) => {
            const s = recordStatus(x, fields, marks[x.id]);
            return (
              <li key={x.id}>
                <button type="button" className={"rv-item" + (x.id === r.id ? " on" : "")} onClick={() => { onSelect(x.id); setField(null); }}>
                  <span className="rv-item-name">{recordName(x, fields)}</span>
                  <span className={"ws-status " + s.kind}>{s.text}</span>
                  <span className="assess rv-item-sub">{x.id}{x.source_id ? ` · ${x.source_id}` : ""}</span>
                </button>
              </li>
            );
          })}
          {shown.length === 0 && <li className="assess">No records match.</li>}
        </ul>
      </aside>
      <section className="rv-main">
        <div className="assess">{r.id} · from {src ? `${src.id} ${src.label}` : r.source_id || "a row you added"}</div>
        <h2 className="rv-title">{recordName(r, fields)} <span className={"ws-status " + st.kind}>{st.text}</span></h2>
        {mark?.flag?.note && <div className="pl-warn">Flagged: {mark.flag.note}</div>}
        <div className="rv-actions">
          <button disabled={disabled} onClick={flag}>{mark?.flag ? "Clear flag" : "⚑ Flag"}</button>
          {rerunnable.length > 0 && (
            <span className="rv-rerun">
              <button disabled={disabled || !rerunStep} onClick={() => act("POST", `/records/${r.id}/rerun`, { step_id: rerunStep })}
                      title="Runs this step again on this record alone, without the cache, then the steps after it">↻ Re-run</button>
              <select value={rerunStep || ""} onChange={(e) => setRerunPick(e.target.value)} disabled={disabled} aria-label="Step to re-run">
                {rerunnable.map((s) => <option key={s.id} value={s.id}>{s.id} {view.catalog[s.type]?.label}</option>)}
              </select>
              <span className="assess">for this record</span>
            </span>
          )}
          <button className="primary" disabled={disabled} onClick={() => act("POST", `/records/${r.id}/accept`, { accept: !allAccepted })}>
            {allAccepted ? "Reopen record" : "✓ Accept record"}</button>
        </div>
        <div className="table-wrap">
          <table className="rv-fields">
            <thead><tr><th>Field</th><th>Value</th><th>Evidence</th><th>Produced by</th></tr></thead>
            <tbody>
              {fields.map((f) => {
                const p = r.prov?.[f.name];
                const cs = cellState(r, f);
                const ev = accepted.has(f.name) ? ["ok", "Accepted"] : EVIDENCE[cs] || ["", KIND_TEXT[p?.kind] || p?.kind || ""];
                const step = (view.definition.steps || []).find((s) => s.id === p?.step);
                return (
                  <tr key={f.name} className={open === f.name ? "on" : ""} onClick={() => { setField(f.name); setSide("source"); }}>
                    <td><code>{f.name}</code></td>
                    <td>{isBlank(r.values?.[f.name]) ? <span className="ws-pill warn">blank</span> : short(r.values[f.name], 80)}</td>
                    <td><span className={"rv-ev " + ev[0]}>{ev[0] === "ok" ? "✓ " : ""}{ev[1]}</span></td>
                    <td className="assess">{p?.step ? `${p.step} ${view.catalog[step?.type]?.label || ""}` : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {r.rejected && Object.keys(r.rejected).length > 0 && (
          <div className="pl-rejected">
            <div className="assess">Rejected by Extract, which keeps only what the source states word for word:</div>
            <ul>
              {Object.entries(r.rejected).map(([name, x]) => (
                <li key={name}><code>{name}</code> = {short(x.value, 60)} <span className="assess">({x.why}{x.quote ? `; quote: “${short(x.quote, 120)}”` : ""})</span></li>
              ))}
            </ul>
          </div>
        )}
        <div className="rv-links">
          {producer && <button className="link" onClick={() => onShowOnCanvas(r.id, open, producer)}>Show {producer} on canvas ↗</button>}
          <button className="link" onClick={() => onAsk(`About record ${r.id} (${recordName(r, fields)}): `)}>Ask the agent about this record</button>
        </div>
      </section>
      <aside className="rv-side">
        <div className="ws-tabs" role="tablist">
          {[["source", "Source page"], ["raw", "Raw extract"]].map(([k, t]) => (
            <button key={k} type="button" role="tab" aria-selected={side === k} className={"ws-tab" + (side === k ? " on" : "")} onClick={() => setSide(k)}>{t}</button>
          ))}
        </div>
        <div className="rv-side-body">
          {side === "source" ? <EvidencePane key={`${r.id}:${open}`} view={view} act={act} disabled={disabled} r={r} field={open} final />
            : <RawExtract r={r} />}
        </div>
      </aside>
    </div>
  );
}
