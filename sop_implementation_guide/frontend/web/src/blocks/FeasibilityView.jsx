import { useCallback, useEffect, useState } from "react";
import { call, money } from "../api.js";
import { References } from "../zoning/ZoningParts.jsx";

// A recommendation's feasibility: an LLM judgement of how likely it is to get
// done, from its approved plan, funding and zoning (backend/agents/feasibility.py).
// It can only be assessed once all three are approved; until then this shows
// what is missing. An assessment waits for the SME, who accepts the score or
// sets another with a note. Then it is current, and the block's feasibility
// score (the map's colour) becomes its best recommendation's score.

const NAME_KEY = "sop.pathways.reviewer"; // shared with plans and the Playbooks tab

function readName() {
  try { return localStorage.getItem(NAME_KEY) || ""; } catch { return ""; }
}
function writeName(v) {
  try { localStorage.setItem(NAME_KEY, v); } catch { /* a per-viewer convenience only */ }
}

export const BAND = {
  strong: { text: "Strong candidate", cls: "ok" },
  possible: { text: "Possible", cls: "cond" },
  weak: { text: "Weak", cls: "no" },
  not_viable: { text: "Not viable as it stands", cls: "no" },
};
const RATING = { strong: "ok", adequate: "prop", weak: "warn", blocking: "warn" };
const INPUT_ROWS = [
  ["plan", "Implementation plan approved"],
  ["funding", "Block funding analysis approved"],
  ["zoning", "Block zoning review approved, covering this recommendation"],
];

const when = (iso) => (iso || "").slice(0, 10);
const pct = (x) => (x == null ? "—" : `${Math.round(x * 100)}%`);

// The recommendation's feasibility, re-read while a run is in progress.
export function useFeasibility(blockId, recId, refreshKey) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let live = true;
    call("GET", `/blocks/${blockId}/recommendations/${recId}/feasibility`)
      .then((d) => { if (live) { setData(d); setError(null); } })
      .catch((e) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [blockId, recId, refreshKey, tick]);

  useEffect(() => { setData(null); }, [blockId, recId]);

  const running = data?.open?.status === "running";
  useEffect(() => {
    if (!running) return undefined;
    const t = setTimeout(reload, 4000);
    return () => clearTimeout(t);
  }, [running, data, reload]);

  return { data, error, reload };
}

// The tile above the tabs: the approved score, or where the assessment stands.
export function FeasibilityTile({ f, onClick }) {
  let value = "…", sub = "", cls = "none";
  if (f) {
    const cur = f.current;
    if (f.open?.status === "running") { value = "Assessing…"; sub = "About a minute"; cls = "edited"; }
    else if (f.open?.status === "in_review") { value = `${f.open.proposed_score} proposed`; sub = "Waiting for the SME"; cls = "edited"; }
    else if (cur) {
      value = `${cur.score} · ${BAND[cur.band]?.text || cur.band}`;
      sub = f.stale?.length ? "Inputs changed: assess again" : `Approved · ${cur.approved_by}`;
      cls = f.stale?.length ? "cond" : BAND[cur.band]?.cls || "none";
    } else if (f.missing?.length) { value = "Not ready"; sub = `Needs ${f.missing.map((m) => m.key).join(", ")}`; }
    else { value = "Ready to assess"; sub = "Plan, funding and zoning approved"; cls = "cond"; }
  }
  return (
    <button type="button" className={"tile " + cls} onClick={onClick}>
      <span className="tile-k">Feasibility</span>
      <span className="tile-v">{value}</span>
      <span className="tile-s">{sub}</span>
    </button>
  );
}

// Text with [R3] citations, shown as the reference numbers below it.
function Cited({ text }) {
  if (!text) return null;
  const parts = text.split(/(\[R\d+(?:\s*,\s*R\d+)*\])/g);
  return (
    <>
      {parts.map((p, i) => {
        const m = p.match(/^\[(R\d+(?:\s*,\s*R\d+)*)\]$/);
        if (!m) return <span key={i}>{p}</span>;
        const nums = m[1].split(",").map((s) => s.trim().slice(1));
        return <sup key={i} className="cites">{nums.map((n) => `[${n}]`).join("")}</sup>;
      })}
    </>
  );
}

function Readiness({ missing, onOpen }) {
  const keys = new Set(missing.map((m) => m.key));
  return (
    <div className="feas-ready">
      <div className="zoning-h">Before it can be assessed</div>
      <ul>
        {INPUT_ROWS.map(([k, label]) => {
          const m = missing.find((x) => x.key === k);
          return (
            <li key={k} className={keys.has(k) ? "todo" : "done"}>
              <span aria-hidden="true">{keys.has(k) ? "○" : "✓"}</span>
              <span>
                {label}
                {m && <span className="assess"> · {m.text}{" "}
                  <button type="button" className="link" onClick={() => onOpen(k)}>Open</button></span>}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// Share of the cost the funding asked would cover. Past 100% it says so, rather than "covers 5749%".
function coverText(cov) {
  if (!cov.funding_total) return "no approved funding";
  if (cov.share_low != null && cov.share_low >= 1) return "more than the estimated cost";
  const hi = cov.share_high != null && cov.share_high >= 1 ? "100%+" : pct(cov.share_high);
  return `covers ${pct(cov.share_low)}${hi !== pct(cov.share_low) ? `–${hi}` : ""} of the cost`;
}

function CostAndFunding({ a }) {
  const p = a.proposed || {};
  const cost = a.decision?.cost || p.cost_estimate || {};
  const cov = a.decision?.coverage || p.coverage || {};
  const total = cov.funding_total || 0;
  // The bar: funding against the high cost (solid) and the low cost (lighter).
  const worst = Math.min(1, cov.share_low || 0), best = Math.min(1, cov.share_high || 0);
  return (
    <div className="feas-money">
      <div className="feas-money-row">
        <div>
          <div className="tile-k">Estimated cost</div>
          <b>{money(Math.round(cost.low))} – {money(Math.round(cost.high))}</b>
          <div className="assess">
            {a.decision?.cost ? `Corrected by ${a.decision.by}` : `LLM estimate, ${p.cost_estimate?.confidence || "unknown"} confidence`}
          </div>
        </div>
        <div>
          <div className="tile-k">Approved funding asked</div>
          <b>{money(total)}</b>
          <div className="assess">{coverText(cov)}</div>
        </div>
      </div>
      <div className="feas-bar" title="Approved funding as a share of the estimated cost (low to high)">
        <span className="best" style={{ width: `${best * 100}%` }} />
        <span className="worst" style={{ width: `${worst * 100}%` }} />
      </div>
      {p.cost_estimate?.basis && <p className="assess"><b>How the cost was estimated:</b> {p.cost_estimate.basis}</p>}
      {p.funding_coverage?.summary && <p><Cited text={p.funding_coverage.summary} /></p>}
    </div>
  );
}

// The SME input an assessment read, cited in it like any other source.
function SmeInput({ guidance }) {
  if (!guidance?.length) return null;
  return (
    <div className="feas-input">
      <div className="zoning-h">Your input this assessment used</div>
      <ul>
        {guidance.map((g, i) => (
          <li key={i}>“{g.text}” <span className="assess">— {g.by || "SME"}, {when(g.at)}</span></li>
        ))}
      </ul>
    </div>
  );
}

// Send an assessment back with what the agent got wrong or should know; a new run reworks it.
function ReworkBox({ a, name, setName, onStarted, current }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function send() {
    setBusy(true); setError(null);
    try {
      await call("POST", `/feasibility/${a.id}/rework`, { guidance: text.trim(), made_by: name.trim() });
      setText(""); setOpen(false); onStarted();
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }

  if (!open) {
    return (
      <div className="btn-row">
        <button type="button" onClick={() => setOpen(true)}>Rework with your input</button>
        <span className="assess">Tell the agent what it got wrong or should know, and it reworks the assessment.</span>
      </div>
    );
  }
  return (
    <div className="plan-draft-bar feas-review">
      <div>
        <b>What should the agent know or reconsider?</b> For example the real scope (“depave and landscape, not
        redevelopment”), the owner's position, a cost you know, or funding it missed. Your input is cited in the
        rework and kept for later runs of this recommendation.
        {current && " The approved score stays in force until you approve the rework."}
      </div>
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={4} autoFocus
                placeholder="Your input for the agent" aria-label="Your input for the agent" />
      <div className="plan-edit-row">
        <label>Your name
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Required" />
        </label>
      </div>
      {error && <div className="banner err">{error}</div>}
      <div className="btn-row">
        <button type="button" className="primary" onClick={send} disabled={busy || !text.trim() || !name.trim()}>
          {busy ? "Starting…" : "Rework"}
        </button>
        <button type="button" onClick={() => setOpen(false)} disabled={busy}>Cancel</button>
      </div>
    </div>
  );
}

function Assessment({ a }) {
  const p = a.proposed || {};
  const score = a.score ?? a.proposed_score;
  const band = BAND[a.band] || {};
  const refs = (a.evidence?.references || []).map((r) => ({ label: r.label, url: r.url, detail: r.detail }));
  return (
    <div className="feas">
      {p.what_changed && (
        <div className="banner ok feas-changed"><b>Reworked with your input.</b> <Cited text={p.what_changed} /></div>
      )}
      <div className="feas-head">
        <div className={"feas-score " + (band.cls || "")}>
          <b>{score}</b><span>/ 100</span>
        </div>
        <div>
          <div className={"feas-band " + (band.cls || "")}>{band.text || a.band}</div>
          <div><Cited text={p.headline} /></div>
          {a.decision?.action === "set" && (
            <div className="assess">Set by {a.decision.by} (the assessment proposed {a.decision.proposed_score}): “{a.decision.note}”</div>
          )}
          {a.decision?.action === "accept" && a.decision.note && <div className="assess">“{a.decision.note}”</div>}
        </div>
      </div>

      <SmeInput guidance={a.inputs?.guidance} />

      <CostAndFunding a={a} />

      <div className="zoning-h">What the score weighs</div>
      <ul className="feas-factors">
        {(p.factors || []).map((f) => (
          <li key={f.key}>
            <span className={"status-chip " + (RATING[f.rating] || "")}>{f.rating}</span>
            <span><b>{f.label}.</b> <Cited text={f.note} /></span>
          </li>
        ))}
      </ul>

      <div className="zoning-h">Why</div>
      <p><Cited text={p.rationale} /></p>

      {p.risks?.length > 0 && (
        <>
          <div className="zoning-h">Risks</div>
          <ul className="feas-list">{p.risks.map((r, i) => <li key={i}><Cited text={r.text} /></li>)}</ul>
        </>
      )}
      {p.raise_score?.length > 0 && (
        <>
          <div className="zoning-h">What would raise it</div>
          <ul className="feas-list">{p.raise_score.map((r, i) => <li key={i}>{r}</li>)}</ul>
        </>
      )}
      {p.sme_questions?.length > 0 && (
        <div className="sme-ask">
          <div className="zoning-h">What we need from the SME</div>
          <ol>{p.sme_questions.map((q, i) => <li key={i}>{q}</li>)}</ol>
        </div>
      )}

      <div className="zoning-h refs-h">Sources</div>
      <References refs={refs} />
      <p className="assess">
        Assessed by {a.model || "the model"} on {when(a.completed_at || a.created_at)}
        {a.inputs?.plan_revision != null && <> from plan v{a.inputs.plan_revision}</>}
        {a.inputs?.funding_version != null && <>, Funding v{a.inputs.funding_version}</>}
        {a.inputs?.zoning_version != null && <>, Zoning v{a.inputs.zoning_version}</>}
        {a.approved_by && <>. Approved by {a.approved_by} on {when(a.approved_at)}</>}.
        The cost is the model's estimate, not a sourced figure.
      </p>
    </div>
  );
}

function ReviewForm({ a, stale, name, setName, onDone }) {
  const p = a.proposed || {};
  const [action, setAction] = useState("accept");
  const [score, setScore] = useState(String(a.proposed_score ?? ""));
  const [costLow, setCostLow] = useState("");
  const [costHigh, setCostHigh] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const changedScore = action === "set" && Number(score) !== a.proposed_score;
  const changedCost = costLow !== "" || costHigh !== "";
  const needsNote = changedScore || changedCost;
  const canApprove = name.trim() && !stale.length && (!needsNote || note.trim()) &&
    (action === "accept" || (score !== "" && Number(score) >= 0 && Number(score) <= 100));

  async function approve() {
    setBusy(true); setError(null);
    try {
      const r = await call("POST", `/feasibility/${a.id}/review`, {
        action, approved_by: name.trim(), note: note.trim() || null,
        score: action === "set" ? Number(score) : null,
        cost_low: costLow !== "" ? Number(costLow) : null,
        cost_high: costHigh !== "" ? Number(costHigh) : null,
      });
      onDone(r.profile);
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  async function discard() {
    if (!window.confirm("Discard this assessment? It is kept in the history as discarded.")) return;
    setBusy(true); setError(null);
    try { await call("POST", `/feasibility/${a.id}/discard`); onDone(null); }
    catch (e) { setError(e.message); } finally { setBusy(false); }
  }

  return (
    <div className="plan-draft-bar feas-review">
      <div>
        <b>Waiting for your review.</b> Accept the score, or set the one you would give, with a note on why.
        Nothing else reads it until you approve.
        {stale.length > 0 && <div className="banner warn" style={{ marginTop: ".5rem" }}>
          {stale.join(", ")} changed since this ran, so it cannot be approved. Assess again.</div>}
      </div>
      <span className="seg" role="group" aria-label="Decision">
        <button type="button" aria-pressed={action === "accept"} onClick={() => setAction("accept")}>Accept {a.proposed_score}</button>
        <button type="button" aria-pressed={action === "set"} onClick={() => setAction("set")}>Set another score</button>
      </span>
      <div className="plan-edit-row">
        {action === "set" && (
          <label>Score (0–100)
            <input type="number" min="0" max="100" value={score} onChange={(e) => setScore(e.target.value)} style={{ width: "6rem" }} />
          </label>
        )}
        <label>Cost low ($)
          <input type="number" min="0" value={costLow} placeholder={String(Math.round(p.cost_estimate?.low || 0))}
                 onChange={(e) => setCostLow(e.target.value)} style={{ width: "8rem" }} />
        </label>
        <label>Cost high ($)
          <input type="number" min="0" value={costHigh} placeholder={String(Math.round(p.cost_estimate?.high || 0))}
                 onChange={(e) => setCostHigh(e.target.value)} style={{ width: "8rem" }} />
        </label>
      </div>
      <div className="plan-edit-row">
        <label>Your name
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Required" />
        </label>
        <label className="grow">Note{needsNote ? " (required: why the score or cost differs)" : " (optional)"}
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Shown with the approved score" />
        </label>
      </div>
      {error && <div className="banner err">{error}</div>}
      <div className="btn-row">
        <button type="button" className="primary" onClick={approve} disabled={busy || !canApprove}>{busy ? "Approving…" : "Approve"}</button>
        <button type="button" onClick={discard} disabled={busy}>Discard</button>
      </div>
    </div>
  );
}

export default function FeasibilityView({ blockId, recId, f, error, reload, onOpenInput, onApproved }) {
  const [name, setNameState] = useState(readName);
  const setName = (v) => { setNameState(v); writeName(v); };
  const [busy, setBusy] = useState(false);
  const [startError, setStartError] = useState(null);

  if (error) return <div className="banner err">Could not load feasibility — {error}</div>;
  if (!f) return <div className="spin">Loading…</div>;

  async function assess() {
    setBusy(true); setStartError(null);
    try { await call("POST", `/blocks/${blockId}/recommendations/${recId}/feasibility`, { created_by: name.trim() || null }); reload(); }
    catch (e) { setStartError(e.message); } finally { setBusy(false); }
  }

  const ready = !f.missing?.length;
  const open = f.open;
  const assessButton = (label) => (
    <div className="btn-row">
      <button type="button" className={f.current ? "" : "primary"} onClick={assess} disabled={busy || !ready || f.storage_ready === false}>
        {busy ? "Starting…" : label}
      </button>
      <span className="assess">One model run, about a minute. The SME reviews the result before it counts.</span>
    </div>
  );

  return (
    <div className="tab-body">
      {f.storage_ready === false && <div className="banner warn">{f.detail}</div>}
      {!ready && <Readiness missing={f.missing} onOpen={onOpenInput} />}
      {startError && <div className="banner err">{startError}</div>}

      {open?.status === "running" && <div className="banner ok">Assessing… the model is weighing the plan, funding and zoning. This takes about a minute.</div>}
      {open?.status === "failed" && (
        <>
          <div className="banner err">The last assessment failed: {open.error}</div>
          {ready && assessButton("Assess again")}
        </>
      )}
      {open?.status === "in_review" && (
        <>
          <Assessment a={open} />
          <ReviewForm a={open} stale={f.open_stale || []} name={name} setName={setName}
                      onDone={(profile) => { reload(); if (profile) onApproved?.(profile); }} />
          <ReworkBox a={open} name={name} setName={setName} onStarted={reload} />
        </>
      )}

      {!open && f.current && (
        <>
          {f.stale?.length > 0 && (
            <div className="banner warn">{f.stale.join(", ").replace(/^./, (c) => c.toUpperCase())} changed since this was
              assessed. The score still counts until a new assessment is approved.</div>
          )}
          <Assessment a={f.current} />
          {ready && <ReworkBox a={f.current} name={name} setName={setName} onStarted={reload} current />}
          {ready && assessButton("Assess again")}
        </>
      )}

      {!open && !f.current && ready && (
        <>
          <p className="assess">
            The plan, funding and zoning for this recommendation are approved. The feasibility agent estimates what it
            would cost, compares that with the approved funding, and weighs the zoning path, the plan's length and the
            parties who must act, then proposes a score from 0 to 100 for you to accept or change.
          </p>
          {assessButton("Assess feasibility")}
        </>
      )}
    </div>
  );
}
