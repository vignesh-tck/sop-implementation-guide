import { useEffect, useMemo, useState } from "react";
import { call } from "../../api.js";
import { ACTORS } from "./actors.jsx";
import PlanCanvas from "./PlanCanvas.jsx";
import { changeSummary, marksFrom } from "./graph.js";

// A plan's audit trail, newest first: every draft saved, approval and
// discarded draft, who did it and when, what they wrote, and what it changed.
// Any entry can be drawn on the canvas with its changes marked, against the
// approved plan (or playbook) it was measured from. The playbook the plan
// started from closes the list.

const ACTION = {
  draft: { icon: "✎", cls: "draft", text: (r) => `Draft saved by ${r.made_by}` },
  approved: { icon: "✓", cls: "approved", text: (r) => `Approved by ${r.made_by}` },
  discarded: { icon: "✕", cls: "discarded", text: (r) => `Draft discarded by ${r.made_by}` },
};

const stamp = (t) => {
  if (!t) return "";
  const d = new Date(t);
  return Number.isNaN(d.getTime()) ? t.slice(0, 16) : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};

function ChangeList({ changes }) {
  if (!changes?.count) return <p className="assess">No changes to the steps: the plan was confirmed as it stood.</p>;
  return (
    <ul className="audit-changes">
      {changes.added.map((a) => (
        <li key={"a" + a.id}><span className="pc-flag new">Added</span> Step {a.n}: {a.title || "Untitled step"}
          {a.actor && <span className="assess"> · {ACTORS[a.actor]?.label || a.actor}</span>}</li>
      ))}
      {changes.changed.map((c) => (
        <li key={"c" + c.id}>
          <span className="pc-flag changed">Changed</span> Step {c.n}: {c.title}
          <ul>
            {c.fields.map((f) => (
              <li key={f.field}>{f.label}: <del>{f.before || "—"}</del> → <ins>{f.after || "—"}</ins></li>
            ))}
          </ul>
        </li>
      ))}
      {changes.removed.map((r) => (
        <li key={"r" + r.id}><span className="pc-flag gone">Removed</span> Step {r.n}: {r.title}</li>
      ))}
    </ul>
  );
}

function Entry({ r, open, onToggle }) {
  const a = ACTION[r.action] || ACTION.draft;
  const [canvas, setCanvas] = useState(false);
  const { marks, removed } = useMemo(() => marksFrom(r.changes), [r.changes]);
  return (
    <li className={"audit-entry " + a.cls}>
      <button type="button" className="audit-head" aria-expanded={open} onClick={onToggle}>
        <span className="audit-icon" aria-hidden="true">{a.icon}</span>
        <span className="audit-text">
          <b>{a.text(r)}</b>
          <span className="assess">
            {stamp(r.created_at)}
            {r.action === "approved" && ` · became version ${r.revision}`}
            {r.changes && ` · ${changeSummary(r.changes)}`}
          </span>
          {r.notes && <span className="audit-note">“{r.notes}”</span>}
        </span>
        <span className="chev" aria-hidden="true">{open ? "⌃" : "›"}</span>
      </button>
      {open && (
        <div className="audit-body">
          {r.action === "discarded" ? (
            <p className="assess">The draft before this was dropped; it never became the plan.</p>
          ) : (
            <>
              <div className="assess">
                Compared with {r.base_revision ? `approved version ${r.base_revision}` : "the playbook"}
                {r.template_version ? ` (playbook ${r.template_version})` : ""}.
              </div>
              <ChangeList changes={r.changes} />
              {r.steps && (
                <button type="button" className="ghost" onClick={() => setCanvas((c) => !c)}>
                  {canvas ? "Hide canvas" : "Show on canvas"}
                </button>
              )}
              {canvas && r.steps && <PlanCanvas steps={r.steps} marks={marks} removed={removed} />}
            </>
          )}
        </div>
      )}
    </li>
  );
}

export default function PlanAudit({ blockId, recId, refreshKey }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(null);

  useEffect(() => {
    let live = true;
    setError(null);
    call("GET", `/blocks/${blockId}/recommendations/${recId}/plan/history`)
      .then((d) => {
        if (!live) return;
        setData(d);
        setOpen(d.revisions[0]?.revision ?? null);
      })
      .catch((e) => live && setError(e.message));
    return () => { live = false; };
  }, [blockId, recId, refreshKey]);

  if (error) return <div className="banner err">Could not load the audit trail — {error}</div>;
  if (!data) return <div className="spin">Loading…</div>;

  return (
    <div className="tab-body plan-audit">
      {!data.history_ready && (
        <div className="banner warn">
          The audit trail starts once the IMPLEMENTATION PLAN REVISIONS section of <code>backend/db/schema.sql</code> has been
          run in the Supabase SQL editor.
        </div>
      )}
      {data.history_ready && !data.revisions.length && (
        <p className="assess">Nobody has changed this plan yet. It is the playbook's, as below, until an SME saves a draft.</p>
      )}
      <ol className="audit-list">
        {data.revisions.map((r) => (
          <Entry key={r.revision} r={r} open={open === r.revision}
                 onToggle={() => setOpen((o) => (o === r.revision ? null : r.revision))} />
        ))}
        {data.playbook && (
          <li className="audit-entry origin">
            <button type="button" className="audit-head" aria-expanded={open === "origin"}
                    onClick={() => setOpen((o) => (o === "origin" ? null : "origin"))}>
              <span className="audit-icon" aria-hidden="true">◇</span>
              <span className="audit-text">
                <b>Started from the {data.playbook.name} playbook</b>
                <span className="assess">Playbook {data.playbook.version} · {data.playbook.steps.length} steps</span>
              </span>
              <span className="chev" aria-hidden="true">{open === "origin" ? "⌃" : "›"}</span>
            </button>
            {open === "origin" && <div className="audit-body"><PlanCanvas steps={data.playbook.steps} /></div>}
          </li>
        )}
      </ol>
    </div>
  );
}
