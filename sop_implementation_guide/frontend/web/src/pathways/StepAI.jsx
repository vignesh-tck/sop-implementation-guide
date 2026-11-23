import { useEffect, useRef, useState } from "react";

// The assistant at the level of one step: the toolbar over the selected step
// (Edit, Duplicate, Ask AI, delete), the Ask AI menu, and the inline
// suggestions a quick action returns into the step panel: a rewritten
// description shown word by word against the current one, or a value to use.

// Word-level differences between two texts, as [{type: same|del|ins, text}].
export function wordDiff(a, b) {
  const A = String(a || "").split(/(\s+)/), B = String(b || "").split(/(\s+)/);
  const n = A.length, m = B.length;
  if (n * m > 250000) return [{ type: "del", text: a }, { type: "ins", text: b }];
  const L = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) {
    L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  }
  const out = [];
  const push = (type, text) => {
    const last = out[out.length - 1];
    if (last && last.type === type) last.text += text;
    else out.push({ type, text });
  };
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { push("same", A[i]); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) push("del", A[i++]);
    else push("ins", B[j++]);
  }
  while (i < n) push("del", A[i++]);
  while (j < m) push("ins", B[j++]);
  return out;
}

export function DiffText({ before, after }) {
  return (
    <span className="wdiff">
      {wordDiff(before, after).map((p, k) => (
        p.type === "same" ? <span key={k}>{p.text}</span>
          : p.type === "del" ? <del key={k}>{p.text}</del>
          : <ins key={k}>{p.text}</ins>
      ))}
    </span>
  );
}

// A rewritten description, in place of the description box until kept or discarded.
export function TextSuggestion({ before, after, note, busy, onKeep, onDiscard, onVariant }) {
  return (
    <div className={"ai-text" + (busy ? " busy" : "")}>
      <div className="ai-text-head">✦ {note ? `Rewritten: ${note}` : "Suggested rewrite"}</div>
      <div className="ai-text-body">{busy ? <span className="pl-spin" /> : <DiffText before={before} after={after} />}</div>
      <div className="ai-text-actions">
        <button type="button" className="primary" onClick={onKeep} disabled={busy}>Keep</button>
        <button type="button" onClick={onDiscard} disabled={busy}>Discard</button>
        <button type="button" className="link" onClick={() => onVariant("again")} disabled={busy}>Try again</button>
        <span className="spacer" />
        <button type="button" className="link" onClick={() => onVariant("shorter")} disabled={busy}>Shorter</button>
        <button type="button" className="link" onClick={() => onVariant("plainer")} disabled={busy}>Plainer</button>
      </div>
    </div>
  );
}

// A suggested value under a field: click to use it.
export function ValueChip({ text, note, onUse, onDismiss }) {
  return (
    <span className="ai-chip-row">
      <button type="button" className="ai-chip" onClick={onUse} title={note || "Use this"}>✦ {text}</button>
      {note && <span className="ai-note">{note}</span>}
      {onDismiss && <button type="button" className="link ai-x" onClick={onDismiss} aria-label="Dismiss">✕</button>}
    </span>
  );
}

const IMPROVE = [
  ["rewrite", "Rewrite description", "clearer, with data source"],
  ["estimate", "Estimate duration", "from similar steps"],
  ["owners", "Suggest owners", "primary + secondary"],
];
const FLOW = [
  ["split", "Split into smaller steps"],
  ["next", "What should come next?"],
  ["deps", "Check dependencies"],
];

export function StepToolbar({ onEdit, onDuplicate, onDelete, onQuick, onFlow, onAsk }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const box = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const away = (e) => { if (!box.current?.contains(e.target)) setOpen(false); };
    window.addEventListener("pointerdown", away);
    return () => window.removeEventListener("pointerdown", away);
  }, [open]);
  const pick = (fn) => () => { setOpen(false); fn(); };
  return (
    <div className="step-toolbar nodrag nopan" ref={box}>
      <button type="button" onClick={onEdit}>Edit</button>
      <button type="button" onClick={onDuplicate}>Duplicate</button>
      <button type="button" className="ask" aria-expanded={open} onClick={() => setOpen((o) => !o)}>✦ Ask AI ▾</button>
      <button type="button" className="del" onClick={onDelete} title="Remove step" aria-label="Remove step">🗑</button>
      {open && (
        <div className="ai-menu" role="menu">
          <div className="ai-menu-h">Improve this step</div>
          {IMPROVE.map(([k, label, hint]) => (
            <button key={k} type="button" role="menuitem" onClick={pick(() => onQuick(k))}>
              <span>{label}</span><span className="hint">{hint}</span>
            </button>
          ))}
          <div className="ai-menu-h">Change the flow</div>
          {FLOW.map(([k, label]) => (
            <button key={k} type="button" role="menuitem" onClick={pick(() => onFlow(k))}><span>{label}</span></button>
          ))}
          <form className="ai-menu-ask" onSubmit={(e) => { e.preventDefault(); if (text.trim()) { setOpen(false); onAsk(text.trim()); setText(""); } }}>
            <input value={text} onChange={(e) => setText(e.target.value)} placeholder="✦ Ask anything about this step…" autoFocus />
          </form>
        </div>
      )}
    </div>
  );
}

// The chat prompts behind the "Change the flow" items.
export const FLOW_PROMPTS = {
  split: (n, t) => `Split @step ${n} (${t}) into smaller steps where it bundles separate activities or owners.`,
  next: (n, t) => `What should come after @step ${n} (${t})? Suggest the missing next step if there is one.`,
  deps: (n, t) => `Check what @step ${n} (${t}) waits on and what waits on it. Fix any dependency that is missing or wrong.`,
};
