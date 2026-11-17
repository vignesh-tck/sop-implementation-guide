// Shared zoning and reference rendering: used on implementation plan steps and
// in the Zoning tab, so a finding reads the same wherever it appears.

// Reference numbers after a sentence, e.g. [1][4], matching the step's reference list.
export function Cites({ refs }) {
  if (!refs?.length) return null;
  return <sup className="cites">{refs.map((n) => `[${n}]`).join("")}</sup>;
}

// The zoning finding: the answer first, then the background, then what is still
// open. Every sentence carries the numbers of the references that back it.
export function ZoningFinding({ z, detail, seeStep }) {
  if (detail === "summary") {
    return <div className="zoning-summary">Zoning: {z.summary}. Full finding in step {seeStep}.</div>;
  }
  const groups = [];
  for (const p of z.narrative) {
    const last = groups[groups.length - 1];
    if (last && last.section === p.section) last.items.push(p);
    else groups.push({ section: p.section, items: [p] });
  }
  return (
    <div className="zoning-finding">
      <div className="zoning-summary"><b>Zoning:</b> {z.summary}</div>
      {groups.map((g) => (
        <div key={g.section} className="zoning-section">
          <div className="zoning-h">{z.sections?.[g.section] || g.section}</div>
          {g.items.map((p, i) => <p key={i}>{p.text}<Cites refs={p.refs} /></p>)}
        </div>
      ))}
      {z.questions.length > 0 && <SmeQuestions items={z.questions} />}
    </div>
  );
}

export function SmeQuestions({ items }) {
  return (
    <div className="sme-ask">
      <div className="zoning-h">What we need from the SME</div>
      <ol>{items.map((q, i) => <li key={i}>{q.text}<Cites refs={q.refs} /></li>)}</ol>
    </div>
  );
}

// Every step shows where its content comes from. A step nothing supplies yet
// still lists what to check, so there is never a step without a reference.
export function References({ refs, seeStep }) {
  if (seeStep) return <div className="refs-list kv">References: as in step {seeStep}.</div>;
  if (!refs?.length) return null;
  return (
    <ol className="refs-list">
      {refs.map((r, i) => (
        <li key={i}>
          {r.url ? <a href={r.url} target="_blank" rel="noopener noreferrer">{r.label} ↗</a> : <span>{r.label}</span>}
          {r.detail && <span className="assess"> · {r.detail}</span>}
        </li>
      ))}
    </ol>
  );
}

// A zoning outcome as a coloured badge: P, SE, X, LCD or ?.
export const OUTCOME = {
  by_right: { short: "P", text: "Permitted by right" },
  special_exception: { short: "SE", text: "Special exception" },
  prohibited: { short: "X", text: "Prohibited" },
  legacy: { short: "LCD", text: "Legacy zone: set by approved plans" },
  unknown: { short: "?", text: "Not resolved" },
};

export function Outcome({ outcome }) {
  const o = OUTCOME[outcome] || OUTCOME.unknown;
  return <span className={"outcome " + (outcome || "unknown")} title={o.text}>{o.short}</span>;
}
