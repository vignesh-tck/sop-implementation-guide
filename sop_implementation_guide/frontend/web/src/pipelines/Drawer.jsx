import { useEffect, useRef, useState } from "react";
import { uploadFiles } from "../api.js";
import { Message } from "./ChatPane.jsx";
import { KIND_TEXT, cellState, isBlank, matchesFilter, recordName, recordStatus, short } from "./records.js";

// Under the canvas: the agent (which proposes, never applies), the records the
// last run produced with the evidence behind each value, and what Check found.

// ── Agent ────────────────────────────────────────────────────────────────────

function ProposalCard({ view, act, disabled, onFocus, onShow }) {
  const p = view.proposal;
  if (!p) return null;
  const nodes = p.changes.map((c) => c.node).filter(Boolean);
  return (
    <div className="ws-prop">
      <div className="ws-prop-head"><b>Proposed changes</b><span className="ws-count">{p.changes.length}</span>
        <span className="assess">by the agent</span></div>
      {p.stale && <div className="pl-warn">You changed the steps after the agent proposed this, so it can't be added. Discard it and ask again.</div>}
      {p.changes.length === 0 ? <div className="assess">No changes.</div> : (
        <ul className="ws-prop-list">
          {p.changes.map((c, i) => (
            <li key={i} className={"op " + ({ "+": "add", "~": "chg", "-": "del" }[c.op] || "")}>
              <span className="op-mark" aria-hidden="true">{c.op}</span>
              <button type="button" className="link" onClick={() => onFocus([c.node])}>{c.text}</button>
            </li>
          ))}
        </ul>
      )}
      {!p.check.ok && (
        <div className="pl-bad">Check would report: {p.check.errors.slice(0, 3).map((e) => e.text).join("; ")}{p.check.errors.length > 3 ? " …" : ""}</div>
      )}
      <div className="btn-row">
        <button className="primary" disabled={disabled || p.stale} onClick={() => act("POST", "/proposal/accept")}
                title="Copies these changes into the draft; you still Check and Apply">Add to draft</button>
        <button onClick={() => { onShow(); onFocus(nodes); }}>Show on canvas</button>
        <button className="link" disabled={disabled} onClick={() => act("DELETE", "/proposal")}>Discard</button>
      </div>
    </div>
  );
}

export function AgentTab({ view, act, disabled, compose, context, contextLabel, onClearContext, onFocus, onShowProposal }) {
  const [text, setText] = useState("");
  const [seen, setSeen] = useState(null);
  if (compose && compose.at !== seen) { setSeen(compose.at); setText(compose.text); }
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState(null);
  const bottom = useRef(null);
  const box = useRef(null);
  const busy = !!view.busy;
  const msgs = view.messages || [];
  useEffect(() => { bottom.current?.scrollIntoView({ block: "end" }); }, [msgs.length, busy]);
  useEffect(() => { if (compose) box.current?.focus(); }, [compose]);

  const last = msgs[msgs.length - 1];
  const failed = last?.kind === "event" && last.level === "error" && /agent/i.test(last.text || "");
  const lastAsk = [...msgs].reverse().find((m) => m.kind === "user");

  async function send(t = text) {
    const body = t.trim();
    if (!body || busy) return;
    const r = await act("POST", "/messages", { text: context ? `[About ${contextLabel}] ${body}` : body });
    if (r) setText("");
  }
  async function attach(e) {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (!files.length) return;
    setUploading(true);
    setUploadError(null);
    try {
      const res = await uploadFiles(files);
      for (const u of res.uploaded) await act("POST", "/sources", { kind: "url", url: u.url, label: u.filename });
      if (res.errors?.length) setUploadError(res.errors.map((er) => `${er.filename}: ${er.error}`).join("; "));
    } catch (err) {
      setUploadError(err.message);
    } finally {
      setUploading(false);
    }
  }
  const copy = async (t) => { try { await navigator.clipboard.writeText(t); } catch { /* clipboard blocked: nothing to do */ } };

  return (
    <div className={"ws-agent" + (view.proposal ? " with-prop" : "")}>
      <div className="ws-agent-chat">
        <div className="pl-messages">
          {msgs.map((m) => <Message key={m.id} m={m} />)}
          {failed && (
            <div className="ws-retry">
              {lastAsk && <button disabled={busy} onClick={() => send(lastAsk.text)}>Retry</button>}
              <button className="link" onClick={() => copy(last.text)}>Copy details</button>
            </div>
          )}
          {busy && (
            <div className="pl-event info"><span className="spin">●</span> {view.busy === "run" ? "Running the pipeline" : "The agent is working"}
              {view.progress ? ` · ${view.progress}` : "…"}</div>
          )}
          <div ref={bottom} />
        </div>
        {uploadError && <div className="banner err" style={{ margin: "0 .6rem .4rem" }}>{uploadError}</div>}
        <div className="ws-compose">
          {context && (
            <div className="ws-context">Context <span className="ws-ctx-chip">{contextLabel}
              <button className="link" aria-label="Remove context" onClick={onClearContext}>✕</button></span></div>
          )}
          <textarea ref={box} rows={2} value={text} disabled={busy}
                    placeholder={busy ? "Wait for the agent…" : "Ask for a change to the pipeline… (Enter to send)"}
                    onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
          <div className="ws-compose-row">
            <label className={"pl-attach" + (busy || uploading ? " disabled" : "")} title="Upload .pdf, .csv or .txt as a source">
              <input type="file" multiple accept=".txt,.csv,.pdf" onChange={attach} disabled={busy || uploading} />
              {uploading ? "Uploading…" : "Attach"}
            </label>
            <span className="spacer" />
            <button className="primary" onClick={() => send()} disabled={busy || !text.trim()} aria-label="Send">Send</button>
          </div>
        </div>
      </div>
      {view.proposal && <ProposalCard view={view} act={act} disabled={disabled} onFocus={onFocus} onShow={onShowProposal} />}
    </div>
  );
}

// ── Records ──────────────────────────────────────────────────────────────────

const FILTERS = [["all", "All"], ["review", "To review"], ["unquoted", "Unquoted"], ["flagged", "Flagged"]];

function Cell({ r, f, accepted }) {
  const st = cellState(r, f);
  const v = r.values?.[f.name];
  if (st === "blank") return <span className={"ws-pill " + (accepted ? "ok" : "warn")}>{accepted ? "blank · accepted" : "blank"}</span>;
  if (st === "unquoted" && !accepted) return <span className="ws-pill bad" title={short(v, 200)}>unquoted</span>;
  if (st === "conflict" && !accepted) return <span className="ws-pill bad" title={short(v, 200)}>conflict</span>;
  return <span className={"ws-val " + st} title={short(v, 300)}>{st === "quoted" || accepted ? <span className="ws-tick">✓</span> : null}{short(v, 40)}</span>;
}

export function EvidencePane({ view, act, disabled, r, field, final, onOpen, onClose }) {
  const fields = view.definition.schema?.fields || [];
  const f = fields.find((x) => x.name === field);
  const [edit, setEdit] = useState(null);
  if (!r || !f) return null;
  const p = r.prov?.[field];
  const st = cellState(r, f);
  const mark = final ? view.review_marks?.[r.id] : null;
  const accepted = (mark?.accepted || []).includes(field);
  const step = (view.definition.steps || []).find((s) => s.id === p?.step);
  const conflict = (r.conflicts || []).find((c) => c.field === field);
  const url = p?.source_url || r.source_url;
  const save = async () => {
    const res = await act("POST", "/output/records", { op: "set", record_id: r.id, values: { [field]: edit } });
    if (res) setEdit(null);
  };
  return (
    <div className="ws-evidence">
      <div className="ws-ev-head"><span className="assess">{recordName(r, fields)} · {r.source_id || ""}</span>
        {onClose && <button className="link" onClick={onClose} aria-label="Close">✕</button>}</div>
      <code className="ws-ev-field">{field}</code>
      {p?.step && (
        <div className="ws-ev-by">Filled by <span className="ins-chip">{p.step} {view.catalog[step?.type]?.label || ""}</span>
          <span className="assess"> · {KIND_TEXT[p.kind] || p.kind}</span></div>
      )}
      {p?.note && <div className="assess">{p.note}</div>}
      {p?.was && <div className="assess">The pipeline produced: {isBlank(p.was.value) ? "blank" : `“${short(p.was.value, 120)}”`}</div>}
      {st === "blank" ? <div className="ws-quote none">No step filled it{f.accept_blank ? "; blanks are accepted for this field" : ""}.</div>
        : p?.quote ? <blockquote className={"ws-quote " + (p.verified ? "ok" : "bad")}>“{p.quote}”
          {!p.verified && <div className="pl-bad">Not found word for word in the source{p.unmatched?.length ? `: ${p.unmatched.map((m) => `“${short(m, 60)}”`).join(", ")}` : ""}.</div>}</blockquote>
        : <div className="ws-quote none">{p?.kind === "inferred" ? "A model judgement: it carries a reason, not a quote." : p?.kind === "manual" ? "Entered by you." : "No quote: computed by a step."}</div>}
      {conflict && <div className="pl-conflicts">A source page says {conflict.page_says}: <blockquote>{conflict.quote}</blockquote></div>}
      {url && <a href={url} target="_blank" rel="noreferrer" className="ins-url">{short(url, 70)} ↗</a>}
      {edit !== null ? (
        <div className="ws-ev-edit">
          <input autoFocus value={edit} onChange={(e) => setEdit(e.target.value)} onKeyDown={(e) => e.key === "Enter" && save()} />
          <button className="primary" disabled={disabled} onClick={save}>Save</button>
          <button onClick={() => setEdit(null)}>Cancel</button>
        </div>
      ) : (
        <div className="btn-row">
          {final && <button disabled={disabled} onClick={() => act("POST", `/records/${r.id}/accept`, { fields: [field], accept: !accepted })}>
            {accepted ? "✓ Accepted" : "Accept"}</button>}
          {final && <button disabled={disabled || !!view.output_locked} title={view.output_locked || ""}
                            onClick={() => setEdit(isBlank(r.values?.[field]) ? "" : String(r.values[field]))}>Edit value</button>}
          {onOpen && <button className="link" onClick={() => onOpen(r.id)}>Open full view</button>}
        </div>
      )}
    </div>
  );
}

function RecordsTab({ view, act, disabled, recordsOf, setRecordsOf, trace, setTrace, onOpenRecord }) {
  const fields = view.definition.schema?.fields || [];
  const [q, setQ] = useState("");
  const [filt, setFilt] = useState("all");
  const final = recordsOf === "final";
  const out = final ? null : view.outputs[recordsOf];
  const records = final ? view.last_records || [] : out?.records || [];
  const marks = final ? view.review_marks || {} : {};
  const shown = records.filter((r) => matchesFilter(r, fields, marks[r.id], filt, q));
  const withOutput = (view.definition.steps || []).filter((s) => view.outputs[s.id] && !view.outputs[s.id].error);
  const run = view.last_run;
  const traced = trace && records.find((r) => r.id === trace.recordId);
  return (
    <div className={"ws-records" + (traced ? " with-ev" : "")}>
      <div className="ws-records-main">
        <div className="ws-rec-bar">
          <input className="ws-search" placeholder="Search records" value={q} onChange={(e) => setQ(e.target.value)} />
          {FILTERS.map(([k, t]) => (
            <button key={k} type="button" className={"ws-filter" + (filt === k ? " on" : "")} onClick={() => setFilt(k)}>{t}</button>
          ))}
          <span className="spacer" />
          <select value={recordsOf} onChange={(e) => { setRecordsOf(e.target.value); setTrace(null); }} aria-label="Whose records">
            <option value="final">Final output</option>
            {withOutput.map((s) => <option key={s.id} value={s.id}>Output of {s.id} {view.catalog[s.type]?.label}</option>)}
          </select>
          <span className="assess">{final ? (run ? `${run.upto ? `Preview to ${run.upto}` : "Last run"} on the applied pipeline` : "No run yet")
            : `${out?.count ?? 0} record(s) from its last preview`}</span>
        </div>
        {records.length === 0 ? <div className="assess" style={{ padding: ".75rem" }}>No records yet: run the pipeline, or run a step on the sample.</div> : (
          <div className="table-wrap ws-rec-table">
            <table>
              <thead><tr>{fields.map((f) => <th key={f.name}>{f.name}{f.required ? "*" : ""}</th>)}{final && <th>Status</th>}</tr></thead>
              <tbody>
                {shown.map((r) => {
                  const mark = marks[r.id];
                  const st = recordStatus(r, fields, mark);
                  return (
                    <tr key={r.id} className={trace?.recordId === r.id ? "on" : ""}>
                      {fields.map((f, i) => (
                        <td key={f.name} className={trace?.recordId === r.id && trace.field === f.name ? "traced" : ""}
                            onClick={() => setTrace({ recordId: r.id, field: f.name, step: r.prov?.[f.name]?.step || null })}>
                          {i === 0 ? <span className="ws-rec-name">{recordName(r, fields)} <span className="assess">{r.source_id}</span></span>
                            : <Cell r={r} f={f} accepted={(mark?.accepted || []).includes(f.name)} />}
                        </td>
                      ))}
                      {final && <td><button type="button" className={"ws-status " + st.kind} onClick={() => onOpenRecord(r.id)}
                                            title={mark?.flag?.note || "Open the record"}>{st.text}</button></td>}
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {shown.length === 0 && <div className="assess" style={{ padding: ".5rem" }}>No records match.</div>}
          </div>
        )}
      </div>
      {traced && <EvidencePane key={`${trace.recordId}:${trace.field}`} view={view} act={act} disabled={disabled} r={traced} field={trace.field}
                               final={final} onOpen={final ? onOpenRecord : null} onClose={() => setTrace(null)} />}
    </div>
  );
}

// ── Check ────────────────────────────────────────────────────────────────────

function CheckTab({ view, onFocus }) {
  const item = (x, k, cls) => (
    <li key={k} className={cls}>
      {x.node ? <button type="button" className="link" onClick={() => onFocus([x.node === "output" ? "output" : `step:${x.node}`])}>{x.node === "output" ? "Output" : x.node}</button> : null}
      {x.node ? " · " : ""}{x.text}
    </li>
  );
  const c = view.draft?.check;
  if (!c) {
    return (
      <div className="ws-check">
        <div className="assess">No draft is open: Check looks at a draft before it applies. Press Edit pipeline to start one.</div>
        {view.design_gaps?.length > 0 && <div className="pl-warn">No step produces {view.design_gaps.join(", ")}.</div>}
        {view.gate?.length > 0 && <><b>Before publishing</b><ul className="pl-warn-list">{view.gate.map((g, i) => <li key={i}>{g}</li>)}</ul></>}
      </div>
    );
  }
  const steps = view.draft.steps || [];
  const resets = steps.filter((s) => s.resets).map((s) => s.id);
  return (
    <div className="ws-check">
      <div className="pl-check-head">
        {c.ok ? <span className="tag ok">no errors</span> : <span className="tag gone">{c.errors.length} error{c.errors.length > 1 ? "s" : ""}</span>}
        {c.warnings.length > 0 && <span className="tag edited">{c.warnings.length} warning{c.warnings.length > 1 ? "s" : ""}</span>}
        <span className="assess">{resets.length ? `Apply sends ${resets.join(", ")} back for approval; the other steps keep theirs.` : "Every applied step keeps its approval."}</span>
      </div>
      {(c.errors.length > 0 || c.warnings.length > 0) ? (
        <ul className="pl-check-list">
          {c.errors.map((x, k) => item(x, "e" + k, "err"))}
          {c.warnings.map((x, k) => item(x, "w" + k, "warn"))}
        </ul>
      ) : <div className="assess">Nothing to fix: Apply when ready.</div>}
    </div>
  );
}

// ── The drawer ───────────────────────────────────────────────────────────────

export default function Drawer(props) {
  const { view, open, setOpen, height, setHeight, tab, setTab } = props;
  const n = (view.last_records || []).length;
  const errs = view.draft?.check?.errors?.length || 0;
  const startDrag = (e) => {
    e.preventDefault();
    const y0 = e.clientY, h0 = height;
    const move = (ev) => setHeight(Math.max(140, Math.min(window.innerHeight * 0.75, h0 - (ev.clientY - y0))));
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  const tabs = [["agent", "Agent", view.proposal ? view.proposal.changes.length : null], ["records", `Records · ${n}`, null],
                ["check", "Check results", errs || null]];
  return (
    <section className={"ws-drawer" + (open ? "" : " closed")} style={open ? { height } : undefined} aria-label="Agent, records and check">
      {open && <div className="ws-grip" onPointerDown={startDrag} role="separator" aria-orientation="horizontal" aria-label="Resize" />}
      <div className="ws-tabs" role="tablist">
        {tabs.map(([k, t, badge]) => (
          <button key={k} type="button" role="tab" aria-selected={open && tab === k} className={"ws-tab" + (open && tab === k ? " on" : "")}
                  onClick={() => { setTab(k); setOpen(true); }}>
            {t}{badge ? <span className={"ws-count" + (k === "check" ? " bad" : "")}>{badge}</span> : null}
          </button>
        ))}
        <span className="spacer" />
        <button type="button" className="link" onClick={() => setOpen(!open)} aria-label={open ? "Collapse" : "Expand"}>{open ? "▾" : "▴"}</button>
      </div>
      {open && (
        <div className="ws-pane">
          {tab === "agent" && <AgentTab {...props} />}
          {tab === "records" && <RecordsTab {...props} />}
          {tab === "check" && <CheckTab view={view} onFocus={props.onFocus} />}
        </div>
      )}
    </section>
  );
}
