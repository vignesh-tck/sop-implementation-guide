import { useEffect, useRef, useState } from "react";
import { call } from "../api.js";
import { isMulti, outputList, ownerOf, stepView, stepsModel } from "./ports.js";

// Add a step, or change one, two ways side by side: set it up yourself from the
// catalog (the server checks it against the schema, the same as the agent's
// steps), or describe it and let the agent propose it in the chat.
//
// Steps change in a draft: adding and editing here open one if needed.
//
// mode: { kind: "add", after?, before?, connect?, output?, type?, at? } | { kind: "edit", stepId }
//     | { kind: "ask", text } (the agent half only)
//
// With several outputs a step works in the schema of the output it feeds: the
// one its neighbour feeds, or the one picked under Wiring.

const OPS = ["equals", "not_equals", "contains", "not_contains", "is_blank", "not_blank", "before", "after", "gte", "lte"];
const NO_VALUE = ["is_blank", "not_blank"];

// What a new step of each type starts with.
function initial(type, fields) {
  const first = fields[0]?.name || "";
  const geo = fields.find((f) => f.name === "geo_scope")?.name || first;
  switch (type) {
    case "parse": return { field: fields.find((f) => /amount|award|match|deadline|date/.test(f.name))?.name || first, as: "money" };
    case "map_values": return { field: first, mapping: {}, case_insensitive: true };
    case "set_value": return { field: first, value: "", only_if_blank: true };
    case "filter": return { field: first, op: "not_blank" };
    case "dedupe": return { keys: [first] };
    case "research": return { max_pages_per_record: 2, web_search: false };
    case "llm_fill": return { field: first, instruction: "", only_if_blank: true };
    case "geo_tie": return { field: geo };
    case "sql": return { query: "SELECT *\nFROM records", tables: null };
    case "from_table": return { table: "blocks" };
    case "spatial_lookup": return { layers: [59], field: "CLASS", writes: fields.find((f) => f.name === "zones")?.name || first,
                                    buffer_m: 25, block_field: fields.find((f) => f.name === "block_id")?.name || first,
                                    ...(fields.some((f) => f.name === "lookup_status") ? { status_field: "lookup_status" } : {}) };
    case "use_table": return {};
    case "read_table": return { table: "blocks" };
    default: return {};
  }
}

// A value typed into a text box, as the field's type wants it.
function typed(raw, f) {
  if (raw === "" || raw == null || !f) return raw;
  if (f.type === "boolean") return raw === true || raw === "true" || raw === "yes";
  if (["number", "money", "percent"].includes(f.type) && typeof raw === "string" && raw.trim() !== "" && !isNaN(Number(raw))) return Number(raw);
  return raw;
}

function FieldSelect({ fields, value, onChange, allowBlank }) {
  return (
    <select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
      {allowBlank && <option value="">—</option>}
      {fields.map((f) => <option key={f.name} value={f.name}>{f.name} ({f.type})</option>)}
    </select>
  );
}

function FieldChecks({ fields, value, onChange }) {
  const set = new Set(value || []);
  return (
    <div className="pl-checks">
      {fields.map((f) => (
        <label key={f.name}>
          <input type="checkbox" checked={set.has(f.name)}
                 onChange={(e) => onChange(fields.map((x) => x.name).filter((n) => (n === f.name ? e.target.checked : set.has(n))))} />
          <code>{f.name}</code>
        </label>
      ))}
    </div>
  );
}

// An input for a value of field f: a list for enums and yes/no, text otherwise.
function ValueInput({ f, value, onChange }) {
  if (f?.type === "enum") {
    return (
      <select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
        <option value="">—</option>
        {(f.enum_values || []).map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  }
  if (f?.type === "boolean") {
    return (
      <select value={value === true ? "true" : value === false ? "false" : ""} onChange={(e) => onChange(e.target.value === "" ? "" : e.target.value === "true")}>
        <option value="">—</option><option value="true">yes</option><option value="false">no</option>
      </select>
    );
  }
  return <input value={value ?? ""} placeholder={f?.type === "date" ? "YYYY-MM-DD" : f?.type === "money" ? "e.g. 50000" : ""}
                onChange={(e) => onChange(e.target.value)} />;
}

function Condition({ fields, c, onChange }) {
  const f = fields.find((x) => x.name === c.field);
  return (
    <div className="pl-cond">
      <FieldSelect fields={fields} value={c.field} onChange={(v) => onChange({ ...c, field: v })} />
      <select value={c.op || "equals"} onChange={(e) => onChange({ ...c, op: e.target.value })}>
        {OPS.map((o) => <option key={o} value={o}>{o.replace("_", " ")}</option>)}
      </select>
      {!NO_VALUE.includes(c.op) && <ValueInput f={f} value={c.value} onChange={(v) => onChange({ ...c, value: v })} />}
    </div>
  );
}

function MappingRows({ mapping, onChange }) {
  const [rows, setRows] = useState(() => {
    const r = Object.entries(mapping || {});
    return r.length ? r : [["", ""]];
  });
  const update = (next) => {
    setRows(next);
    onChange(Object.fromEntries(next.filter(([k]) => k.trim() !== "")));
  };
  return (
    <div className="pl-map-rows">
      {rows.map(([k, v], i) => (
        <div key={i} className="pl-map-row">
          <input placeholder="from" value={k} onChange={(e) => update(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
          <span className="assess">→</span>
          <input placeholder="to" value={v} onChange={(e) => update(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} />
          <button type="button" className="rm" title="Remove row" onClick={() => update(rows.filter((_, j) => j !== i))}>×</button>
        </div>
      ))}
      <button type="button" className="link" onClick={() => update([...rows, ["", ""]])}>+ add a row</button>
    </div>
  );
}

function SourcePick({ sources, kind, value, onChange }) {
  const fit = sources.filter((s) => s.kind === kind);
  return (
    <select value={value || ""} onChange={(e) => onChange(e.target.value || null)}>
      <option value="">—</option>
      {fit.map((s) => <option key={s.id} value={s.id}>{s.id} · {s.label}</option>)}
    </select>
  );
}

function Row({ label, hint, children }) {
  return (
    <label className="pl-form-row">
      <span className="pl-form-label">{label}</span>
      {children}
      {hint && <span className="assess">{hint}</span>}
    </label>
  );
}

// SQL: the query, the tables it may join (reference tables, CSV sources, and
// with several inputs each input on its own), and every column it can use.
// Clicking a table or column name puts it at the cursor.
function SqlForm({ c, set, fields, refTables, srcTables, sqlTypes, pipelineId, inputs }) {
  const box = useRef(null);
  const [test, setTest] = useState(null);      // { result } | { error } | { running: true }
  const joins = inputs.length > 1 ? inputs : [];
  const testOn = inputs.length === 1 ? inputs[0] : null;
  async function runTest() {
    setTest({ running: true });
    try {
      const result = await call("POST", `/pipelines/${pipelineId}/sql`, {
        query: c.query || "", tables: c.tables || null, limit: 50, ...(joins.length ? { inputs: joins } : { on: testOn }) });
      setTest({ result });
    } catch (e) {
      setTest({ error: e.message.replace(/^\d+ — /, "") });
    }
  }
  const tables = c.tables || [];
  const insert = (text) => {
    const el = box.current;
    const q = c.query || "";
    const at = el ? el.selectionStart : q.length, end = el ? el.selectionEnd : q.length;
    set({ ...c, query: q.slice(0, at) + text + q.slice(end) });
    requestAnimationFrame(() => { if (el) { el.focus(); el.selectionStart = el.selectionEnd = at + text.length; } });
  };
  const cols = (list) => <div className="pl-cols">{list.map(([n, t]) => (
    <button type="button" key={n} className="pl-col-btn" onClick={() => insert(n)} title={`Insert ${n}`}><code>{n}</code> {t}</button>
  ))}</div>;
  const choices = [...Object.entries(refTables || {}).map(([name, r]) => [name, r.description, r.columns]),
                   ...Object.entries(srcTables || {}).map(([name, r]) => [name, `CSV source${r.label ? `: ${r.label}` : ""}, read as it is (all text)`, r.columns])];
  const known = Object.fromEntries(choices.map(([name, desc, columns]) => [name, { desc, columns }]));
  const recordCols = [["id", "VARCHAR"], ...fields.map((f) => [f.name, sqlTypes?.[f.type] || "VARCHAR"])];
  return (
    <>
      <Row label="Tables to join" hint="Reference tables, and CSV sources by id (a lookup CSV needs no Extract step). Tick a table before the SQL uses it.">
        <div className="pl-checks">
          {choices.map(([name, desc]) => (
            <label key={name} title={desc}>
              <input type="checkbox" checked={tables.includes(name)}
                     onChange={(e) => {
                       const next = choices.map(([t]) => t).filter((t) => (t === name ? e.target.checked : tables.includes(t)));
                       set({ ...c, tables: next.length ? next : null });
                     }} />
              <code>{name}</code>
            </label>
          ))}
        </div>
      </Row>
      <Row label="SQL" hint="One SELECT. Return id plus the fields to set; only values that change are written. Records not returned are dropped.">
        <textarea ref={box} className="pl-sql-edit" rows={9} spellCheck={false} value={c.query || ""}
                  onChange={(e) => set({ ...c, query: e.target.value })}
                  onKeyDown={(e) => {
                    if (e.key !== "Tab") return;
                    e.preventDefault();
                    insert("  ");
                  }} />
      </Row>
      <div className="btn-row" style={{ marginTop: 0 }}>
        <button type="button" disabled={!inputs.length || !(c.query || "").trim() || test?.running} onClick={runTest}>
          {test?.running ? "Running…" : "Test on input"}
        </button>
        <span className="assess" style={{ alignSelf: "center" }}>
          {joins.length ? `Runs the query on the last previews of ${joins.join(" and ")}. Nothing is saved.`
            : testOn ? `Runs the query on ${testOn}'s last preview. Nothing is saved.`
            : "A SQL step works on the records of the step before it: put it after a step to test it."}
        </span>
      </div>
      {test?.error && <div className="banner err" style={{ margin: 0 }}>{test.error}</div>}
      {test?.result && <SqlResult r={test.result} />}
      <div className="pl-sql-help">
        <div><button type="button" className="pl-col-btn tbl" onClick={() => insert("records")}><code>records</code></button>
          <span className="assess">{joins.length ? `every input's records (${joins.join(" + ")}); ` : ""}typed; values that don't fit their type are NULL here</span></div>
        {cols(recordCols)}
        <div><button type="button" className="pl-col-btn tbl" onClick={() => insert("records_text")}><code>records_text</code></button>
          <span className="assess">the same columns, all VARCHAR</span></div>
        {joins.map((name) => (
          <div key={name}>
            <div><button type="button" className="pl-col-btn tbl" onClick={() => insert(name)}><code>{name}</code></button>
              <span className="assess">only {name}'s records, to JOIN one input to another. Rows keep the ids they return.</span></div>
          </div>
        ))}
        {tables.map((name) => (
          <div key={name}>
            <div><button type="button" className="pl-col-btn tbl" onClick={() => insert(name)}><code>{name}</code></button>
              <span className="assess">{known[name]?.desc}</span></div>
            {cols(Object.entries(known[name]?.columns || {}))}
          </div>
        ))}
      </div>
    </>
  );
}

function SqlResult({ r }) {
  const cell = (v) => (v == null ? <span className="assess">NULL</span> : typeof v === "object" ? JSON.stringify(v) : String(v));
  return (
    <div className="pl-sql-result">
      <div className="assess">
        On {r.records_from} ({r.records_count} record{r.records_count === 1 ? "" : "s"}): {r.more ? `more than ${r.row_count}` : r.row_count} row(s)
        {r.shown < r.row_count ? `, first ${r.shown} shown` : ""}.
      </div>
      <div className={r.as_step.ok ? "pl-sql-ok" : "pl-warn"}>
        {r.as_step.ok ? <>Works as a step: writes {r.as_step.writes.length ? r.as_step.writes.map((w) => <code key={w} style={{ marginRight: ".3rem" }}>{w}</code>) : "no fields (it only filters)"}</>
          : <>As a step: {r.as_step.why}</>}
      </div>
      {r.notes?.length > 0 && <ul className="pl-warn-list">{r.notes.map((n, i) => <li key={i} className="assess">{n}</li>)}</ul>}
      {r.columns.length > 0 && (
        <div className="table-wrap">
          <table className="pl-port-table">
            <thead><tr>{r.columns.map((col) => <th key={col.name} title={col.type}>{col.name}</th>)}</tr></thead>
            <tbody>{r.rows.map((row, i) => <tr key={i}>{row.map((v, j) => <td key={j}>{cell(v)}</td>)}</tr>)}</tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// The settings for one step type. Keys this form does not know are kept as they are.
function ConfigForm({ type, c, set, fields, sources, refTables, srcTables, sqlTypes, pipelineId, inputs }) {
  const put = (k, v) => set({ ...c, [k]: v });
  const field = fields.find((f) => f.name === c.field);
  switch (type) {
    case "extract":
      return (
        <>
          <Row label="Reads from" hint="None ticked reads every source.">
            <div className="pl-checks">
              {sources.map((s) => {
                const on = (c.source_ids || []).includes(s.id);
                return (
                  <label key={s.id} title={s.url || s.query}>
                    <input type="checkbox" checked={on}
                           onChange={(e) => {
                             const ids = sources.map((x) => x.id).filter((id) => (id === s.id ? e.target.checked : (c.source_ids || []).includes(id)));
                             put("source_ids", ids.length ? ids : null);
                           }} />
                    <b className="pl-id">{s.id}</b> {s.label}
                  </label>
                );
              })}
            </div>
          </Row>
          <Row label="Instructions" hint="Optional: what counts as one item, which sections matter, what to ignore.">
            <textarea rows={4} value={c.instructions || ""} onChange={(e) => put("instructions", e.target.value || null)} />
          </Row>
        </>
      );
    case "parse":
      return (
        <>
          <Row label="Field"><FieldSelect fields={fields} value={c.field} onChange={(v) => put("field", v)} /></Row>
          <Row label="Read as">
            <select value={c.as || "money"} onChange={(e) => put("as", e.target.value)}>
              {["money", "number", "percent", "date"].map((o) => <option key={o}>{o}</option>)}
            </select>
          </Row>
        </>
      );
    case "map_values":
      return (
        <>
          <Row label="Field"><FieldSelect fields={fields} value={c.field} onChange={(v) => put("field", v)} /></Row>
          <Row label="Mapping" hint="Values with no row are kept as they are."><MappingRows mapping={c.mapping} onChange={(m) => put("mapping", m)} /></Row>
          <label className="assess"><input type="checkbox" checked={c.case_insensitive !== false}
                 onChange={(e) => put("case_insensitive", e.target.checked)} /> Ignore case</label>
        </>
      );
    case "set_value":
      return (
        <>
          <Row label="Field"><FieldSelect fields={fields} value={c.field} onChange={(v) => put("field", v)} /></Row>
          <Row label="Value" hint="A value you know to be true. Never a guess to fill a gap."><ValueInput f={field} value={c.value} onChange={(v) => put("value", v)} /></Row>
          <label className="assess"><input type="checkbox" checked={c.only_if_blank !== false}
                 onChange={(e) => put("only_if_blank", e.target.checked)} /> Only where the field is blank</label>
          <label className="assess"><input type="checkbox" checked={!!c.where}
                 onChange={(e) => put("where", e.target.checked ? { field: fields[0]?.name, op: "equals", value: "" } : null)} /> Only where…</label>
          {c.where && <Condition fields={fields} c={c.where} onChange={(w) => put("where", w)} />}
        </>
      );
    case "filter":
      return <Row label="Keep records where"><Condition fields={fields} c={c} onChange={(w) => set({ ...c, ...w })} /></Row>;
    case "dedupe":
      return <Row label="Same record when these match"><FieldChecks fields={fields} value={c.keys} onChange={(v) => put("keys", v)} /></Row>;
    case "research":
      return (
        <>
          <Row label="Fill these fields" hint="None ticked: every extractable field that is blank somewhere.">
            <FieldChecks fields={fields.filter((f) => f.extract !== false)} value={c.fields} onChange={(v) => put("fields", v.length ? v : null)} />
          </Row>
          <Row label="Pages per record">
            <select value={c.max_pages_per_record || 2} onChange={(e) => put("max_pages_per_record", Number(e.target.value))}>
              {[1, 2, 3, 4].map((n) => <option key={n}>{n}</option>)}
            </select>
          </Row>
          <label className="assess"><input type="checkbox" checked={!!c.web_search} onChange={(e) => put("web_search", e.target.checked)} />
            {" "}Search the web for an official page when the source links nothing useful</label>
          <Row label="Instructions" hint="Optional: what to look for.">
            <textarea rows={3} value={c.instructions || ""} onChange={(e) => put("instructions", e.target.value || null)} />
          </Row>
        </>
      );
    case "llm_fill":
      return (
        <>
          <Row label="Field"><FieldSelect fields={fields} value={c.field} onChange={(v) => put("field", v)} /></Row>
          <Row label="Instruction" hint="The judgement to make from each record's own excerpt. Values are marked inferred.">
            <textarea rows={4} value={c.instruction || ""} onChange={(e) => put("instruction", e.target.value)} />
          </Row>
          <label className="assess"><input type="checkbox" checked={c.only_if_blank !== false}
                 onChange={(e) => put("only_if_blank", e.target.checked)} /> Only where the field is blank</label>
        </>
      );
    case "geo_tie":
      return (
        <>
          <Row label="Field"><FieldSelect fields={fields} value={c.field || "geo_scope"} onChange={(v) => put("field", v)} /></Row>
          <Row label="Instruction" hint="Optional: replaces the default guidance. The allowed values stay fixed.">
            <textarea rows={3} value={c.instruction || ""} onChange={(e) => put("instruction", e.target.value || null)} />
          </Row>
        </>
      );
    case "sql":
      return <SqlForm c={c} set={set} fields={fields} refTables={refTables} srcTables={srcTables} sqlTypes={sqlTypes}
                      pipelineId={pipelineId} inputs={inputs} />;
    case "from_table":
      return (
        <>
          <Row label="Data on file" hint="One record per row. Fields named like a column are filled from it.">
            <select value={c.table || ""} onChange={(e) => put("table", e.target.value)}>
              {Object.entries(refTables || {}).map(([k, r]) => <option key={k} value={k} title={r.description}>{k}</option>)}
            </select>
          </Row>
          {c.table && refTables?.[c.table] && (
            <div className="assess">{refTables[c.table].description} Columns: {Object.keys(refTables[c.table].columns).slice(0, 14).join(", ")}
              {Object.keys(refTables[c.table].columns).length > 14 ? " …" : ""}</div>
          )}
        </>
      );
    case "spatial_lookup":
      return (
        <>
          <Row label="Map source" hint="An ArcGIS service or layer source.">
            <SourcePick sources={sources} kind="arcgis" value={c.source_id} onChange={(v) => put("source_id", v)} />
          </Row>
          <Row label="Layers" hint="Layer ids in the service, comma-separated (e.g. 59 for zoning, 65 prior zoning, 26, 46, 29, 31 cases).">
            <input value={(c.layers || []).join(", ")}
                   onChange={(e) => put("layers", e.target.value.split(/[ ,]+/).filter(Boolean).map(Number).filter((n) => !isNaN(n)))} />
          </Row>
          <Row label="Layer field" hint="The attribute to read (CLASS, CASE_NUM), or * for each polygon's name.">
            <input value={c.field || ""} onChange={(e) => put("field", e.target.value)} />
          </Row>
          <Row label="Writes"><FieldSelect fields={fields} value={c.writes} onChange={(v) => put("writes", v)} /></Row>
          <Row label="Block id field"><FieldSelect fields={fields} value={c.block_field || "block_id"} onChange={(v) => put("block_field", v)} /></Row>
          <Row label="Buffer (m)" hint="How far from the block's centreline counts as fronting it (5-100).">
            <input type="number" min={5} max={100} value={c.buffer_m ?? 25} onChange={(e) => put("buffer_m", Number(e.target.value))} />
          </Row>
          <Row label="Status field" hint="Optional, on the zones lookup: records found / no_zone / failed. A failed lookup is never stored as no zone.">
            <FieldSelect fields={fields} allowBlank value={c.status_field} onChange={(v) => put("status_field", v || null)} />
          </Row>
        </>
      );
    case "use_table":
      return (
        <>
          <Row label="Use-table source" hint="The enCodePlus page of §27-5101 (doc-view.aspx?secid=649).">
            <SourcePick sources={sources} kind="url" value={c.source_id} onChange={(v) => put("source_id", v)} />
          </Row>
          <Row label="Tables" hint="None ticked reads (c), (d) and (e). The overlay table (f) is not read as permissions.">
            <div className="pl-checks">
              {["27-5101(c)", "27-5101(d)", "27-5101(e)"].map((t) => (
                <label key={t}><input type="checkbox" checked={(c.tables || []).includes(t)}
                       onChange={(e) => {
                         const next = ["27-5101(c)", "27-5101(d)", "27-5101(e)"].filter((x) => (x === t ? e.target.checked : (c.tables || []).includes(x)));
                         put("tables", next.length ? next : null);
                       }} /> <code>{t}</code></label>
              ))}
            </div>
          </Row>
          <div className="assess">Fields named like a column are filled: use, use_group, use_category, standards, amended_through, and zone_&lt;code&gt; (zone_tac_c …).</div>
        </>
      );
    case "read_table": {
      // Our Supabase tables, as the SQL step names them (db.<table>), and this pipeline's CSV sources.
      const dbTables = Object.fromEntries(Object.entries(refTables || {}).filter(([k]) => k.startsWith("db."))
        .map(([k, r]) => [k.slice(3), r]));
      const csvs = srcTables || {};
      const cols = Object.keys((c.source_id ? csvs[c.source_id] : dbTables[c.table])?.columns || {});
      const map = c.fields || {};
      return (
        <>
          <Row label="Table" hint="One record per row, read only, no model: each cell is copied as it is. Filter or join rows in a SQL step after it.">
            <select value={c.source_id ? `csv:${c.source_id}` : c.table || ""}
                    onChange={(e) => {
                      const v = e.target.value;
                      set(v.startsWith("csv:") ? { ...c, source_id: v.slice(4), table: null, fields: null }
                        : { ...c, source_id: null, table: v, fields: null });
                    }}>
              {Object.keys(csvs).length > 0 && (
                <optgroup label="CSV sources">
                  {Object.entries(csvs).map(([k, r]) => <option key={k} value={`csv:${k}`}>{k}{r.label ? `: ${r.label}` : ""}</option>)}
                </optgroup>
              )}
              <optgroup label="Supabase tables">
                {Object.keys(dbTables).map((k) => <option key={k} value={k}>{k}</option>)}
              </optgroup>
            </select>
          </Row>
          {c.source_id && <div className="assess">A CSV's columns are its header row in lower_snake_case. A cell that does not fit its field's type is left blank and reported.</div>}
          <Row label="Fields from columns" hint="A field named like a column fills itself; pick a column for the others.">
            <table className="pl-loadmap-table">
              <tbody>
                {fields.map((f) => {
                  const auto = !(f.name in map) && cols.includes(f.name);
                  return (
                    <tr key={f.name}>
                      <td><code>{f.name}</code></td>
                      <td>
                        <select value={map[f.name] || (auto ? f.name : "")} aria-label={`Column for ${f.name}`}
                                onChange={(e) => {
                                  const next = { ...map };
                                  if (e.target.value) next[f.name] = e.target.value; else delete next[f.name];
                                  put("fields", Object.keys(next).length ? next : null);
                                }}>
                          <option value="">none</option>
                          {cols.map((col) => <option key={col} value={col}>{col}</option>)}
                        </select>
                        {auto && <span className="tag"> same name</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Row>
        </>
      );
    }
    default:
      return <div className="assess">No settings.</div>;
  }
}

// The form's values as the server expects them.
function cleanConfig(type, c, fields) {
  const out = { ...c };
  for (const k of Object.keys(out)) if (out[k] === null || out[k] === undefined) delete out[k];
  const f = (name) => fields.find((x) => x.name === name);
  if (type === "set_value") {
    out.value = typed(out.value, f(out.field));
    if (out.where) out.where = { ...out.where, value: typed(out.where.value, f(out.where.field)) };
    if (out.where && NO_VALUE.includes(out.where.op)) delete out.where.value;
  }
  if (type === "filter") {
    if (NO_VALUE.includes(out.op)) delete out.value;
    else out.value = typed(out.value ?? "", f(out.field));
  }
  return out;
}

export default function StepDialog({ view, mode, act, onClose, onAdded }) {
  const model = stepsModel(view);          // the draft if one is open, else the applied steps
  const steps = model.steps;
  const multi = isMulti(view);
  const outs = outputList(view);
  const sources = model.sources;
  const catalog = view.catalog || {};
  const editing = mode.kind === "edit" ? steps.find((s) => s.id === mode.stepId) : null;

  // Where a new step is wired: "none" (wire it on the canvas), "end" / "end:o2" (feeds an output),
  // "after:ID", "before:ID".
  const endOf = (oid) => (oid && oid !== "output" ? `end:${oid}` : "end");
  const [wiring, setWiring] = useState(mode.after ? `after:${mode.after}` : mode.before ? `before:${mode.before}` : mode.connect ? endOf(mode.output) : "none");
  const wiredOutput = wiring.startsWith("end:") ? wiring.slice(4) : wiring === "end" ? "output" : null;
  const near = editing?.id || (wiring.startsWith("after:") ? wiring.slice(6) : wiring.startsWith("before:") ? wiring.slice(7) : null);
  // The schema the step works in: its output's.
  const owner = near ? ownerOf(view, near) : wiredOutput || mode.output || "output";
  const fields = near ? stepView(view, near).definition.schema?.fields || []
    : outs.find((o) => o.id === owner)?.schema?.fields || view.definition.schema?.fields || [];

  const [type, setType] = useState(editing?.type || mode.type || "");
  const [config, setConfig] = useState(() => (editing ? { ...editing.config } : mode.type ? initial(mode.type, fields) : {}));
  const [note, setNote] = useState(editing?.note || "");
  const [ask, setAsk] = useState(() => {
    if (mode.kind === "ask") return mode.text || "";
    if (editing) return `Change step ${editing.id} (${catalog[editing.type]?.label || editing.type}): `;
    const where = mode.after ? `right after ${mode.after}` : mode.before ? `right before ${mode.before}` : "at the end";
    return `Add a step ${where} that `;
  });
  // A SQL draft is tested on the records it would receive: its first input's last preview.
  // The steps it takes, as wired (several: a SQL step sees each as a table of its own).
  const inputs = editing ? model.inputs[editing.id] || []
    : wiring.startsWith("after:") ? [wiring.slice(6)]
    : wiring.startsWith("before:") ? model.inputs[wiring.slice(7)] || []
    : wiredOutput ? model.outputsFrom[wiredOutput] || [] : [];
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const pickType = (t) => { setType(t); setConfig(initial(t, fields)); setError(null); };

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const cfg = cleanConfig(type, config, fields);
      if (!view.draft) await act("POST", "/draft", undefined, { throwErrors: true });
      if (editing) {
        await act("PATCH", `/draft/steps/${editing.id}`, { config: cfg, ...(note !== (editing.note || "") ? { note } : {}) }, { throwErrors: true });
      } else {
        const known = new Set([...steps.map((x) => x.id), ...(view.definition.steps || []).map((x) => x.id)]);
        const where = wiring.startsWith("after:") ? { after: wiring.slice(6) } : wiring.startsWith("before:") ? { before: wiring.slice(7) }
          : { connect: !!wiredOutput, output: wiredOutput || owner };
        const r = await act("POST", "/draft/steps", { type, config: cfg, note: note || null, ...where }, { throwErrors: true });
        const added = (r?.draft?.steps || []).find((x) => !known.has(x.id));
        if (added) onAdded?.(added.id, mode.at);
      }
      onClose();
    } catch (e) {
      setError(e.message.replace(/^\d+ — /, ""));
    } finally {
      setSaving(false);
    }
  }

  async function send() {
    if (!ask.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await act("POST", "/messages", { text: ask.trim() }, { throwErrors: true });
      onClose();
    } catch (e) {
      setError(e.message.replace(/^\d+ — /, ""));
    } finally {
      setSaving(false);
    }
  }

  const cat = catalog[type];
  const askOnly = mode.kind === "ask";
  return (
    <div className="pl-modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={"pl-modal" + (askOnly ? " narrow" : "")} role="dialog" aria-modal="true"
           aria-label={askOnly ? "Ask the agent" : editing ? `Edit step ${editing.id}` : "Add a step"}>
        <div className="pl-modal-head">
          <b>{askOnly ? "Ask the agent" : editing ? `Edit ${editing.id} · ${cat?.label || editing.type}` : "Add a step"}</b>
          <span className="spacer" />
          <button className="link" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {error && <div className="banner err" style={{ margin: ".5rem .9rem 0" }}>{error}</div>}
        <div className="pl-modal-body">
          {!askOnly && <section className="pl-modal-col">
            <h5>Set it up yourself</h5>
            {!editing && (
              <div className="pl-type-grid">
                {Object.entries(catalog).map(([k, v]) => (
                  <button key={k} type="button" className={"pl-type" + (type === k ? " on" : "")} onClick={() => pickType(k)} title={v.description}>
                    <b>{v.label}</b>{v.model && <span className="pl-node-model">model</span>}
                  </button>
                ))}
              </div>
            )}
            {cat && <div className="muted-note" style={{ marginTop: ".2rem" }}>{cat.description}</div>}
            {type && (
              <div className="pl-form">
                <ConfigForm type={type} c={config} set={setConfig} fields={fields} sources={sources}
                            refTables={view.ref_tables} srcTables={view.source_tables} sqlTypes={view.sql_types}
                            pipelineId={view.id} inputs={inputs} />
                {!editing && (
                  <Row label="Wiring" hint="Several wires into one step merge their records. You can rewire on the canvas.">
                    <select value={wiring} onChange={(e) => setWiring(e.target.value)}>
                      <option value="none">not wired yet: I'll wire it on the canvas{multi ? ` (works in ${outs.find((o) => o.id === owner)?.name}'s schema)` : ""}</option>
                      {outs.map((o) => <option key={o.id} value={endOf(o.id)}>at the end, feeding {multi ? o.name : "Output"}</option>)}
                      {steps.map((x) => <option key={x.id} value={`after:${x.id}`}>right after {x.id} ({catalog[x.type]?.label || x.type})</option>)}
                      {mode.before && <option value={`before:${mode.before}`}>right before {mode.before}</option>}
                    </select>
                  </Row>
                )}
                <Row label="Note" hint="Optional: why this step is here.">
                  <input value={note} onChange={(e) => setNote(e.target.value)} />
                </Row>
              </div>
            )}
            <div className="btn-row">
              <button className="primary" disabled={!type || saving} onClick={save}>{editing ? "Save to draft" : "Add to draft"}</button>
              <span className="assess" style={{ alignSelf: "center" }}>
                {editing ? "The change goes into the draft; it applies when you press Apply." : "It goes into the draft. Check, then Apply to use it."}
              </span>
            </div>
          </section>}
          {!askOnly && <div className="pl-modal-or"><span>or</span></div>}
          <section className="pl-modal-col">
            {!askOnly && <h5>Ask the agent</h5>}
            <div className="muted-note" style={{ marginTop: 0 }}>
              {askOnly ? "Your message goes to the chat; the agent's changes show on the canvas, ready for your approval."
                : "Describe what the step should do. The agent proposes it in the chat, checked against the schema and sources."}
            </div>
            <textarea rows={7} value={ask} autoFocus={askOnly} onChange={(e) => setAsk(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send(); }} />
            <div className="btn-row">
              <button className="primary" disabled={saving || !ask.trim()} onClick={send}>Send to the agent</button>
              <span className="assess" style={{ alignSelf: "center" }}>Ctrl+Enter sends</span>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
