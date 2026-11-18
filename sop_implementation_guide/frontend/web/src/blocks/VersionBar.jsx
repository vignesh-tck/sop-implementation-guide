import { useState } from "react";

// Which Funding version this block's funding comes from, and every run of it.
//
// A block's funding is one run (a Block Funding analysis) against one Funding
// dataset version. The current run is the one plans and gold read. A new run
// can be made against any version, and any earlier complete run can be looked
// at or made current again, so blocks can sit on different versions.

const when = (iso) => (iso || "").slice(0, 16).replace("T", " ");

export function versionLabel(versions, n) {
  if (n == null) return "the live catalogue";
  const v = (versions || []).find((x) => x.version === n);
  return `Funding v${n}${v?.name ? ` “${v.name}”` : ""}`;
}

function runLabel(a) {
  return `run #${a.id}${a.name ? ` “${a.name}”` : ""}`;
}

const STATUS_TEXT = {
  running: "running", in_review: "awaiting review", complete: "complete", failed: "failed", discarded: "discarded",
};

export default function VersionBar({ block, s, actions, busy }) {
  const [open, setOpen] = useState(false);
  const id = block.id;
  const versions = s.versions || [];
  const analyses = (s.analyses || []).filter((a) => a.status !== "discarded");
  const current = analyses.find((a) => a.pinned) || null;
  const viewing = s.viewing ? analyses.find((a) => a.id === s.viewing) : null;
  const latest = versions[0]?.version ?? null;
  const reviewing = s.thread && !s.thread.approved ? s.thread : null;
  const goldStale = s.profile && current && s.profile.funding_analysis_id !== current.id;
  const waiting = analyses.filter((a) => a.status === "in_review" && !(reviewing && reviewing.analysis_id === a.id));

  return (
    <div className="vbar">
      <div className="vbar-line">
        <span className="vbar-k">Current funding</span>
        {current ? (
          <span>
            <b>{versionLabel(versions, current.dataset_version)}</b> · {runLabel(current)} ·{" "}
            {current.result?.signals ?? "?"} match(es) · {when(current.completed_at || current.created_at)}
            {current.dataset_version != null && latest != null && current.dataset_version < latest && (
              <span className="tag derived" style={{ marginLeft: ".4rem" }} title="A newer Funding version exists. Run against it to update this block.">
                v{latest} available
              </span>
            )}
          </span>
        ) : (
          <span className="assess">No approved run yet.</span>
        )}
        <span className="spacer" />
        <button type="button" className="link small" onClick={() => setOpen((o) => !o)}>
          {open ? "Hide runs" : `All runs (${analyses.length})`}
        </button>
      </div>

      {reviewing && (
        <div className="vbar-line">
          <span className="vbar-k">Reviewing</span>
          <span>{reviewing.analysis_id ? `run #${reviewing.analysis_id}` : "a new run"}{reviewing.name ? ` “${reviewing.name}”` : ""} against{" "}
            <b>{versionLabel(versions, reviewing.dataset_version)}</b>. Approving makes it the block's current funding.</span>
        </div>
      )}

      {!reviewing && !s.analyzing && (
        <div className="vbar-line vbar-run">
          <span className="vbar-k">Next run against</span>
          <select value={s.nextVersion ?? ""} disabled={busy || !versions.length}
                  onChange={(e) => actions.setRunOptions(id, { nextVersion: e.target.value === "" ? null : Number(e.target.value) })}>
            <option value="">{latest != null ? `Latest (v${latest}${versions[0]?.name ? ` “${versions[0].name}”` : ""})` : "Latest"}</option>
            {versions.map((v) => (
              <option key={v.id} value={v.version}>
                v{v.version}{v.name ? ` “${v.name}”` : ""} · {v.summary?.programs ?? v.record_count} programmes · {when(v.created_at).slice(0, 10)}
              </option>
            ))}
          </select>
          <input placeholder="Run name (optional)" value={s.runName} disabled={busy}
                 onChange={(e) => actions.setRunOptions(id, { runName: e.target.value })} />
        </div>
      )}

      {viewing && (
        <div className="banner warn" style={{ margin: ".5rem 0 0" }}>
          Viewing {runLabel(viewing)} on <b>{versionLabel(versions, viewing.dataset_version)}</b>, read-only. It is not this
          block's current funding.{" "}
          {viewing.status === "complete" && (
            <button type="button" className="link small" onClick={() => actions.pin(id, viewing.id)}>Make it current</button>
          )}{" "}
          <button type="button" className="link small" onClick={() => actions.view(id, null)}>Back to current</button>
        </div>
      )}

      {waiting.length > 0 && !open && (
        <div className="assess" style={{ marginTop: ".35rem" }}>
          {waiting.length} run(s) awaiting review. Open <i>All runs</i> to resume or discard.
        </div>
      )}

      {goldStale && !reviewing && (
        <div className="vbar-line" style={{ marginTop: ".35rem" }}>
          <span className="warn-text">The gold profile was computed from a different run than the current one.</span>
          <button type="button" className="link small" disabled={s.computingGold} onClick={() => actions.computeGold(id)}>
            {s.computingGold ? "Recomputing…" : "Recompute gold"}
          </button>
        </div>
      )}

      {s.historyError && <div className="banner err" style={{ margin: ".5rem 0 0" }}>{s.historyError}</div>}

      {open && (
        <div className="table-wrap vbar-runs">
          <table>
            <thead><tr><th>Run</th><th>Funding version</th><th>Status</th><th>Matches</th><th>When</th><th></th></tr></thead>
            <tbody>
              {analyses.length === 0 && <tr><td colSpan={6} className="assess">No runs yet.</td></tr>}
              {analyses.map((a) => (
                <tr key={a.id} className={a.id === s.viewing ? "sel" : ""}>
                  <td>
                    #{a.id}{a.name ? ` “${a.name}”` : ""}
                    {a.pinned && <span className="tag ok" style={{ marginLeft: ".35rem" }}>current</span>}
                  </td>
                  <td className="assess">{versionLabel(versions, a.dataset_version)}</td>
                  <td className="assess" title={a.error || a.note || ""}>
                    {STATUS_TEXT[a.status] || a.status}
                    {a.status === "in_review" && a.review_live === false && " (review lost on restart)"}
                  </td>
                  <td className="assess">{a.result?.signals ?? ""}</td>
                  <td className="assess">{when(a.completed_at || a.created_at)}{a.created_by ? ` · ${a.created_by}` : ""}</td>
                  <td className="nowrap">
                    {a.status === "complete" && !a.pinned && (
                      <>
                        <button type="button" className="link small" disabled={busy} onClick={() => actions.view(id, a.id)}>View</button>{" "}
                        <button type="button" className="link small" disabled={busy} onClick={() => actions.pin(id, a.id)}>Make current</button>
                      </>
                    )}
                    {a.status === "complete" && a.pinned && s.viewing && (
                      <button type="button" className="link small" onClick={() => actions.view(id, null)}>View</button>
                    )}
                    {a.status === "in_review" && a.review_live && !(reviewing && reviewing.analysis_id === a.id) && (
                      <button type="button" className="link small" disabled={busy} onClick={() => actions.resume(id, a)}>Resume review</button>
                    )}{" "}
                    {["running", "in_review", "failed"].includes(a.status) && !(s.analyzing && a.status === "running") && (
                      <button type="button" className="link small" disabled={busy} onClick={() => actions.discard(id, a.id)}>Discard</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="muted-note">
            Each run is tied to the Funding version it assessed. Making a run current changes what this block's plans and
            gold read; other blocks are not affected. New versions come from the Pipeline development tab.
          </div>
        </div>
      )}
    </div>
  );
}
