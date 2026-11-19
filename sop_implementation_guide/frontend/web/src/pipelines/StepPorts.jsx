// A step's ports: the records and schema it receives (its inputs' output,
// merged when there are several), and what it hands on. Every step works on
// the same target schema, so the ports show, per field, how many records have
// a value going in and coming out, and which fields this step writes. Counts
// come from the last preview; a step not previewed with its current settings
// shows "—". A draft step has no records yet: its ports show the schema the
// check works out instead (whether each field is produced, and parsed).

import { STATE_TEXT, draftPortText, portText, stepPorts } from "./ports.js";

// Records with a value for `name`, summed over outputs (a merge), or null if any is missing.
function filled(outs, name) {
  if (!outs.length || outs.some((o) => !o || o.error || !o.coverage?.fields?.[name])) return null;
  return { n: outs.reduce((a, o) => a + o.coverage.fields[name].filled, 0), of: outs.reduce((a, o) => a + o.count, 0) };
}

const Count = ({ c }) => (c ? <span title={`${c.of - c.n} blank`}>{c.n}/{c.of}</span> : <span className="assess">—</span>);
const State = ({ s }) => (s ? <span className={"pl-state " + s}>{STATE_TEXT[s]}</span> : <span className="assess">not produced</span>);

function SqlTables({ view, tables }) {
  return (
    <div className="pl-port-tables">
      <b>Tables the SQL can read</b>
      <ul>
        <li><code>records</code>: <span className="assess">id and the fields above, typed as shown</span></li>
        <li><code>records_text</code>: <span className="assess">the same columns, all as text (for values not parsed yet)</span></li>
        {tables.map((name) => {
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
  );
}

function Heads({ t }) {
  return (
    <>
      {t.error && <div className="banner err" style={{ margin: ".4rem 0" }}>{t.error}</div>}
      <div className="pl-port-heads">
        <div className="pl-port-head in"><span className="pl-port-dot" /> <b>Input</b> <span className="assess">{t.input}</span></div>
        <div className="pl-port-head out"><span className="pl-port-dot" /> <b>Output</b> <span className="assess">{t.output}</span></div>
      </div>
    </>
  );
}

function Writes({ w, sql }) {
  return w ? <span className="tag edited" title={sql ? "Returned by the SQL" : "This step may write it"}>writes</span>
    : <span className="assess" title="Passed through unchanged">→</span>;
}

export function DraftPorts({ view, stepId }) {
  const t = draftPortText(view, stepId);
  const step = (view.draft?.steps || []).find((s) => s.id === stepId);
  if (!step) return null;
  const fields = view.definition.schema?.fields || [];
  const writes = new Set(t.writes);
  return (
    <details className="pl-ports" open>
      <summary><b>Ports</b> <span className="assess">schema in → out, as the check works it out</span></summary>
      <Heads t={t} />
      <div className="table-wrap">
        <table className="pl-port-table">
          <thead><tr><th>Field</th><th>Type</th><th>In</th><th></th><th>Out</th></tr></thead>
          <tbody>
            {fields.map((f) => (
              <tr key={f.name} className={writes.has(f.name) ? "writes" : ""}>
                <td><code>{f.name}</code>{f.required ? "*" : ""}</td>
                <td className="assess">{f.type}</td>
                <td>{t.inSchema ? <State s={t.inSchema[f.name]} /> : <span className="assess">—</span>}</td>
                <td className="pl-port-mark"><Writes w={writes.has(f.name)} sql={step.type === "sql"} /></td>
                <td>{t.schema ? <State s={t.schema[f.name]} /> : <span className="assess">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {step.type === "sql" && <SqlTables view={view} tables={t.tables} />}
    </details>
  );
}

export default function StepPorts({ view, stepId }) {
  const p = stepPorts(view, stepId);
  const t = portText(view, stepId);
  if (!p.step) return null;
  const fields = view.definition.schema?.fields || [];
  const writes = new Set(t.writes);
  const sqlType = p.step.type === "sql" ? view.sql_types || {} : null;
  const ins = p.inputs.map(([, o]) => o);
  return (
    <details className="pl-ports" open>
      <summary><b>Ports</b> <span className="assess">input → output schema</span></summary>
      <Heads t={t} />
      <div className="table-wrap">
        <table className="pl-port-table">
          <thead><tr><th>Field</th><th>Type</th><th title="Records with a value coming in">In</th><th></th><th title="Records with a value going out">Out</th></tr></thead>
          <tbody>
            {sqlType && <tr className="pl-port-id"><td><code>id</code></td><td className="assess">VARCHAR</td><td className="assess" colSpan={3}>record key: maps each result row back to its record</td></tr>}
            {fields.map((f) => {
              const inC = ins.length ? filled(ins, f.name) : null;
              const outC = filled([p.output], f.name);
              const delta = outC && (inC || !ins.length) ? outC.n - (inC ? inC.n : 0) : null;
              return (
                <tr key={f.name} className={writes.has(f.name) ? "writes" : ""}>
                  <td><code>{f.name}</code>{f.required ? "*" : ""}</td>
                  <td className="assess">{f.type}{sqlType ? ` · ${sqlType[f.type]}` : ""}</td>
                  <td>{ins.length ? <Count c={inC} /> : <span className="assess">—</span>}</td>
                  <td className="pl-port-mark"><Writes w={writes.has(f.name)} sql={!!sqlType} /></td>
                  <td><Count c={outC} />
                    {delta ? <span className={"pl-delta" + (delta < 0 ? " neg" : "")}> {delta > 0 ? "+" : ""}{delta}</span> : null}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {p.step.type === "sql" && <SqlTables view={view} tables={t.tables} />}
    </details>
  );
}
