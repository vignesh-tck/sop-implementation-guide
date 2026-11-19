import { useState } from "react";
import LoadMap from "./LoadMap.jsx";
import StepPorts, { DraftPorts } from "./StepPorts.jsx";
import { ancestorsOf, stepsModel } from "./ports.js";
import { attention } from "./records.js";

// The selected node, on the right of the canvas: what it does, its instructions
// (a model step's one editable part), what it reads and fills, its settings,
// and what to do next with it. Model steps' fixed rules are shown, locked.

const SLOT = { extract: "instructions", research: "instructions", llm_fill: "instruction", geo_tie: "instruction" };
const DRAFT_TEXT = { new: "New in draft", changed: "Draft change" };

function Section({ title, children }) {
  if (children == null || children === false || (Array.isArray(children) && !children.some(Boolean))) return null;
  return <section className="ins-sec"><h6>{title}</h6>{children}</section>;
}

function Chips({ items, onPick }) {
  if (!items.length) return <span className="assess">none</span>;
  return (
    <div className="ins-chips">
      {items.map(([id, text, node]) => (
        node && onPick ? <button key={id} type="button" className="ins-chip link" onClick={() => onPick(node)}>{text}</button>
          : <span key={id} className="ins-chip"><code>{text}</code></span>
      ))}
    </div>
  );
}

function Toggle({ on, locked, disabled, onChange, children }) {
  return (
    <label className={"ins-toggle" + (locked ? " locked" : "")} title={locked ? "Part of the fixed rules: every value must be checked against its source" : ""}>
      <input type="checkbox" role="switch" checked={!!on} disabled={locked || disabled} onChange={(e) => onChange?.(e.target.checked)} />
      <span className="ins-switch" aria-hidden="true" />
      <span>{children}{locked && <span className="assess"> · always on</span>}</span>
    </label>
  );
}

function Instructions({ view, step, live, act, disabled }) {
  const slot = SLOT[step.type];
  const saved = step.config?.[slot] || "";
  const [text, setText] = useState(null);       // null: not edited
  const prompt = live ? view.prompts?.[step.id] : null;
  const value = text ?? saved;
  const dirty = text !== null && text.trim() !== saved.trim();
  const toDraft = !live || !!view.draft;
  const save = async () => {
    const config = { ...step.config, [slot]: value.trim() || null };
    const r = await act("PATCH", toDraft ? `/draft/steps/${step.id}` : `/steps/${step.id}`, { config });
    if (r) setText(null);
  };
  return (
    <Section title="Instructions">
      <textarea className="ins-text" rows={Math.min(12, Math.max(4, value.split("\n").length + 1))} value={value}
                disabled={disabled} placeholder={prompt?.default || "None: the step follows its fixed rules and the pipeline goal."}
                onChange={(e) => setText(e.target.value)} />
      {dirty && (
        <div className="ins-row">
          <button className="primary" disabled={disabled} onClick={save}>Save instructions</button>
          <button onClick={() => setText(null)}>Cancel</button>
          <span className="assess">{toDraft ? "Goes into the draft." : `${step.id} and the steps after it go back for approval.`}</span>
        </div>
      )}
      {prompt?.prompts?.length > 0 && (
        <details className="ins-prompt">
          <summary>The full prompt (fixed rules locked)</summary>
          {prompt.prompts.map((pr, i) => (
            <pre key={i} className="pl-prompt-text">{pr.before}<mark className="pl-slot">{prompt.value || prompt.default || "(your instructions)"}</mark>{pr.after}</pre>
          ))}
        </details>
      )}
    </Section>
  );
}

function StepSettings({ step, setConfig, disabled }) {
  const c = step.config || {};
  if (step.type === "research") return (
    <Section title="Settings">
      <Toggle on={c.web_search} disabled={disabled} onChange={(v) => setConfig({ web_search: v })}>Search the web when sources link nothing useful</Toggle>
      <Toggle on locked>Discard values without a word-for-word quote</Toggle>
      <label className="ins-inline">Pages per record
        <select value={c.max_pages_per_record || 2} disabled={disabled} onChange={(e) => setConfig({ max_pages_per_record: Number(e.target.value) })}>
          {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
      </label>
    </Section>
  );
  if (step.type === "extract") return (
    <Section title="Settings">
      <Toggle on locked>Every value quotes the source word for word, checked</Toggle>
      <Toggle on locked>Fields the source does not state stay blank</Toggle>
    </Section>
  );
  if (step.type === "llm_fill") return (
    <Section title="Settings">
      <Toggle on={c.only_if_blank !== false} disabled={disabled} onChange={(v) => setConfig({ only_if_blank: v })}>Only fill blanks</Toggle>
      <Toggle on locked>Values are marked as model judgements</Toggle>
    </Section>
  );
  if (step.type === "geo_tie") return (
    <Section title="Settings"><Toggle on locked>Allowed values stay fixed (federal, state:MD, county, tract, unknown)</Toggle></Section>
  );
  if (step.type === "sql") return (
    <Section title="SQL">
      <pre className="pl-sql pl-sql-small">{c.query}</pre>
      {c.tables?.length > 0 && <div className="assess">Joins {c.tables.join(", ")}</div>}
    </Section>
  );
  const entries = Object.entries(c).filter(([, v]) => v != null && v !== "");
  return (
    <Section title="Settings">
      <table className="pl-kv"><tbody>
        {entries.map(([k, v]) => <tr key={k}><td><code>{k}</code></td><td>{typeof v === "object" ? <pre>{JSON.stringify(v, null, 2)}</pre> : String(v)}</td></tr>)}
      </tbody></table>
    </Section>
  );
}

function StepInspector({ view, act, disabled, step, model, onClose, onSelect, onEditStep, onShowRecords }) {
  const cat = view.catalog[step.type] || {};
  const live = !model.draft;
  const liveStep = (view.definition.steps || []).find((s) => s.id === step.id);
  const out = view.outputs[step.id];
  const ins = model.inputs[step.id] || [];
  const io = (model.draft ? view.draft?.ports : view.ports)?.[step.id] || {};
  const check = view.draft?.check;
  const errs = model.draft ? (check?.errors || []).filter((e) => e.node === step.id) : [];
  const warns = model.draft ? (check?.warnings || []).filter((e) => e.node === step.id) : [];
  const before = ancestorsOf(model, step.id);
  const blocked = model.steps.filter((x) => before.has(x.id) && (view.definition.steps || []).find((y) => y.id === x.id)?.status !== "approved").map((x) => x.id);
  const fields = view.definition.schema?.fields || [];
  const needLook = (out?.records || []).filter((r) => attention(r, fields).length).length;
  const feedsAlone = model.outputFrom.length === 1 && model.outputFrom[0] === step.id;
  const setConfig = (patch) => act("PATCH", `/draft/steps/${step.id}`, { config: { ...step.config, ...patch } });
  const sources = step.type === "extract" ? (step.config?.source_ids?.length ? step.config.source_ids : model.sources.map((s) => s.id)) : [];
  const reads = [
    ...ins.map((i) => [i, `${i} ${view.catalog[model.steps.find((x) => x.id === i)?.type]?.label || ""}`, `step:${i}`]),
    ...sources.map((s) => [s, s, `src:${s}`]),
    ...(step.config?.tables || []).map((t) => [t, `${t} (table)`, model.sources.some((s) => s.id === t) ? `src:${t}` : null]),
  ];
  let badge = null;
  if (!live && DRAFT_TEXT[step.draft_state]) badge = <span className="ins-badge draft">{DRAFT_TEXT[step.draft_state]}</span>;
  else if (liveStep?.status === "approved") badge = <span className="ins-badge ok">Approved</span>;
  else if (out?.error) badge = <span className="ins-badge bad">Failed</span>;
  else if (liveStep) badge = <span className="ins-badge pending">Needs approval</span>;

  const preview = () => act("POST", "/run", feedsAlone ? {} : { upto: step.id });
  const remove = () => {
    if (live && !window.confirm(`Remove ${step.id}? This opens a draft and removes it there; nothing changes until you Apply.`)) return;
    act("DELETE", `/draft/steps/${step.id}`);
  };
  return (
    <>
      <div className="ins-head">
        <div className="ins-tags"><code className="pl-id">{step.id}</code>
          <span className={"ins-kind " + (cat.model ? "model" : "rule")}>{cat.model ? "Model" : "Rule"}</span>{badge}</div>
        <button className="link" onClick={onClose} aria-label="Close">✕</button>
      </div>
      <h3 className="ins-title">{cat.label || step.type}</h3>
      <div className="ins-body">
        {cat.description && <div className="muted-note" style={{ marginTop: 0 }}>{cat.description}</div>}
        {step.note && <div className="pl-step-note">{step.note}</div>}
        {(errs.length > 0 || warns.length > 0) && (
          <ul className="pl-check-list">
            {errs.map((e, k) => <li key={"e" + k} className="err">{e.text}</li>)}
            {warns.map((w, k) => <li key={"w" + k} className="warn">{w.text}</li>)}
          </ul>
        )}
        {SLOT[step.type] && <Instructions key={`${step.id}:${step.config?.[SLOT[step.type]] || ""}`} view={view} step={step} live={live} act={act} disabled={disabled} />}
        <Section title="Reads from"><Chips items={reads} onPick={onSelect} /></Section>
        <Section title="Fills">{io.writes?.length ? <Chips items={io.writes.map((w) => [w, w])} /> : <span className="assess">no fields ({io.rows === "filters" ? "it keeps or drops records" : "it passes records on"})</span>}</Section>
        <StepSettings step={step} disabled={disabled} setConfig={setConfig} />
        {live && (
          <Section title="Last preview">
            {!out ? <span className="assess">Not previewed with these settings.</span>
              : out.error ? <div className="pl-bad">{out.error}</div> : (
                <>
                  <div>{out.count} record{out.count === 1 ? "" : "s"}{needLook ? ` · ${needLook} need a look` : ""}{" "}
                    <button className="link" onClick={() => onShowRecords(step.id)}>see records</button></div>
                  {out.warnings?.length > 0 && <ul className="pl-warn-list">{out.warnings.slice(0, 6).map((w, i) => <li key={i}>{w}</li>)}</ul>}
                </>
              )}
          </Section>
        )}
        <details className="ins-ports"><summary>Ports and schema</summary>
          {live ? <StepPorts view={view} stepId={step.id} /> : <DraftPorts view={view} stepId={step.id} />}</details>
        {!live && <div className="muted-note">Draft steps have no output yet: preview and approve them after Apply.</div>}
        {live && blocked.length > 0 && liveStep?.status !== "approved" && <div className="assess">Approve {blocked.join(", ")} first: this step works on their output.</div>}
      </div>
      <div className="ins-foot">
        {live && <button disabled={disabled} onClick={preview} title="Runs the pipeline up to this step, reusing the saved work of the steps before it">Run on sample</button>}
        {live && liveStep?.status !== "approved" && (
          <button className="primary" disabled={disabled || !out || !!out?.error || blocked.length > 0}
                  title={blocked.length ? `Approve ${blocked.join(", ")} first` : !out ? "Run it on the sample first" : ""}
                  onClick={() => act("POST", `/steps/${step.id}/approve`)}>Approve</button>
        )}
        <button disabled={disabled} onClick={() => onEditStep(step.id)} title={live ? "Opens a draft" : ""}>Edit settings…</button>
        <span className="spacer" />
        <button className="link danger" disabled={disabled} onClick={remove}>Delete step</button>
      </div>
    </>
  );
}

function SourceInspector({ view, act, disabled, src, model, onClose, onSelect, onOpenSource }) {
  const meta = view.sources[src.id] || {};
  const readers = model.steps.filter((x) => (x.type === "extract" && (!x.config?.source_ids?.length || x.config.source_ids.includes(src.id)))
                                       || (x.type === "sql" && (x.config?.tables || []).includes(src.id)));
  const table = view.source_tables?.[src.id];
  const final = view.definition.sources_status === "final";
  const remove = () => {
    if (!final && !window.confirm(`Remove ${src.id} (${src.label})?`)) return;
    act("DELETE", `/sources/${src.id}`);
  };
  return (
    <>
      <div className="ins-head">
        <div className="ins-tags"><code className="pl-id">{src.id}</code><span className="ins-kind rule">Source</span>
          {src.draft_state === "new" && <span className="ins-badge draft">New in draft</span>}</div>
        <button className="link" onClick={onClose} aria-label="Close">✕</button>
      </div>
      <h3 className="ins-title">{src.label}</h3>
      <div className="ins-body">
        <Section title={src.kind === "grants_gov" ? "Search" : "Address"}>
          {src.url ? <a href={src.url} target="_blank" rel="noreferrer" className="ins-url">{src.url}</a> : <span>{src.query}</span>}</Section>
        <Section title="Fetched">{meta.error ? <span className="pl-bad">{meta.error}</span>
          : meta.chars != null ? `${meta.chars.toLocaleString()} characters${meta.truncated ? " (truncated)" : ""}` : "not yet"}</Section>
        <Section title="Read by"><Chips items={readers.map((x) => [x.id, `${x.id} ${view.catalog[x.type]?.label || x.type}${x.type === "sql" ? " (as a table)" : ""}`, `step:${x.id}`])} onPick={onSelect} /></Section>
        {table && <Section title="As a table"><Chips items={Object.keys(table.columns).map((c) => [c, c])} />
          <div className="assess">A CSV: a SQL step can join it by its id, read exactly, with no Extract step.</div></Section>}
        {meta.preview && <Section title="Preview"><div className="ins-preview">{meta.preview}</div></Section>}
      </div>
      <div className="ins-foot">
        <button onClick={() => onOpenSource(src.id)}>Open in the source reader</button>
        <span className="spacer" />
        <button className="link danger" disabled={disabled} onClick={remove}>{final ? "Remove (in a draft)" : "Remove source"}</button>
      </div>
    </>
  );
}

function OutputInspector({ view, act, disabled, model, onClose, onSelect, onPublish, onShowRecords }) {
  const run = view.last_run;
  const check = view.draft?.check;
  const errs = model.draft ? (check?.errors || []).filter((e) => e.node === "output") : [];
  const warns = model.draft ? (check?.warnings || []).filter((e) => e.node === "output") : [];
  return (
    <>
      <div className="ins-head">
        <div className="ins-tags"><code className="pl-id">out</code><span className="ins-kind rule">Output</span></div>
        <button className="link" onClick={onClose} aria-label="Close">✕</button>
      </div>
      <h3 className="ins-title">Output</h3>
      <div className="ins-body">
        <Section title="Takes"><Chips items={model.outputFrom.map((i) => [i, i, `step:${i}`])} onPick={onSelect} /></Section>
        {(errs.length > 0 || warns.length > 0) && (
          <ul className="pl-check-list">
            {errs.map((e, k) => <li key={"e" + k} className="err">{e.text}</li>)}
            {warns.map((w, k) => <li key={"w" + k} className="warn">{w.text}</li>)}
          </ul>
        )}
        <Section title="Last run">{run ? (
          <>
            <div>{(run.at || "").slice(0, 16).replace("T", " ")}{run.upto ? ` · preview to ${run.upto}` : " · full run"}</div>
            <div>{run.coverage?.records ?? 0} records{" "}<button className="link" onClick={() => onShowRecords("final")}>see records</button></div>
          </>
        ) : <span className="assess">No run yet.</span>}</Section>
        <Section title="Publishing">{view.gate?.length ? <ul className="pl-warn-list">{view.gate.map((g, k) => <li key={k}>{g}</li>)}</ul> : "Ready for review."}</Section>
        {view.load_map && <Section title="Catalogue mapping"><LoadMap view={view} act={act} disabled={disabled} /></Section>}
      </div>
      <div className="ins-foot">
        <button disabled={disabled || !(view.definition.steps || []).length} onClick={() => act("POST", "/run", {})}>Run the whole pipeline</button>
        <span className="spacer" />
        <button className="primary" disabled={!!view.gate?.length} onClick={onPublish}
                title={view.gate?.length ? "Not ready yet: see Publishing" : ""}>Review & publish →</button>
      </div>
    </>
  );
}

export default function Inspector({ view, act, disabled, sel, onClose, onSelect, onEditStep, onOpenSource, onShowRecords, onPublish }) {
  const model = stepsModel(view);
  let body = null;
  if (sel?.startsWith("step:")) {
    const step = model.steps.find((s) => `step:${s.id}` === sel);
    if (step) body = <StepInspector view={view} act={act} disabled={disabled} step={step} model={model} onClose={onClose}
                                    onSelect={onSelect} onEditStep={onEditStep} onShowRecords={onShowRecords} />;
  } else if (sel?.startsWith("src:")) {
    const src = model.sources.find((s) => `src:${s.id}` === sel);
    if (src) body = <SourceInspector view={view} act={act} disabled={disabled} src={src} model={model} onClose={onClose}
                                     onSelect={onSelect} onOpenSource={onOpenSource} />;
  } else if (sel === "output") {
    body = <OutputInspector view={view} act={act} disabled={disabled} model={model} onClose={onClose} onSelect={onSelect}
                            onPublish={onPublish} onShowRecords={onShowRecords} />;
  }
  if (!body) return null;
  return <aside className="ws-inspector" aria-label="Selected node">{body}</aside>;
}
