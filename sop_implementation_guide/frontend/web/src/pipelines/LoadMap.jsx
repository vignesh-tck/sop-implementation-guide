import { useState } from "react";

// How the output's fields map onto the catalogue's columns at Load. The output
// can be any schema: each column's field is proposed from the field names and
// the planner confirms or changes it here. Only a programme name is needed;
// fields mapped to no column travel with the programme and Block Funding reads
// them too. Read at Load only, so changing it resets no step.

export default function LoadMap({ view, act, disabled }) {
  const [saving, setSaving] = useState(null);
  const lm = view.load_map;
  if (!lm) return null;
  const fields = view.definition.schema?.fields || [];
  const owner = new Map(lm.columns.filter((c) => c.field).map((c) => [c.field, c.column]));
  const fits = (c, f) => !c.types || c.types.includes(f.type);

  async function pick(col, field) {
    setSaving(col);
    // A field already on another column moves here.
    const from = field && owner.get(field) !== col ? owner.get(field) : null;
    await act("PUT", "/load-map", { mapping: { [col]: field || null, ...(from ? { [from]: null } : {}) } });
    setSaving(null);
  }

  const mapped = lm.columns.filter((c) => c.field).length;
  return (
    <div className="pl-loadmap">
      <div className="assess">
        {mapped} of {lm.columns.length} catalogue columns mapped. Block Funding judges each programme on these, so unmapped
        agency, eligibility or reach means a thinner match; it never stops the load.
      </div>
      <table className="pl-loadmap-table">
        <thead><tr><th>Catalogue column</th><th>From field</th><th>Used for</th></tr></thead>
        <tbody>
          {lm.columns.map((c) => (
            <tr key={c.column} className={c.required && !c.field ? "pl-bad" : ""}>
              <td><code>{c.column}</code>{c.required && <span className="pl-req"> required</span>}</td>
              <td>
                <select value={c.field || ""} disabled={disabled || !!saving} aria-label={`Field for ${c.column}`}
                        onChange={(e) => pick(c.column, e.target.value)}>
                  <option value="">none</option>
                  {fields.filter((f) => fits(c, f)).map((f) => (
                    <option key={f.name} value={f.name}>
                      {f.name}{owner.has(f.name) && owner.get(f.name) !== c.column ? ` (now ${owner.get(f.name)})` : ""}
                    </option>
                  ))}
                </select>
                {c.auto && c.field && <span className="tag" title="Proposed from the field name; pick another to change it"> proposed</span>}
                {c.types && <span className="assess"> {c.types.join(" or ")}</span>}
              </td>
              <td className="assess">{c.use}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {lm.extras.length > 0 && (
        <div className="assess">
          Kept with each programme and read by Block Funding: {lm.extras.map((n) => <code key={n} style={{ marginRight: ".3rem" }}>{n}</code>)}
        </div>
      )}
      {lm.notes.map((n, i) => <div key={i} className="pl-warn">{n}</div>)}
    </div>
  );
}
