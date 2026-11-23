// The recommendation types routed to one playbook, under its canvas.
//
// Each type has a playbook picker to move it. The assistant's suggested moves
// (from Check fit or the chat) show under the type they touch: a move out of
// this playbook ("Move to Streetscape? No step here covers sign permits.") or
// a type suggested into it. Each is one decision: accept or keep things as
// they are.

export default function RecTypes({ pathway, draft, preview, pending, unmapped, fit, onCheck, onMap, onAccept, onReject }) {
  const key = pathway.key;
  const name = (k) => preview.pathways.find((p) => p.key === k)?.name || k;
  const isDraft = draft.pathways.some((p) => p.key === key);
  const here = Object.keys(draft.rec_types).filter((l) => draft.rec_types[l] === key);
  const moves = pending.filter((o) => o.kind === "map_rec");
  const incoming = moves.filter((o) => o.to === key && o.from !== key);
  const labels = [...new Set([...here, ...incoming.map((o) => o.label)])].sort();
  const count = isDraft ? here.length : labels.length;
  const myFit = fit?.key === key ? fit : null;

  return (
    <section className="rec-types">
      <div className="rec-types-head">
        <h4>Recommendation types in this playbook</h4>
        <span className="rec-count">{count} type{count === 1 ? "" : "s"}</span>
        <span className="spacer" />
        <button type="button" className="ai" onClick={onCheck} disabled={!isDraft || myFit?.busy || (!here.length && !unmapped.length)}
                title="Ask the assistant whether each type belongs in this playbook, and whether any unmapped type does">
          {myFit?.busy ? <><span className="pl-spin" /> Checking…</> : "✦ Check fit"}
        </button>
      </div>
      <p className="rec-types-sub">
        Each recommendation type is routed to one playbook. Change the playbook to move it.
        {myFit && !myFit.busy && !myFit.error && (
          <span className="rec-fit-note">
            {myFit.moves ? ` ✦ ${myFit.moves} suggestion${myFit.moves > 1 ? "s" : ""} below.` : ` ✦ All ${myFit.checked} types fit here.`}
          </span>
        )}
      </p>
      {myFit?.error && <div className="banner err">Check fit failed — {myFit.error}</div>}

      <ul className="pathway-recs">
        {labels.map((label) => {
          const mine = draft.rec_types[label] === key;
          const out = mine && moves.find((o) => o.label === label && o.from === key);
          const into = !mine && incoming.find((o) => o.label === label);
          return (
            <li key={label} data-rec={label}>
              <div className="rec-row">
                <span className="rec-label">
                  {label}
                  {into && <span className="pc-flag sugg">✦ Suggested</span>}
                </span>
                <select value={mine ? key : ""} disabled={!mine} onChange={(e) => onMap(label, e.target.value)} title="Move to another playbook">
                  {!mine && <option value="">{into.from ? name(into.from) : "No playbook"}</option>}
                  {preview.pathways.map((p) => <option key={p.key} value={p.key}>{p.name || p.key}</option>)}
                  {mine && <option value="">No playbook</option>}
                </select>
              </div>
              {out && (
                <div className="rec-sugg">
                  <div>
                    ✦ {out.to ? <>Move to <b>{name(out.to)}</b>?</> : <>Take out of every playbook?</>}
                    {out.reason && <> {out.reason}</>}
                  </div>
                  <div className="rec-sugg-act">
                    <button type="button" className="primary" onClick={() => onAccept(out)}>{out.to ? "Move" : "Take out"}</button>
                    <button type="button" onClick={() => onReject(out)}>Keep here</button>
                  </div>
                </div>
              )}
              {into && (
                <div className="rec-sugg">
                  <div>
                    ✦ Add here{into.from ? <> from <b>{name(into.from)}</b></> : null}?
                    {into.reason && <> {into.reason}</>}
                  </div>
                  <div className="rec-sugg-act">
                    <button type="button" className="primary" onClick={() => onAccept(into)}>Add here</button>
                    <button type="button" onClick={() => onReject(into)}>{into.from ? `Keep in ${name(into.from)}` : "Leave unmapped"}</button>
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {!labels.length && <p className="assess">No recommendation types are routed here yet.</p>}
      {unmapped.length > 0 && isDraft && (
        <select className="rec-add" value="" onChange={(e) => e.target.value && onMap(e.target.value, key)}>
          <option value="">+ Add an unmapped recommendation type…</option>
          {unmapped.map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
      )}
    </section>
  );
}
