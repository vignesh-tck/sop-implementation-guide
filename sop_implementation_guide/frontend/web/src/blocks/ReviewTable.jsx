import { money, num, scoreColour } from "../api.js";

// One assessed program for one recommendation, with its keep box and editable
// score. Used by the funding review (FundingWorkspace).

function deadlineCell(iso) {
  if (!iso) return <span className="assess">—</span>;
  const days = Math.round((new Date(iso) - new Date()) / 86400000);
  const soon = days >= 0 && days <= 45;
  const suffix = days < 0 ? " (passed)" : days <= 45 ? ` (${days}d)` : "";
  return <span className={"deadline" + (soon ? " soon" : "")}>{iso}{suffix}</span>;
}

export function FitRow({ recId, f, edit, onChange }) {
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
