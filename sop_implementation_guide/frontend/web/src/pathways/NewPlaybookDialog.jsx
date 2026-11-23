import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { call, uploadFiles } from "../api.js";
import { ACTORS } from "../blocks/plan/actors.jsx";

// A new playbook three ways: blank, a copy of an existing one, or drafted by
// the assistant from what it is for, optional source documents and a playbook
// to model it on. The draft streams in step by step; "Open draft on canvas"
// turns it into suggestions (one per step, each citing its source) that the
// planner accepts or rejects there. Recommendation types come only from the
// real SoP list.

const POLL_MS = 1200;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function Outline({ steps, running }) {
  const num = Object.fromEntries(steps.map((s, i) => [s.id, i + 1]));
  return (
    <ol className="np-outline">
      {steps.map((s, i) => (
        <li key={s.id || i}>
          <span className="np-n">{i + 1}</span>
          <div>
            <div className="np-title">{s.title}</div>
            <div className="np-meta">
              {[ACTORS[s.actor]?.label || s.actor, s.owner, s.time,
                (s.depends_on || []).length ? `after ${(s.depends_on || []).map((d) => num[d]).filter(Boolean).join(s.depends_mode === "any" ? " or " : ", ")}` : null,
                s.condition ? `only if ${s.condition.toLowerCase()}` : null].filter(Boolean).join(" · ")}
            </div>
            {s.source && <div className="np-src">{s.source}</div>}
          </div>
        </li>
      ))}
      {running && (
        <li className="np-skel"><span className="np-n">{steps.length + 1}</span><div><span /><span /></div></li>
      )}
      {!running && !steps.length && <li className="np-empty">The outline appears here as the assistant writes it.</li>}
    </ol>
  );
}

export default function NewPlaybookDialog({ working, labels, onClose, onBlank, onCopy, onDrafted }) {
  const [tab, setTab] = useState("ai");
  const [name, setName] = useState("");
  const [copyFrom, setCopyFrom] = useState(working.pathways[0]?.key || "");
  const [purpose, setPurpose] = useState("");
  const [files, setFiles] = useState([]);
  const [recTypes, setRecTypes] = useState([]);
  const [modelOn, setModelOn] = useState("");
  const [job, setJob] = useState(null);
  const [progress, setProgress] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [uploading, setUploading] = useState(false);
  const live = useRef(true);
  const fileInput = useRef(null);

  useEffect(() => () => { live.current = false; }, []);
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && !job && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [job, onClose]);

  const running = !!job;
  const steps = (result || progress)?.steps || [];
  const meta = (result || progress)?.meta || {};
  const suggested = (result?.suggested_rec_types || []).filter((l) => !recTypes.includes(l));
  const mappedTo = (l) => working.rec_types[l];

  async function attach(list) {
    if (!list?.length) return;
    setUploading(true);
    setError(null);
    try {
      const r = await uploadFiles([...list]);
      setFiles((f) => [...f, ...r.uploaded.map((u) => ({ name: u.filename, url: u.url }))]);
      if (r.errors?.length) setError(r.errors.map((e) => `${e.filename}: ${e.error}`).join("; "));
    } catch (e) {
      setError(e.message);
    } finally {
      setUploading(false);
    }
  }

  async function draft() {
    setError(null);
    setResult(null);
    setProgress(null);
    try {
      const { job: id } = await call("POST", "/pathways/draft", {
        purpose, working, files, rec_types: recTypes, model_on: modelOn || null,
      });
      setJob(id);
      for (;;) {
        await wait(POLL_MS);
        if (!live.current) return;
        const r = await call("GET", `/pathways/agent/${id}`);
        if (r.progress) setProgress(r.progress);
        if (r.status === "running") continue;
        if (r.status === "failed") throw new Error(r.error);
        setResult(r.result);
        break;
      }
    } catch (e) {
      if (live.current) setError(e.message);
    } finally {
      if (live.current) setJob(null);
    }
  }

  const stop = () => job && call("POST", `/pathways/agent/${job}/stop`).catch(() => {});

  const body = (
    <div className="np-backdrop" onMouseDown={(e) => e.target === e.currentTarget && !running && onClose()}>
      <div className="np-dialog" role="dialog" aria-label="New playbook">
        <div className="np-head">
          <h3>New playbook</h3>
          <span className="seg" role="tablist">
            <button type="button" aria-pressed={tab === "blank"} onClick={() => setTab("blank")} disabled={running}>Start blank</button>
            <button type="button" aria-pressed={tab === "ai"} onClick={() => setTab("ai")} disabled={running}>✦ Draft with AI</button>
            <button type="button" aria-pressed={tab === "copy"} onClick={() => setTab("copy")} disabled={running}>Copy an existing one</button>
          </span>
          <span className="spacer" />
          <button type="button" className="si-close" onClick={onClose} disabled={running} aria-label="Close">✕</button>
        </div>

        {tab === "blank" && (
          <div className="np-simple">
            <label>Name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Vacant lot activation" autoFocus /></label>
            <div className="btn-row">
              <button type="button" className="primary" disabled={!name.trim()} onClick={() => onBlank(name.trim())}>Create</button>
              <button type="button" onClick={onClose}>Cancel</button>
            </div>
          </div>
        )}

        {tab === "copy" && (
          <div className="np-simple">
            <label>Copy
              <select value={copyFrom} onChange={(e) => setCopyFrom(e.target.value)}>
                {working.pathways.map((p) => <option key={p.key} value={p.key}>{p.name} ({p.steps.length} steps)</option>)}
              </select>
            </label>
            <label>New name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name of the copy" /></label>
            <div className="btn-row">
              <button type="button" className="primary" disabled={!name.trim() || !copyFrom} onClick={() => onCopy(copyFrom, name.trim())}>Copy</button>
              <button type="button" onClick={onClose}>Cancel</button>
            </div>
          </div>
        )}

        {tab === "ai" && (
          <div className="np-grid">
            <div className="np-form">
              <label className="np-field">
                <b>What is this playbook for?</b>
                <textarea rows={4} value={purpose} disabled={running} onChange={(e) => setPurpose(e.target.value)}
                          placeholder="e.g. Activating vacant lots on the block: find the owner, get permission or take control through the land bank, clean the lot, and hand it to a community steward." />
              </label>
              <div className="np-field">
                <b>Source material <span className="assess">(optional)</span></b>
                <div className="np-chips">
                  {files.map((f, i) => (
                    <span key={f.url} className="pb-file">📄 {f.name}
                      <button type="button" className="link" disabled={running} onClick={() => setFiles((x) => x.filter((_, j) => j !== i))} aria-label={`Remove ${f.name}`}>✕</button>
                    </span>
                  ))}
                  <input ref={fileInput} type="file" multiple accept=".pdf,.csv,.txt" hidden onChange={(e) => { attach(e.target.files); e.target.value = ""; }} />
                  <button type="button" className="np-add" disabled={running || uploading} onClick={() => fileInput.current?.click()}>
                    {uploading ? "Uploading…" : "+ Add file"}
                  </button>
                </div>
              </div>
              <div className="np-field">
                <b>Recommendation types it covers</b>
                <div className="np-chips">
                  {recTypes.map((l) => (
                    <span key={l} className="np-type">{l}
                      <button type="button" className="link" onClick={() => setRecTypes((x) => x.filter((y) => y !== l))} aria-label={`Remove ${l}`}>✕</button>
                    </span>
                  ))}
                  {suggested.map((l) => (
                    <button key={l} type="button" className="np-type sugg" onClick={() => setRecTypes((x) => [...x, l])}
                            title={mappedTo(l) ? `Now in another playbook; adding it moves it here` : "Suggested by the assistant"}>+ {l}</button>
                  ))}
                </div>
                <select value="" onChange={(e) => e.target.value && setRecTypes((x) => [...x, e.target.value])}>
                  <option value="">+ Add a recommendation type…</option>
                  {labels.filter((l) => !recTypes.includes(l)).map((l) => (
                    <option key={l} value={l}>{l}{mappedTo(l) ? "" : " (no playbook yet)"}</option>
                  ))}
                </select>
                <span className="assess">Only the SoP recommendation types can be covered. A type already in another playbook moves here.</span>
              </div>
              <label className="np-field">
                <b>Model it on</b>
                <select value={modelOn} disabled={running} onChange={(e) => setModelOn(e.target.value)}>
                  <option value="">Nothing: general practice</option>
                  {working.pathways.map((p) => <option key={p.key} value={p.key}>{p.name} ({p.steps.length} steps)</option>)}
                </select>
                <span className="assess">Reuses its owners, step style and typical durations.</span>
              </label>
            </div>

            <div className="np-side">
              <div className="np-side-h">
                <b>Draft outline</b>
                {running && <span className="np-status">Writing step {steps.length + 1}{meta.estimated_steps ? ` of about ${meta.estimated_steps}` : ""}</span>}
                {!running && result && <span className="np-status done">{meta.name || "Draft"} · {steps.length} steps</span>}
              </div>
              <Outline steps={steps} running={running} />
              <div className="np-note">✦ Every step opens on the canvas as a suggestion you accept or reject. Each one cites where it came from.</div>
              {error && <div className="banner err">{error}</div>}
            </div>
          </div>
        )}

        {tab === "ai" && (
          <div className="np-foot">
            {running && <button type="button" className="link" onClick={stop}>Stop</button>}
            {!running && result && <button type="button" className="link" onClick={draft} disabled={!purpose.trim()}>Draft again</button>}
            <span className="spacer" />
            <button type="button" onClick={onClose} disabled={running}>Cancel</button>
            {result ? (
              <button type="button" className="primary" onClick={() => onDrafted(result, recTypes)}>Open draft on canvas</button>
            ) : (
              <button type="button" className="primary" onClick={draft} disabled={running || !purpose.trim()}>
                {running ? "Drafting…" : "✦ Draft it"}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
  return createPortal(body, document.body);
}
