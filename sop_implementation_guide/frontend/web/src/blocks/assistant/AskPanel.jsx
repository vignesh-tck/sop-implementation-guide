import { useEffect, useRef, useState } from "react";
import { num } from "../../api.js";
import { rankRecs } from "../AllRecommendations.jsx";
import { estimateText } from "../plan/graph.js";
import { fundingStatus } from "../useBlockFunding.js";
import { zoningStatus } from "../useBlockZoning.js";
import AnswerView from "./AnswerView.jsx";

// "Ask about this block": a briefing read straight from the block's data (no
// model involved), good questions to start from, and the conversation. With
// scope "all" the same panel asks across every block.

function Spark() {
  return <span className="spark" aria-hidden="true">✧</span>;
}

// The briefing's lines and the gaps in what the assistant can know.
function briefing(recs, fs, zs) {
  const ranked = rankRecs(recs);
  const lines = [];
  if (ranked.length) {
    const top = ranked.slice(0, 5);
    const counts = {};
    for (const r of top) counts[r.dimension] = (counts[r.dimension] || 0) + 1;
    const [dim, n] = Object.entries(counts).sort((a, b) => b[1] - a[1])[0] || [];
    lines.push(<>{ranked.length} recommendations.{n >= 2 && <> {n} of the top {top.length} are {dim}.</>}</>);
    const best = ranked[0];
    const est = estimateText(best.estimate);
    lines.push(<>Biggest single lift: <b>{best.rec_label}, +{num(best.predicted_score_increase)}</b>{est && <>, {est}</>}.</>);
    const approved = ranked.filter((r) => r.plan_status === "final").length;
    const waiting = ranked.filter((r) => r.draft_pending).length;
    lines.push(approved === 0
      ? <>No plan approved yet. {waiting ? <>{waiting} draft{waiting === 1 ? "" : "s"} waiting; the rest</> : <>All {ranked.length}</>} still use their playbook defaults.</>
      : <>{approved} of {ranked.length} plans approved{waiting ? <>, {waiting} draft{waiting === 1 ? "" : "s"} waiting</> : null}.</>);
  } else {
    lines.push(<>No recommendations on this block.</>);
  }

  const f = fundingStatus(fs), z = zoningStatus(zs);
  const gaps = [];
  if (f.key === "none") gaps.push("funding hasn't been run");
  else if (f.key === "review") gaps.push("funding is awaiting approval");
  if (z.text === "Needs a re-run") gaps.push("zoning needs a re-run");
  else if (z.key === "none") gaps.push("zoning hasn't been run");
  else if (z.key === "review") gaps.push("zoning is awaiting approval");
  return { lines, gaps, funding: f, zoning: z };
}

function Briefing({ recs, fs, zs, onOpenFunding, onOpenZoning }) {
  if (!recs) return <div className="ask-brief"><span className="spin">Reading the block…</span></div>;
  const b = briefing(recs, fs, zs);
  return (
    <div className="ask-brief">
      <div className="ask-brief-head"><b>Block briefing</b> <span className="assess">from this block's data</span></div>
      <ul>{b.lines.map((l, i) => <li key={i}>{l}</li>)}</ul>
      {b.gaps.length > 0 && (
        <div className="ask-gaps">
          <div>
            <b>Gaps in what I know:</b> {b.gaps.join(" and ")}, so answers about cost and what's allowed by right
            will be incomplete.
          </div>
          <div className="ask-gap-btns">
            {b.gaps.some((g) => g.startsWith("zoning")) && (
              <button type="button" onClick={onOpenZoning}>{b.zoning.key === "review" ? "Open zoning review" : "Run zoning review"}</button>
            )}
            {b.gaps.some((g) => g.startsWith("funding")) && (
              <button type="button" onClick={onOpenFunding}>{b.funding.key === "review" ? "Open funding review" : "Run funding analysis"}</button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function starters(block, blocksCount) {
  if (!block) {
    return [
      ["Lowest scores", "Which blocks score lowest, and what holds them back?"],
      ["Reviews", "Which blocks still need funding or zoning reviews?"],
      ["Biggest lifts", "Where would one recommendation lift the score the most?"],
      ["Streets", `Which streets have the most Traffic Safety recommendations across the ${blocksCount} blocks?`],
    ];
  }
  return [
    ["The block", `Why is SoP ${num(block.sop_index_norm)} here?`],
    ["Recommendations", "Which ones overlap or conflict?"],
    ["Initial plan", "Draft a first plan from the top 5"],
    ["Duration", "How long would the top 5 take?"],
  ];
}

export default function AskPanel({
  block, blocksCount, scope, onScope, recs, fs, zs, turns, pending, onAsk, onRetry, onClear, onAction,
  onOpenFunding, onOpenZoning, contextRec, focusKey,
}) {
  const [text, setText] = useState("");
  const [attached, setAttached] = useState([]);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  const onBlock = scope === "block" && block;

  // Opening the tab from a recommendation's page attaches that recommendation.
  const ctxId = contextRec?.rec_id ?? null;
  const ctxLabel = contextRec?.rec_label ?? null;
  const blockId = block?.id ?? null;
  useEffect(() => {
    setAttached(ctxId != null ? [{ rec_id: ctxId, label: ctxLabel }] : []);
  }, [ctxId, ctxLabel, blockId]);

  useEffect(() => { if (focusKey) inputRef.current?.focus(); }, [focusKey]);

  // New turns scroll into view.
  const busy = pending != null;
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns.length, busy]);

  function send(question, recsFor = onBlock ? attached : []) {
    if (!question.trim() || pending != null) return;
    onAsk(question, recsFor);
    setText("");
  }

  const available = rankRecs(recs).filter((r) => !attached.some((a) => a.rec_id === r.rec_id));

  return (
    <div className="ask">
      <div className="ask-scroll" ref={scrollRef}>
        <div className="ask-scope" role="tablist" aria-label="What to ask about">
          <button type="button" role="tab" aria-selected={scope === "block"} disabled={!block} onClick={() => onScope("block")}>
            This block
          </button>
          <button type="button" role="tab" aria-selected={scope === "all"} onClick={() => onScope("all")}>
            All {blocksCount} blocks
          </button>
        </div>

        {onBlock && <Briefing recs={recs} fs={fs} zs={zs} onOpenFunding={onOpenFunding} onOpenZoning={onOpenZoning} />}

        <div className="ask-start-head">
          <span className="eyebrow-sm">Good places to start</span>
          {turns.length > 0 && pending == null && (
            <button type="button" className="link" onClick={onClear}>Clear chat</button>
          )}
        </div>
        <div className="ask-starters">
          {starters(onBlock ? block : null, blocksCount).map(([k, q]) => (
            <button type="button" key={k} disabled={pending != null} onClick={() => send(q, [])}>
              <span className="eyebrow-sm">{k}</span>
              <span>{q}</span>
            </button>
          ))}
        </div>

        {turns.map((t, i) => {
          if (t.role === "user") {
            return (
              <div key={t.id} className="ask-q">
                {t.recs?.length > 0 && <div className="ask-q-ctx">{t.recs.map((r) => r.label).join(", ")}</div>}
                {t.text}
              </div>
            );
          }
          if (t.role === "error") {
            return (
              <div key={t.id} className="banner err">
                {t.text}{" "}
                {i === turns.length - 1 && pending == null && (
                  <button type="button" className="link" onClick={() => onRetry(t)}>Try again</button>
                )}
              </div>
            );
          }
          return <AnswerView key={t.id} answer={t.answer} onAction={onAction} />;
        })}
        {pending != null && (
          <div className="ask-pending"><Spark /> {pending}…</div>
        )}
      </div>

      <form className="ask-compose" onSubmit={(e) => { e.preventDefault(); send(text); }}>
        <textarea
          ref={inputRef}
          rows={2}
          value={text}
          placeholder={onBlock ? "Ask about this block, a recommendation, the plan or timing…" : `Ask across all ${blocksCount} blocks…`}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(text);
            }
          }}
        />
        <div className="ask-compose-row">
          <span className="ask-ctx">{onBlock ? `Block ${block.id}` : `All ${blocksCount} blocks`}</span>
          {onBlock && attached.map((r) => (
            <span key={r.rec_id} className="ask-ctx rec">
              {r.label}
              <button type="button" aria-label={`Remove ${r.label}`} onClick={() => setAttached(attached.filter((a) => a.rec_id !== r.rec_id))}>×</button>
            </span>
          ))}
          {onBlock && available.length > 0 && (
            <select
              className="ask-add"
              value=""
              aria-label="Add a recommendation to the question"
              onChange={(e) => {
                const r = available.find((x) => String(x.rec_id) === e.target.value);
                if (r) setAttached([...attached, { rec_id: r.rec_id, label: r.rec_label }]);
              }}
            >
              <option value="">+ Recommendation</option>
              {available.map((r) => <option key={r.rec_id} value={r.rec_id}>{r.rec_label}</option>)}
            </select>
          )}
          <span className="spacer" />
          <button type="submit" className="primary ask-send" disabled={!text.trim() || pending != null} aria-label="Send">
            ➤
          </button>
        </div>
      </form>
    </div>
  );
}
