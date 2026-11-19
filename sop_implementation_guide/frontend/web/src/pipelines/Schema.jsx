// The target schema, shown on the Output node: the fields every record must
// contain. Required fields gate publishing; steps are designed only after the
// schema is approved. The agent proposes fields from the chat; the planner can
// load the defaults, accept blanks for a required field, and approve.

export default function Schema({ view, act, disabled }) {
  const schema = view.definition.schema || {};
  const fields = schema.fields || [];
  const cov = view.last_run?.coverage?.fields || {};
  const total = view.last_run?.coverage?.records;
  const approved = schema.status === "approved";

  if (!fields.length) {
    return (
      <div className="sch">
        <div className="assess">No fields yet. Describe the fields you need to the agent, or start from the defaults.</div>
        <button disabled={disabled} onClick={() => act("POST", "/schema/default")}>
          Use default {view.kind_label.toLowerCase()} fields</button>
      </div>
    );
  }
  return (
    <div className="sch">
      <table className="sch-table">
        <thead><tr><th>Field</th><th>Type</th><th>Filled</th><th title="Publish even though some records leave this required field blank">Blanks ok</th></tr></thead>
        <tbody>
          {fields.map((f) => {
            const c = cov[f.name];
            const gap = f.required && c && c.missing > 0 && !f.accept_blank;
            return (
              <tr key={f.name} className={gap ? "gap" : ""}>
                <td title={f.description}>
                  <code>{f.name}</code>{f.required && <span className="sch-req">*</span>}
                  {f.extract === false && <span className="sch-judge" title="A judgement, not a stated fact: a model step fills it, marked as model judgement">judgement</span>}
                </td>
                <td className="assess">{f.type}{f.enum_values ? ` (${f.enum_values.length})` : ""}</td>
                <td className="assess">{c ? `${c.filled}/${total}` : "—"}</td>
                <td>{f.required && (
                  <input type="checkbox" checked={!!f.accept_blank} disabled={disabled} aria-label={`Accept blanks for ${f.name}`}
                         onChange={(e) => act("POST", `/fields/${f.name}/accept-blank`, { accept: e.target.checked })} />
                )}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="assess">* required. Hover a field for its description; ask the agent to add, rename or change fields.</div>
      {!approved && (
        <button className="primary" disabled={disabled} onClick={() => act("POST", "/schema/approve")}>Approve schema</button>
      )}
    </div>
  );
}
