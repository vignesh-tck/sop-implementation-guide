import { useEffect, useState } from "react";
import { num } from "../../api.js";

const EMPTY_PROGRAM = {
  program_name: "", program_type: null, source_agency: "",
  award_amount_min: null, award_amount_max: null, deadline: null,
  eligibility_notes: "", application_url: "", source_url: "",
  source_excerpt: "", confidence: null, excerpt_verified: null,
};

function ProgramCard({ pr, index, onField, onToggleReject }) {
  const verifiedCls = pr.excerpt_verified === true ? "verified" : pr.excerpt_verified === false ? "unverified" : "";
  const verifiedBadge =
    pr.excerpt_verified === true ? <span className="pill yes">excerpt verified</span>
    : pr.excerpt_verified === false ? <span className="pill no">excerpt NOT verified</span>
    : null;
  const val = (v) => (v == null ? "" : v);

  return (
    <div className={"program-edit " + verifiedCls + (pr.__rejected ? " rejected" : "")}>
      <div className="card-head">
        <span className="card-title">
          <span className="card-n">Program {index + 1}</span>
          {verifiedBadge}
          {pr.__rejected && <span className="drop-flag">rejected — not saved</span>}
        </span>
        <button type="button" className="drop-btn" onClick={onToggleReject}>
          {pr.__rejected ? "Restore" : "Reject"}
        </button>
      </div>
      <div className="row">
        <div><label>Program name</label>
          <input disabled={pr.__rejected} value={val(pr.program_name)} onChange={(e) => onField("program_name", e.target.value)} /></div>
        <div><label>Source agency</label>
          <input disabled={pr.__rejected} value={val(pr.source_agency)} onChange={(e) => onField("source_agency", e.target.value)} /></div>
      </div>
      <div className="row">
        <div><label>Program type</label>
          <select disabled={pr.__rejected} value={val(pr.program_type)} onChange={(e) => onField("program_type", e.target.value || null)}>
            <option value="">—</option>
            <option value="federal">federal</option>
            <option value="state">state</option>
            <option value="local">local</option>
          </select></div>
        <div><label>Deadline (YYYY-MM-DD)</label>
          <input disabled={pr.__rejected} placeholder="YYYY-MM-DD" value={val(pr.deadline)} onChange={(e) => onField("deadline", e.target.value || null)} /></div>
      </div>
      <div className="row">
        <div><label>Award min ($)</label>
          <input disabled={pr.__rejected} type="number" step="1" value={val(pr.award_amount_min)}
                 onChange={(e) => onField("award_amount_min", e.target.value === "" ? null : Number(e.target.value))} /></div>
        <div><label>Award max ($)</label>
          <input disabled={pr.__rejected} type="number" step="1" value={val(pr.award_amount_max)}
                 onChange={(e) => onField("award_amount_max", e.target.value === "" ? null : Number(e.target.value))} /></div>
      </div>
      <div className="row full">
        <div><label>Eligibility notes</label>
          <textarea disabled={pr.__rejected} rows={2} value={val(pr.eligibility_notes)} onChange={(e) => onField("eligibility_notes", e.target.value)} /></div>
      </div>
      <div className="row">
        <div><label>Application URL</label>
          <input disabled={pr.__rejected} value={val(pr.application_url)} onChange={(e) => onField("application_url", e.target.value)} /></div>
        <div><label>Source URL</label>
          <input disabled={pr.__rejected} value={val(pr.source_url)} onChange={(e) => onField("source_url", e.target.value)} /></div>
      </div>
      <div className="row full">
        <div>
          <label>
            Source excerpt (verbatim)
            {pr.confidence != null && <span className="muted-note" style={{ margin: 0 }}> — confidence {num(pr.confidence, 2)}</span>}
          </label>
          <textarea disabled={pr.__rejected} rows={3} value={val(pr.source_excerpt)} onChange={(e) => onField("source_excerpt", e.target.value)} />
        </div>
      </div>
    </div>
  );
}

export default function ExtractStage({ payload, onValueChange }) {
  const [programs, setPrograms] = useState(() => (payload.programs || []).map((p) => ({ ...p, __rejected: false })));

  useEffect(() => {
    const kept = programs
      .filter((p) => !p.__rejected)
      .map(({ __rejected, ...rest }) => rest)
      .filter((p) => p.program_name && p.program_name.trim());
    onValueChange({ programs: kept });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [programs]);

  const verifiedCount = (payload.programs || []).filter((p) => p.excerpt_verified).length;
  const total = (payload.programs || []).length;
  const kept = programs.filter((p) => !p.__rejected).length;

  return (
    <>
      {total > 0 ? (
        <div className={"banner " + (verifiedCount === total ? "ok" : "warn")}>
          {verifiedCount}/{total} excerpts verified against fetched text. Unverified ones may have been invented — check before approving.
        </div>
      ) : (
        <div className="banner warn">No programs were extracted. Add sources or fill in manually.</div>
      )}
      {payload.notes && <div className="muted-note">{payload.notes}</div>}
      <div className="muted-note">Reject the programmes you do not want — only the kept ones are carried forward.</div>
      <div>
        {programs.map((pr, i) => (
          <ProgramCard
            key={i}
            pr={pr}
            index={i}
            onField={(field, value) =>
              setPrograms((prev) => prev.map((p, idx) => (idx === i ? { ...p, [field]: value } : p)))
            }
            onToggleReject={() =>
              setPrograms((prev) => prev.map((p, idx) => (idx === i ? { ...p, __rejected: !p.__rejected } : p)))
            }
          />
        ))}
      </div>
      <div className="btn-row">
        <button type="button" onClick={() => setPrograms((prev) => [...prev, { ...EMPTY_PROGRAM, __rejected: false }])}>
          + Add empty program
        </button>
      </div>
      <div className="muted-note">
        {kept === programs.length
          ? <>Keeping all <b>{programs.length}</b> programme(s).</>
          : <>Keeping <b>{kept}</b> of {programs.length} programme(s) — {programs.length - kept} rejected.</>}
      </div>
      <div className="muted-note">Submitting sends <code>{"{"}"programs": [...]{"}"}</code> with your edits included.</div>
    </>
  );
}
