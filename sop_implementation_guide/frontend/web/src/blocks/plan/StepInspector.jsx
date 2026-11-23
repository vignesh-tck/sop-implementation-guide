import { ACTORS, ACTOR_KEYS, ActorChip, ActorIcon, ClockIcon } from "./actors.jsx";
import { numbers, wouldLoop } from "./graph.js";

// The selected step, beside the canvas. Read-only, it leads with who and what,
// then how, how long and what it waits on, then whatever the agents found
// (`children`: the zoning verdict, the funding, the SME's question, sources).
// While editing it is the step's form.

function Changes({ mark }) {
  if (!mark) return null;
  if (mark.kind === "new") return <div className="si-change new">New in this draft</div>;
  if (mark.kind === "removed") return <div className="si-change gone">Removed in this draft</div>;
  return (
    <div className="si-change changed">
      <b>Changed in this draft</b>
      <ul>
        {mark.fields.map((f) => (
          <li key={f.field}>
            {f.label}: <del>{f.before || "—"}</del> → <ins>{f.after || "—"}</ins>
          </li>
        ))}
      </ul>
    </div>
  );
}

function After({ s, steps, onSelect }) {
  const num = numbers(steps);
  const deps = (s.depends_on || []).map((d) => steps.find((x) => x.id === d)).filter(Boolean)
    .sort((a, b) => num[a.id] - num[b.id]);
  if (!deps.length) return <span>Nothing: this step can start first</span>;
  return (
    <span>
      {deps.length > 1 && (s.depends_mode === "any" ? "Whichever is done first: " : "All of: ")}
      {deps.map((d, k) => (
        <span key={d.id}>
          {k > 0 && (s.depends_mode === "any" ? " or " : ", ")}
          <button type="button" className="link" onClick={() => onSelect(d.id)} title={d.title}>step {num[d.id]}</button>
        </span>
      ))}
    </span>
  );
}

export default function StepInspector({
  s, n, steps, mark, removed, editable, onChange, onDelete, onAddAfter, onSelect, onClose, children,
}) {
  const kind = ACTORS[s.actor] ? s.actor : "other";

  if (!editable || removed) {
    return (
      <aside className={"step-inspector actor-" + kind} aria-label={`Step ${n}`}>
        <div className="si-head">
          <ActorChip kind={kind} owner={s.owner} />
          <span className="spacer" />
          <span className="kv">Step {n}</span>
          {onClose && <button type="button" className="si-close" onClick={onClose} aria-label="Close">✕</button>}
        </div>
        <h5 className="si-title">{s.title || <i>Untitled step</i>}</h5>
        <Changes mark={removed ? { kind: "removed" } : mark} />
        {s.how && <p className="si-how">{s.how}</p>}
        <dl className="si-facts">
          <div><dt><ClockIcon /> Time</dt><dd>{s.time || "No set time"}</dd></div>
          {!removed && <div><dt>Waits on</dt><dd><After s={s} steps={steps} onSelect={onSelect} /></dd></div>}
          {s.condition && <div><dt>Only if</dt><dd>{s.condition}</dd></div>}
        </dl>
        {children}
      </aside>
    );
  }

  const num = numbers(steps);
  const others = steps.filter((x) => x.id !== s.id);
  const deps = s.depends_on || [];
  const toggleDep = (id) => {
    const next = deps.includes(id) ? deps.filter((d) => d !== id) : [...deps, id];
    onChange({ depends_on: next, ...(next.length < 2 ? { depends_mode: "all" } : {}) });
  };

  return (
    <aside className={"step-inspector editing actor-" + kind} aria-label={`Edit step ${n}`}>
      <div className="si-head">
        <b>Step {n}</b>
        <span className="spacer" />
        {onClose && <button type="button" className="si-close" onClick={onClose} aria-label="Close">✕</button>}
      </div>
      <Changes mark={mark} />
      <label className="si-field">
        Task
        <textarea rows={2} value={s.title || ""} placeholder="What has to happen" onChange={(e) => onChange({ title: e.target.value })} />
      </label>
      <label className="si-field">
        How it is done
        <textarea rows={3} value={s.how || ""} placeholder="How it gets done, in a sentence or two"
                  onChange={(e) => onChange({ how: e.target.value })} />
      </label>
      <div className="si-field">
        Who
        <div className="actor-pick" role="radiogroup" aria-label="Who does this step">
          {ACTOR_KEYS.map((k) => (
            <button key={k} type="button" role="radio" aria-checked={kind === k} title={ACTORS[k].desc}
                    className={"actor-chip actor-" + k + (kind === k ? " on" : "")} onClick={() => onChange({ actor: k })}>
              <ActorIcon kind={k} size={14} />{ACTORS[k].label}
            </button>
          ))}
        </div>
      </div>
      <div className="si-row">
        <label className="si-field">
          Who exactly
          <input value={s.owner || ""} placeholder="e.g. Code enforcement" onChange={(e) => onChange({ owner: e.target.value })} />
        </label>
        <label className="si-field">
          Time
          <input value={s.time || ""} placeholder="e.g. 1–3 mo" onChange={(e) => onChange({ time: e.target.value })} />
        </label>
      </div>
      <div className="si-field">
        Waits on
        <div className="si-deps">
          {others.length === 0 && <span className="assess">No other steps yet.</span>}
          {others.map((o) => {
            const loop = !deps.includes(o.id) && wouldLoop(steps, o.id, s.id);
            return (
              <label key={o.id} className={"si-dep" + (loop ? " off" : "")}
                     title={loop ? `Step ${num[o.id]} already waits on this one` : o.title}>
                <input type="checkbox" checked={deps.includes(o.id)} disabled={loop} onChange={() => toggleDep(o.id)} />
                <span className="si-dep-n">{num[o.id]}</span>
                <span className="si-dep-t">{o.title || "Untitled step"}</span>
              </label>
            );
          })}
        </div>
        {deps.length > 1 && (
          <div className="si-mode">
            <label><input type="radio" checked={s.depends_mode !== "any"} onChange={() => onChange({ depends_mode: "all" })} /> after all of them</label>
            <label><input type="radio" checked={s.depends_mode === "any"} onChange={() => onChange({ depends_mode: "any" })} /> after whichever is first</label>
          </div>
        )}
      </div>
      <label className="si-field">
        Only if
        <input value={s.condition || ""} placeholder="Leave blank if it always happens"
               onChange={(e) => onChange({ condition: e.target.value })} />
      </label>
      <div className="btn-row">
        <button type="button" onClick={onAddAfter}>+ Step after this</button>
        <span className="spacer" />
        <button type="button" className="danger" onClick={onDelete}>Remove step</button>
      </div>
    </aside>
  );
}
