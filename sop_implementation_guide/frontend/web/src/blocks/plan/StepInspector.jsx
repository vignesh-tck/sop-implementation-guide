import { ACTORS, ACTOR_KEYS, ActorChip, ActorIcon, ClockIcon } from "./actors.jsx";
import { numbers, wouldLoop } from "./graph.js";

// The selected step, beside the canvas: the activity, its description, what it
// depends on, its primary and secondary owner, and its potential duration.
// Read-only, it ends with whatever the agents found (`children`: the zoning
// verdict, the funding, the SME's question, sources). While editing it is the
// step's form, in the same order.
//
// `sources` (the playbook editor only) adds which agent or lookup fills the
// step in, folded away: it is set up once per playbook, not per plan.

// Suggestions for the secondary owner; anything can be typed.
const SECONDARY = [
  "SME reviews the agent's output",
  "SME reviews the result",
  "City planning staff",
  "City engineer",
  "Road owner",
  "City Council",
];

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

function DependsOn({ s, steps, onSelect }) {
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
  s, n, steps, mark, removed, editable, sources, onChange, onDelete, onAddAfter, onSelect, onClose, children,
}) {
  const kind = ACTORS[s.actor] ? s.actor : "other";

  if (!editable || removed) {
    return (
      <aside className={"step-inspector actor-" + kind} aria-label={`Step ${n}`}>
        <div className="si-head">
          <span className="si-step">Step {n}</span>
          <span className="spacer" />
          {onClose && <button type="button" className="si-close" onClick={onClose} aria-label="Close">✕</button>}
        </div>
        <h5 className="si-title">{s.title || <i>Untitled activity</i>}</h5>
        <Changes mark={removed ? { kind: "removed" } : mark} />
        {s.how && <p className="si-how">{s.how}</p>}
        <dl className="si-facts">
          {!removed && <div><dt>Depends on</dt><dd><DependsOn s={s} steps={steps} onSelect={onSelect} /></dd></div>}
          {s.condition && <div><dt>Only if</dt><dd>{s.condition}</dd></div>}
          <div><dt>Primary owner</dt><dd><ActorChip kind={kind} />{s.owner && <div>{s.owner}</div>}</dd></div>
          {s.secondary_owner && <div><dt>Secondary owner</dt><dd>{s.secondary_owner}</dd></div>}
          <div><dt><ClockIcon /> Duration</dt><dd>{s.time || "No set duration"}</dd></div>
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
        <span className="si-step">Step {n}</span>
        <span className="spacer" />
        {onClose && <button type="button" className="si-close" onClick={onClose} aria-label="Close">✕</button>}
      </div>
      <Changes mark={mark} />

      <label className="si-field">
        <span className="si-label"><span className="si-k">1</span>Activity</span>
        <textarea rows={2} value={s.title || ""} placeholder="What has to happen" autoFocus={!s.title}
                  onChange={(e) => onChange({ title: e.target.value })} />
      </label>

      <label className="si-field">
        <span className="si-label"><span className="si-k">2</span>Activity description</span>
        <textarea rows={3} value={s.how || ""} placeholder="How it gets done, in a sentence or two"
                  onChange={(e) => onChange({ how: e.target.value })} />
      </label>

      <div className="si-field">
        <span className="si-label"><span className="si-k">3</span>Depends on</span>
        <span className="si-hint">Tick the steps this one waits for, or drag between steps on the canvas.</span>
        <div className="si-deps">
          {others.length === 0 && <span className="assess">No other steps yet.</span>}
          {others.map((o) => {
            const loop = !deps.includes(o.id) && wouldLoop(steps, o.id, s.id);
            return (
              <label key={o.id} className={"si-dep" + (loop ? " off" : "")}
                     title={loop ? `Step ${num[o.id]} already waits on this one` : o.title}>
                <input type="checkbox" checked={deps.includes(o.id)} disabled={loop} onChange={() => toggleDep(o.id)} />
                <span className="si-dep-n">{num[o.id]}</span>
                <span className="si-dep-t">{o.title || "Untitled activity"}</span>
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
        <input className="si-cond" value={s.condition || ""} placeholder="Only if… (leave blank if it always happens)"
               aria-label="Only if" onChange={(e) => onChange({ condition: e.target.value })} />
      </div>

      <div className="si-field">
        <span className="si-label"><span className="si-k">4</span>Primary owner</span>
        <div className="actor-pick" role="radiogroup" aria-label="Primary owner">
          {ACTOR_KEYS.map((k) => (
            <button key={k} type="button" role="radio" aria-checked={kind === k} title={ACTORS[k].desc}
                    className={"actor-chip actor-" + k + (kind === k ? " on" : "")} onClick={() => onChange({ actor: k })}>
              <ActorIcon kind={k} size={14} />{ACTORS[k].label}
            </button>
          ))}
        </div>
        <input value={s.owner || ""} placeholder="Who exactly, e.g. Code enforcement" aria-label="Primary owner, exactly"
               onChange={(e) => onChange({ owner: e.target.value })} />
      </div>

      <label className="si-field">
        <span className="si-label"><span className="si-k">5</span>Secondary owner</span>
        <input value={s.secondary_owner || ""} list="si-secondary" placeholder="e.g. SME reviews the agent's output"
               onChange={(e) => onChange({ secondary_owner: e.target.value })} />
        <datalist id="si-secondary">{SECONDARY.map((v) => <option key={v} value={v} />)}</datalist>
      </label>

      <label className="si-field">
        <span className="si-label"><span className="si-k">6</span>Potential duration</span>
        <input value={s.time || ""} placeholder="e.g. 2–4 wk, 1–3 mo, Program deadline" onChange={(e) => onChange({ time: e.target.value })} />
      </label>

      {sources && (
        <details className="si-advanced">
          <summary>Filled in by: {sources[s.filled_by || "template"]?.label || s.filled_by}</summary>
          <select value={s.filled_by || "template"} onChange={(e) => onChange({ filled_by: e.target.value })}>
            {Object.entries(sources).map(([k, src]) => (
              <option key={k} value={k}>{src.label}{src.live ? "" : " (not built yet: the SME fills it in)"}</option>
            ))}
          </select>
          <p className="assess">{sources[s.filled_by || "template"]?.note}</p>
        </details>
      )}

      <div className="btn-row">
        <button type="button" onClick={onAddAfter}>+ Step after this</button>
        <span className="spacer" />
        <button type="button" className="danger" onClick={onDelete}>Remove step</button>
      </div>
    </aside>
  );
}
