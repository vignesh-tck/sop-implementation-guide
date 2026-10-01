import { useState } from "react";
import { money, num, scoreColour } from "../api.js";

function deadlineCell(iso) {
  if (!iso) return <span className="assess">—</span>;
  const days = Math.round((new Date(iso) - new Date()) / 86400000);
  const soon = days >= 0 && days <= 45;
  const suffix = days < 0 ? " (passed)" : days <= 45 ? ` (${days}d)` : "";
  return <span className={"deadline" + (soon ? " soon" : "")}>{iso}{suffix}</span>;
}

function SignalRow({ f }) {
  const amt =
    f.award_amount_min || f.award_amount_max
      ? `${money(f.award_amount_min)} – ${money(f.award_amount_max)}`
      : null;
  return (
    <tr>
      <td className="fit" style={{ color: scoreColour(f.fit_score) }}>
        {num(f.fit_score, 2)}
        {f.score_adjusted_by_reviewer && (
          <span className="tag derived" title="the reviewer changed the model's proposed score">edited</span>
        )}
      </td>
      <td>
        <div>
          {f.program_name}
          {f.assessment_failed && (
            <span style={{ color: "var(--amber)" }} title="the model did not assess this — 0.50 is a placeholder"> ⚠</span>
          )}
        </div>
        <div className="assess">{f.narrative}</div>
        {f.recommended_action && <div className="src">action: {f.recommended_action}</div>}
        {f.human_notes && <div className="src">reviewer note: {f.human_notes}</div>}
        {f.source_name ? (
          <div className="src">
            {f.is_extracted && (
              <span className="curated">
                extracted{f.extraction_confidence != null ? ` ${num(f.extraction_confidence, 2)}` : ""}
              </span>
            )}
            {f.is_extracted && " · "}
            {f.source_url ? (
              <a href={f.source_url} target="_blank" rel="noopener noreferrer">{f.source_name}</a>
            ) : (
              f.source_name
            )}
          </div>
        ) : (
          <div className="src">no source recorded</div>
        )}
      </td>
      <td>{deadlineCell(f.deadline)}</td>
      <td className="assess">{amt || <span className="assess">varies</span>}</td>
      <td>
        {f.application_url ? (
          <a className="apply" href={f.application_url} target="_blank" rel="noopener noreferrer">apply ↗</a>
        ) : (
          <span className="assess">—</span>
        )}
      </td>
    </tr>
  );
}

function SignalTable({ fits }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>Fit</th><th>Program / why it fits / source</th><th>Deadline</th><th>Award</th><th></th></tr>
        </thead>
        <tbody>{fits.map((f, i) => <SignalRow key={f.signal_id ?? i} f={f} />)}</tbody>
      </table>
    </div>
  );
}

function SignalGroup({ g, defaultOpen }) {
  const [open, setOpen] = useState(defaultOpen);
  const fits = g.fits || [];
  const best = fits.reduce((m, f) => Math.max(m, f.fit_score || 0), 0);
  const gain = g.predicted_score_increase;
  const down = g.direction === "Decrease";

  return (
    <div className="rec-group ro">
      <div className="rec-head" role="button" tabIndex={0} aria-expanded={open}
           onClick={() => setOpen((o) => !o)}
           onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen((o) => !o); } }}>
        <span className="caret">{open ? "▾" : "▸"}</span>
        {down ? (
          <span className="pill no" title="this block has too much of it">remove/reduce feature</span>
        ) : (
          <span className="pill yes">add/increase feature</span>
        )}
        <h5>{g.rec_label}</h5>
        <span className="dim">
          {g.dimension || "—"}
          {gain != null ? ` · predicted +${num(gain, 1)}` : ""}
        </span>
        <span className="spacer" />
        <span className="count">
          {fits.length} program(s) · best fit <b style={{ color: scoreColour(best) }}>{num(best, 2)}</b>
        </span>
      </div>
      {open && <SignalTable fits={fits} />}
    </div>
  );
}

function UnlinkedGroup({ fits }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rec-group ro unlinked">
      <div className="rec-head" role="button" tabIndex={0} aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className="caret">{open ? "▾" : "▸"}</span>
        <h5>Not linked to a recommendation</h5>
        <span className="dim">scored against the block as a whole</span>
        <span className="spacer" />
        <span className="count">{fits.length} signal(s)</span>
      </div>
      {open && <SignalTable fits={fits} />}
    </div>
  );
}

export default function SignalsView({ signals }) {
  const recs = signals.recommendations || [];
  const unlinked = signals.unlinked || [];
  if (!recs.length && !unlinked.length) return null;

  return (
    <div className="card">
      <h4>Funding detail by recommendation</h4>
      <div className="kv" style={{ marginBottom: ".65rem" }}>
        {signals.signal_count} approved signal(s) · {recs.length} of {signals.recommendations_total} recommendation(s)
        on this block have funding · read-only view of <code>funding_signals</code>
      </div>
      {unlinked.length > 0 && (
        <div className="banner warn">
          <b>{unlinked.length} signal(s) predate per-recommendation analysis</b> and carry no <code>rec_id</code>,
          so they cannot be attributed to a specific improvement. Re-run <b>Analyze</b> to replace them.
        </div>
      )}
      {recs.map((g, i) => <SignalGroup key={g.rec_id} g={g} defaultOpen={i === 0} />)}
      {unlinked.length > 0 && <UnlinkedGroup fits={unlinked} />}
    </div>
  );
}
