import { useEffect, useState } from "react";
import { call } from "../api.js";

function SessionDetail({ summary, full }) {
  const s = full || summary;
  const sources = s.sources || [];
  const programs = s.programs || [];
  return (
    <div className="s-body">
      <div className="field"><label>Thread</label><code>{s.thread_id}</code></div>
      <div className="field">
        <label>Sources read</label>
        {sources.length ? (
          <div className="s-sources">{sources.map((u, i) => <div key={i}>{u}</div>)}</div>
        ) : (
          <div className="assess">No sources recorded.</div>
        )}
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Program</th><th>Agency</th><th>Geo scope</th><th>Deadline</th></tr></thead>
          <tbody>
            {programs.length === 0 ? (
              <tr><td colSpan={4} className="assess">This session wrote no programs.</td></tr>
            ) : (
              programs.map((p, i) => {
                let flag = null;
                if (p.still_in_catalogue === false) {
                  flag = <span className="tag gone" title="No longer in funding_programs — deleted since this session ran.">removed</span>;
                } else if (p.superseded_by) {
                  flag = <span className="tag superseded" title={`Re-extracted by session ${p.superseded_by}, which now owns the catalogue row.`}>re-extracted later</span>;
                }
                return (
                  <tr key={i}>
                    <td><b>{p.program_name || ""}</b> {flag}</td>
                    <td className="assess">{p.source_agency || "—"}</td>
                    <td className="assess">{p.geo_scope || "—"}</td>
                    <td className="deadline">{p.deadline || "—"}</td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SessionItem({ s, onRerun }) {
  const [open, setOpen] = useState(false);
  const [full, setFull] = useState(null);
  const [loading, setLoading] = useState(false);
  const when = s.created_at ? new Date(s.created_at).toLocaleString() : "date unknown";
  const n = s.program_count ?? (s.programs || []).length;
  const derived = s.record === "derived" ? (
    <span className="tag derived" title="Reconstructed by grouping funding_programs — this session ran before session history was recorded, so its source list and program count may be incomplete.">reconstructed</span>
  ) : null;

  async function toggle() {
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (!full) {
      setLoading(true);
      try {
        const data = await call("GET", `/funding/discovery/sessions/${encodeURIComponent(s.thread_id)}`);
        setFull(data);
      } finally {
        setLoading(false);
      }
    }
  }

  return (
    <div className="session-item">
      <div className="s-head">
        <span className="s-goal">{s.goal || "(no goal recorded)"} {derived}</span>
        <span className="s-meta">{when} · {n} program{n === 1 ? "" : "s"}</span>
        <span className="s-actions">
          <button onClick={toggle}>{open ? "Close" : "Open"}</button>
          <button onClick={() => onRerun(s)}>Re-run</button>
        </span>
      </div>
      {open && (loading ? <div className="assess">Loading…</div> : <SessionDetail summary={s} full={full} />)}
    </div>
  );
}

export default function SessionHistory({ onRerun }) {
  const [sessions, setSessions] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    call("GET", "/funding/discovery/sessions")
      .then((res) => setSessions(res.sessions || []))
      .catch((e) => setError(e.message));
  }, []);

  return (
    <>
      <h3 style={{ marginTop: "1.5rem" }}>Past discoveries</h3>
      <div className="sub">
        Every session that has been run, newest first. Opening one only reads its history — nothing is re-run unless you start a new session from it.
      </div>
      {error && <div className="banner warn">Could not load past sessions — {error}</div>}
      {!error && sessions === null && <div className="card spin">Loading past sessions…</div>}
      {!error && sessions && sessions.length === 0 && (
        <div className="card"><div className="assess">No discovery sessions yet. The first one you complete will appear here.</div></div>
      )}
      {!error && sessions && sessions.length > 0 && sessions.map((s) => <SessionItem key={s.thread_id} s={s} onRerun={onRerun} />)}
    </>
  );
}
