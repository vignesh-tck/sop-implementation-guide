import { Fragment } from "react";
import { num, score100Colour } from "../../api.js";

// One assistant answer: the direct answer, a few lines backing it, at most one
// chart or list (drawn from numbers the server read from the data, never from
// the model), what it assumes, next steps, and the data it read.

const WEEKS_PER_MONTH = 4.345;

// "**bold**" inside a line.
function inline(text) {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4 ? <b key={i}>{part.slice(2, -2)}</b> : <Fragment key={i}>{part}</Fragment>
  );
}

// Paragraphs, and "- " lines as bullets.
export function RichText({ text }) {
  const blocks = [];
  for (const raw of (text || "").split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const bullet = /^[-•*]\s+/.test(line);
    const last = blocks[blocks.length - 1];
    if (bullet && last?.type === "ul") last.items.push(line.replace(/^[-•*]\s+/, ""));
    else if (bullet) blocks.push({ type: "ul", items: [line.replace(/^[-•*]\s+/, "")] });
    else blocks.push({ type: "p", text: line });
  }
  return (
    <div className="rich">
      {blocks.map((b, i) =>
        b.type === "ul" ? (
          <ul key={i}>{b.items.map((t, j) => <li key={j}>{inline(t)}</li>)}</ul>
        ) : (
          <p key={i}>{inline(b.text)}</p>
        )
      )}
    </div>
  );
}

function TimelineChart({ v }) {
  const bars = v.bars || [];
  const maxWeeks = Math.max(...bars.map((b) => b.start_weeks[1] + b.weeks[1]), 1);
  const months = maxWeeks / WEEKS_PER_MONTH;
  const step = months > 36 ? 12 : months > 12 ? 6 : months > 4 ? 3 : 1;
  const end = Math.ceil(months / step) * step;
  const pct = (w) => `${Math.min(100, (w / WEEKS_PER_MONTH / end) * 100)}%`;
  const ticks = Array.from({ length: end / step + 1 }, (_, i) => i * step);
  const phased = v.arrangement === "phases";

  return (
    <div className="ans-chart">
      <div className="ans-legend">
        <span><i className="sw dark" /> Earliest finish</span>
        <span><i className="sw light" /> Could run to</span>
        {v.total && <span className="ans-total">All of them: <b>{v.total}</b></span>}
      </div>
      {bars.map((b, i) => {
        const head = phased && (i === 0 || bars[i - 1].phase !== b.phase);
        return (
          <Fragment key={b.rec_id}>
            {head && <div className="tl-phase">Phase {b.phase}{v.phase_names?.[b.phase - 1] ? ` · ${v.phase_names[b.phase - 1]}` : ""}</div>}
            <div className="tl-row">
              <div className="tl-name">
                <span>{b.label}</span>
                <small>{b.text}{b.untimed_steps?.length ? ` · ${b.untimed_steps.length} untimed step${b.untimed_steps.length === 1 ? "" : "s"}` : ""}</small>
              </div>
              <div className="tl-track">
                <span className="tl-bar light" style={{ left: pct(b.start_weeks[0]), width: `calc(${pct(b.start_weeks[1] + b.weeks[1])} - ${pct(b.start_weeks[0])})` }} />
                <span className="tl-bar dark" style={{ left: pct(b.start_weeks[0]), width: `calc(${pct(b.start_weeks[0] + b.weeks[0])} - ${pct(b.start_weeks[0])})` }} />
              </div>
            </div>
          </Fragment>
        );
      })}
      <div className="tl-row tl-axis">
        <div className="tl-name" />
        <div className="tl-track bare">
          {ticks.map((m) => (
            <span key={m} style={{ left: `${(m / end) * 100}%` }}>{m === 0 ? "0" : m % 12 === 0 && end > 24 ? `${m / 12} yr` : `${m} mo`}</span>
          ))}
        </div>
      </div>
      {v.untimed?.length > 0 && (
        <div className="assess">No duration for {v.untimed.map((u) => `${u.label} (${u.why})`).join(", ")}.</div>
      )}
    </div>
  );
}

function ScoresChart({ v }) {
  return (
    <div className="ans-chart">
      <div className="ans-legend">
        <span><i className="sw dark" /> This block</span>
        <span><i className="sw avg" /> Average of all blocks</span>
        <span className="ans-total">SoP <b>{num(v.sop)}</b> vs {num(v.sop_average)} average</span>
      </div>
      {v.rows.map((r) => (
        <div className="tl-row" key={r.dimension}>
          <div className="tl-name"><span>{r.dimension}</span><small>{num(r.score)} · avg {num(r.average)}</small></div>
          <div className="tl-track">
            <span className="tl-bar" style={{ left: 0, width: `${r.score ?? 0}%`, background: score100Colour(r.score) }} />
            {r.average != null && <span className="tl-avg" style={{ left: `${r.average}%` }} title={`Average ${num(r.average)}`} />}
          </div>
        </div>
      ))}
    </div>
  );
}

function RecRows({ v, onAction }) {
  return (
    <div className="ans-rows">
      {v.rows.map((r) => (
        <button type="button" key={r.rec_id} onClick={() => onAction({ type: "open_recommendation", rec_id: r.rec_id })}>
          <span><b>{r.label}</b><small>{r.dimension}{r.duration ? ` · ~${r.duration}` : ""}</small></span>
          {r.gain != null && <span className="rec-gain">{r.direction === "Decrease" ? "" : "+"}{num(r.gain)}</span>}
        </button>
      ))}
    </div>
  );
}

function BlockRows({ v, onAction }) {
  return (
    <>
      <div className="ans-rows">
        {v.rows.map((r) => (
          <button type="button" key={r.block_id} onClick={() => onAction({ type: "open_block", block_ids: [r.block_id] })}>
            <span><b>{r.street}</b> <span className="meta">#{r.block_id}</span></span>
            <span className="meta">SoP {num(r.sop)}</span>
          </button>
        ))}
      </div>
      <button type="button" className="ans-soft" onClick={() => onAction({ type: "highlight_blocks", block_ids: v.rows.map((r) => r.block_id) })}>
        Highlight these on the map
      </button>
    </>
  );
}

export default function AnswerView({ answer, onAction }) {
  const v = answer.visual;
  return (
    <div className="ans">
      <div className="ans-headline">{answer.headline}</div>
      {answer.body && <RichText text={answer.body} />}
      {v?.type === "timeline" && <TimelineChart v={v} />}
      {v?.type === "scores" && <ScoresChart v={v} />}
      {v?.type === "recommendations" && <RecRows v={v} onAction={onAction} />}
      {v?.type === "blocks" && <BlockRows v={v} onAction={onAction} />}
      {answer.assumptions?.length > 0 && (
        <div className="ans-assume">
          <div className="eyebrow-sm">What this assumes</div>
          <ul>{answer.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        </div>
      )}
      {answer.actions?.length > 0 && (
        <div className="ans-actions">
          {answer.actions.map((a, i) => (
            <button type="button" key={i} className={i === 0 ? "primary" : i === 1 ? "ans-outline" : ""} onClick={() => onAction(a)}>
              {a.label}
            </button>
          ))}
        </div>
      )}
      {answer.sources?.length > 0 && (
        <div className="ans-sources">
          Sources:
          {answer.sources.map((s) => <span key={s.label} className="src" title={s.detail}>{s.label}</span>)}
        </div>
      )}
    </div>
  );
}
