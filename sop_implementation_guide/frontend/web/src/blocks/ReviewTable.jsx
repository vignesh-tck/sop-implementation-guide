import { useState } from "react";
import { money, num, scoreColour } from "../api.js";

export const fitKey = (recId, programName) => `${recId}::${programName}`;

function deadlineCell(iso) {
  if (!iso) return <span className="assess">—</span>;
  const days = Math.round((new Date(iso) - new Date()) / 86400000);
  const soon = days >= 0 && days <= 45;
  const suffix = days < 0 ? " (passed)" : days <= 45 ? ` (${days}d)` : "";
  return <span className={"deadline" + (soon ? " soon" : "")}>{iso}{suffix}</span>;
}

function FitRow({ recId, f, edit, onChange }) {
  const v = edit.fit_score;
  const strong = v != null && v >= edit.floor;
  const amt =
    f.award_amount_min || f.award_amount_max
      ? `${money(f.award_amount_min)} – ${money(f.award_amount_max)}`
      : null;

  return (
    <tr className={strong ? "" : "weak"}>
      <td className="pick">
        <input
          type="checkbox"
          className="fit-pick"
          checked={edit.approved}
          onChange={(e) => onChange({ approved: e.target.checked })}
        />
      </td>
      <td>
        <input
          type="number"
          className={"score-edit" + (edit.edited ? " edited" : "")}
          min="0"
          max="1"
          step="0.05"
          value={v == null ? "" : v}
          style={{ color: edit.edited ? "var(--amber)" : scoreColour(v) }}
          onChange={(e) => {
            const raw = e.target.value;
            const parsed = raw === "" ? null : parseFloat(raw);
            onChange({
              fit_score: parsed,
              edited: true,
              // Editing a score ticks the row — forgetting to keep an edited row
              // was the easiest mistake to make here.
              approved: true,
            });
          }}
        />
      </td>
      <td>
        <div>
          {f.program_name}
          {f.assessment_failed && (
            <span style={{ color: "var(--amber)" }} title="not assessed by model"> ⚠</span>
          )}
        </div>
        <div className="assess">{f.narrative}</div>
        {f.recommended_action && <div className="src">action: {f.recommended_action}</div>}
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
    </tr>
  );
}

function RecGroup({ rec, floor, edits, onChange }) {
  const [showAll, setShowAll] = useState(false);
  const fits = rec.fits || [];
  const strongCount = fits.filter((f) => {
    const e = edits[fitKey(rec.rec_id, f.program_name)];
    return e && e.fit_score != null && e.fit_score >= floor;
  }).length;
  const weak = fits.length - strongCount;
  const gain = rec.predicted_score_increase;
  const down = rec.direction === "Decrease";

  return (
    <div className={"rec-group" + (showAll ? " show-all" : "")}>
      <div className="rec-head">
        {down ? (
          <span className="pill no" title="this block has too much of it">remove/reduce feature</span>
        ) : (
          <span className="pill yes">add/increase feature</span>
        )}
        <h5>{rec.rec_label}</h5>
        <span className="dim">
          {rec.dimension || "—"}
          {gain != null ? ` · predicted +${num(gain, 1)}` : ""}
        </span>
        <span className="spacer" />
        <span className="count">
          {strongCount} of {fits.length} above {floor}
        </span>
        {weak > 0 && (
          <button type="button" onClick={() => setShowAll((s) => !s)}>
            {showAll ? "hide weaker" : `show ${weak} weaker`}
          </button>
        )}
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Keep</th><th>Fit</th><th>Program / why it fits / source</th><th>Deadline</th><th>Award</th>
            </tr>
          </thead>
          <tbody>
            {fits.length === 0 ? (
              <tr><td colSpan={5} className="assess">No programs assessed.</td></tr>
            ) : (
              fits.map((f) => {
                const key = fitKey(rec.rec_id, f.program_name);
                const edit = edits[key] || { fit_score: f.fit_score, approved: false, floor };
                return (
                  <FitRow
                    key={key}
                    recId={rec.rec_id}
                    f={f}
                    edit={{ ...edit, floor }}
                    onChange={(patch) => onChange(key, patch)}
                  />
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function ReviewTable({ review, edits, onChange, feedback, onFeedbackChange }) {
  const recs = review.recommendations || [];
  const floor = review.default_floor != null ? review.default_floor : 0.3;
  const allFits = recs.flatMap((r) => r.fits || []);
  const failed = allFits.filter((f) => f.assessment_failed);

  if (!recs.length) {
    return (
      <div className="card">
        <h4>Human review</h4>
        <div className="banner warn">
          This block has no recommendations, so there is nothing to match funding against.
          Load its State of Place recommendations first.
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <h4>Human review — funding fit per recommendation</h4>
      {failed.length > 0 ? (
        <div className="banner warn">
          <b>{failed.length} of {allFits.length} fits were not assessed by the model.</b>{" "}
          Their 0.50 scores are placeholders, not analysis — check the server log before trusting this run.
        </div>
      ) : (
        <div className="banner ok">
          {recs.length} recommendation(s) × {recs[0].fits ? recs[0].fits.length : 0} program(s) assessed.
          Scores are proposals — edit any of them.
        </div>
      )}
      {recs.map((r) => (
        <RecGroup key={r.rec_id} rec={r} floor={floor} edits={edits} onChange={onChange} />
      ))}
      <div style={{ marginTop: ".85rem" }}>
        <label htmlFor="feedback">Optional note recorded with every signal you keep</label>
        <textarea
          id="feedback"
          rows={2}
          style={{ width: "100%", marginTop: ".3rem" }}
          placeholder="approved"
          value={feedback}
          onChange={(e) => onFeedbackChange(e.target.value)}
        />
      </div>
    </div>
  );
}
