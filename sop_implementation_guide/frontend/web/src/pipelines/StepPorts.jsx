// A step's ports: the records and schema it receives (the previous step's
// output), and what it hands on. Every step works on the same target schema,
// so the ports show, per field, how many records have a value going in and
// coming out, and which fields this step writes. Counts come from the last
// preview; a step not previewed with its current settings shows "—".

import { portText, stepPorts } from "./ports.js";

function Filled({ o, name }) {
  const c = o?.coverage?.fields?.[name];
  if (!o || o.error || !c) return <span className="assess">—</span>;
  return <span title={`${c.missing} blank`}>{c.filled}/{o.count}</span>;
}

export default function StepPorts({ view, stepId }) {
  const p = stepPorts(view, stepId);
  const t = portText(view, stepId);
  if (!p.step) return null;
  const fields = view.definition.schema?.fields || [];
  const writes = new Set(t.writes);
  const sqlType = p.step.type === "sql" ? view.sql_types || {} : null;
  const outCols = p.io.columns ? new Set(p.io.columns.map((c) => c.name)) : null;
  return (
    <details className="pl-ports" open>
      <summary><b>Ports</b> <span className="assess">input → output schema</span></summary>
      {t.error && <div className="banner err" style={{ margin: ".4rem 0" }}>{t.error}</div>}
      <div className="pl-port-heads">
        <div className="pl-port-head in"><span className="pl-port-dot" /> <b>Input</b> <span className="assess">{t.input}</span></div>
        <div className="pl-port-head out"><span className="pl-port-dot" /> <b>Output</b> <span className="assess">{t.output}</span></div>
      </div>
      <div className="table-wrap">
        <table className="pl-port-table">
          <thead><tr><th>Field</th><th>Type</th><th title="Records with a value coming in">In</th><th></th><th title="Records with a value going out">Out</th></tr></thead>
          <tbody>
            {sqlType && <tr className="pl-port-id"><td><code>id</code></td><td className="assess">VARCHAR</td><td className="assess" colSpan={3}>record key: maps each result row back to its record</td></tr>}
            {fields.map((f) => {
              const w = writes.has(f.name);
              const inC = p.input?.coverage?.fields?.[f.name]?.filled;
              const outC = p.output?.coverage?.fields?.[f.name]?.filled;
              const delta = inC != null && outC != null ? outC - inC : p.prev ? null : outC;
              return (
                <tr key={f.name} className={w ? "writes" : ""}>
                  <td><code>{f.name}</code>{f.required ? "*" : ""}</td>
                  <td className="assess">{f.type}{sqlType ? ` · ${sqlType[f.type]}` : ""}</td>
                  <td>{p.prev ? <Filled o={p.input} name={f.name} /> : <span className="assess">—</span>}</td>
                  <td className="pl-port-mark">
                    {w ? <span className="tag edited" title={outCols ? "Returned by the SQL" : "This step may write it"}>writes</span>
                      : <span className="assess" title="Passed through unchanged">→</span>}
                  </td>
                  <td><Filled o={p.output} name={f.name} />
                    {delta ? <span className={"pl-delta" + (delta < 0 ? " neg" : "")}> {delta > 0 ? "+" : ""}{delta}</span> : null}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {p.step.type === "sql" && (
        <div className="pl-port-tables">
          <b>Tables the SQL can read</b>
          <ul>
            <li><code>records</code>: <span className="assess">id and the fields above, typed as shown</span></li>
            <li><code>records_text</code>: <span className="assess">the same columns, all as text (for values not parsed yet)</span></li>
            {t.tables.map((name) => {
              const ref = view.ref_tables?.[name];
              return (
                <li key={name}><code>{name}</code> <span className="tag">reference, read only</span>
                  <div className="assess">{ref?.description}</div>
                  <div className="pl-cols">{Object.entries(ref?.columns || {}).map(([c, ty]) => <span key={c}><code>{c}</code> {ty}</span>)}</div>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </details>
  );
}
