import { useState } from "react";

// Where a general pipeline's output is written when it is published: a table
// of its own in Supabase (gp_…), created on the first publish, with a column
// per field. Merge updates rows with the same key and keeps the rest; replace
// swaps this pipeline's earlier rows for the run's. Read at publish only, so
// changing it resets no step.

export default function WriteTarget({ view, act, disabled }) {
  const w = view.write;
  const [draft, setDraft] = useState(null);     // the table name while it is being typed
  const [saving, setSaving] = useState(false);
  if (!w) return null;
  const table = draft ?? w.table;
  const fields = view.definition.schema?.fields || [];
  const keys = new Set(w.key || []);

  async function save(change) {
    setSaving(true);
    await act("PUT", "/write", change);
    setSaving(false);
  }
  const off = disabled || saving;
  const newCols = w.columns.filter((c) => c.state === "new");

  return (
    <div className="pl-write">
      <label className="pl-write-row">
        <span>Table</span>
        <input value={table} disabled={off} spellCheck={false} aria-label="Table name"
               onChange={(e) => setDraft(e.target.value)}
               onBlur={async () => {
                 if (table.trim() && table.trim() !== w.table) await save({ table: table.trim() });
                 setDraft(null);
               }}
               onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()} />
        <span className={"tag" + (w.exists ? " ok" : "")}>{w.exists ? "exists" : "new: created on publish"}</span>
      </label>
      <div className="pl-write-row">
        <span>Mode</span>
        <select value={w.mode} disabled={off} aria-label="Write mode" onChange={(e) => save({ mode: e.target.value })}>
          <option value="merge">merge</option>
          <option value="replace">replace</option>
        </select>
        <span className="assess">{w.modes?.[w.mode]}</span>
      </div>
      <div className="pl-write-row">
        <span>Key</span>
        <div className="pl-checks">
          {fields.map((f) => (
            <label key={f.name}>
              <input type="checkbox" checked={keys.has(f.name)} disabled={off || (keys.has(f.name) && keys.size === 1)}
                     onChange={(e) => save({ key: fields.map((x) => x.name).filter((n) => (n === f.name ? e.target.checked : keys.has(n))) })} />
              <code>{f.name}</code>
            </label>
          ))}
        </div>
      </div>
      <div className="assess">
        The key fields identify a row, so they must be filled and unique on every record.
        {!w.saved && " These are the defaults: change any of them."}
      </div>
      {fields.length > 0 && (
        <div className="assess">
          {w.exists
            ? (newCols.length ? <>Publishing adds {newCols.length === 1 ? "column" : "columns"} {newCols.map((c) => <code key={c.name} style={{ marginRight: ".3rem" }}>{c.name}</code>)}.</>
              : "Every field already has its column.")
            : `Publishing creates it with ${w.columns.length} column${w.columns.length === 1 ? "" : "s"}, one per field, plus each row's key, record, source and evidence.`}
          {w.other_columns?.length > 0 && ` Columns this output doesn’t fill are left as they are (${w.other_columns.join(", ")}).`}
        </div>
      )}
      {w.problems.map((p, i) => <div key={i} className="pl-warn">{p}</div>)}
      <div className="assess">General pipelines write only to their own gp_ tables; the app’s tables are read only to them.</div>
    </div>
  );
}
