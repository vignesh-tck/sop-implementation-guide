import { useMemo, useState } from "react";
import Stepper from "./Stepper.jsx";
import PipelineCanvas from "./PipelineCanvas.jsx";
import Inspector from "./Inspector.jsx";
import Drawer from "./Drawer.jsx";
import RecordsView from "./RecordsView.jsx";
import StepDialog from "./StepDialog.jsx";
import SourceDialog from "./SourceDialog.jsx";
import { ancestorsOf, stepsModel } from "./ports.js";

// The Pipeline page: the canvas leads. The selected node opens on the right;
// the agent, the records and Check sit in a drawer underneath; the records can
// also fill the page. The agent proposes changes; the planner adds them to the
// draft, checks and applies.

function pref(key, fallback) {
  try { const v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; }
}
function keep(key, v) {
  try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* storage blocked: the setting lasts this visit */ }
}

function DraftBar({ view, act, disabled, onCheck, onAgent }) {
  const d = view.draft;
  const prop = view.proposal;
  const def = view.definition;
  const canDesign = def.schema?.status === "approved" && def.sources_status === "final";
  const propChip = prop && (
    <button type="button" className="ws-chip prop" onClick={onAgent}>Agent proposal · {prop.changes.length} change{prop.changes.length === 1 ? "" : "s"}</button>
  );
  if (!d) {
    const steps = def.steps || [];
    const approved = steps.filter((s) => s.status === "approved").length;
    return (
      <div className="ws-bar">
        <span className="ws-chip">Applied · def v{view.definition_version} · {approved}/{steps.length} steps approved</span>
        <button disabled={disabled || !canDesign} title={canDesign ? "Change the steps in a draft" : "Approve the schema and mark the sources final first"}
                onClick={() => act("POST", "/draft")}>Edit pipeline</button>
        {propChip}
      </div>
    );
  }
  const c = d.check;
  const n = (d.steps || []).filter((s) => s.draft_state !== "unchanged").length + (d.removed || []).length
    + (d.sources || []).filter((s) => s.draft_state === "new").length + (d.removed_sources || []).length;
  const apply = async () => { if (!(await act("POST", "/draft/apply"))) onCheck(); };
  const discard = () => window.confirm("Discard the draft? The applied steps stay as they are.") && act("DELETE", "/draft");
  return (
    <div className="ws-bar">
      <span className="ws-chip draft">Draft · {n} unapplied change{n === 1 ? "" : "s"}</span>
      <button className="link" disabled={disabled} onClick={discard}>Discard</button>
      <button onClick={onCheck}>✓ Check{c && !c.ok ? ` · ${c.errors.length} error${c.errors.length > 1 ? "s" : ""}` : ""}</button>
      <button className="primary" disabled={disabled || !c?.ok} onClick={apply} title={c?.ok ? "Replace the steps with the draft" : "Fix what Check reports first"}>Apply</button>
      {propChip}
    </div>
  );
}

export default function PipelineWorkspace({ view, act, pending, nav, onBack, error, onClearError, onOpenSource, initialSel }) {
  const disabled = pending || !!view.busy;
  const model = useMemo(() => stepsModel(view), [view]);
  const [mode, setMode] = useState("canvas");          // canvas | records
  const [tab, setTabState] = useState(() => pref("pl.drawer.tab", "agent"));
  const [open, setOpenState] = useState(() => pref("pl.drawer.open", true));
  const [height, setHeightState] = useState(() => pref("pl.drawer.h", 300));
  const setTab = (t) => { setTabState(t); keep("pl.drawer.tab", t); };
  const setOpen = (o) => { setOpenState(o); keep("pl.drawer.open", o); };
  const setHeight = (h) => { setHeightState(h); keep("pl.drawer.h", h); };

  // The selection follows the design: when a step is approved the next one opens.
  const [sel, setSel] = useState(initialSel || (nav.stage.startsWith("step:") ? nav.stage : null));
  const [seenStage, setSeenStage] = useState(nav.stage);
  if (nav.stage !== seenStage) { setSeenStage(nav.stage); if (nav.stage.startsWith("step:")) setSel(nav.stage); }

  // The selected node rides along with the next message, unless removed.
  const [noContext, setNoContext] = useState(null);
  const context = sel && sel !== noContext && sel !== "gaps" ? sel : null;
  const contextLabel = context === "output" ? "Output" : context?.startsWith("src:") ? context.slice(4)
    : context ? `${context.slice(5)} ${view.catalog[model.steps.find((s) => `step:${s.id}` === context)?.type]?.label || ""}` : "";

  const [compose, setCompose] = useState(null);       // text to put in the agent box
  const [trace, setTrace] = useState(null);           // { recordId, field, step } traced on the canvas
  const [recordsOf, setRecordsOf] = useState("final");
  const [recordSel, setRecordSel] = useState(null);
  const [focus, setFocus] = useState(null);           // { ids, at }: the canvas fits these
  const [showProposal, setShowProposal] = useState(true);
  const [dialog, setDialog] = useState(null);
  const [srcDialog, setSrcDialog] = useState(null);

  const known = (id) => id === "output" || model.steps.some((s) => `step:${s.id}` === id) || model.sources.some((s) => `src:${s.id}` === id);
  const focusNodes = (ids) => {
    const list = (ids || []).filter(Boolean);
    setMode("canvas");
    if (list.length === 1 && known(list[0])) setSel(list[0]);
    if (list.length) setFocus({ ids: list, at: Date.now() });
  };
  const askAgent = (text) => { setMode("canvas"); setOpen(true); setTab("agent"); setCompose({ text, at: Date.now() }); };
  const showRecords = (of) => { setRecordsOf(of); setOpen(true); setTab("records"); };

  // Tracing a value lights the path that produced it: the step, the steps before it, their sources.
  const highlight = useMemo(() => {
    if (!trace?.step) return null;
    const live = stepsModel(view, false);
    const ids = new Set([trace.step, ...ancestorsOf(live, trace.step)]);
    const out = new Set([...ids].map((i) => `step:${i}`));
    for (const s of live.steps) {
      if (ids.has(s.id) && s.type === "extract") {
        for (const sid of s.config?.source_ids?.length ? s.config.source_ids : live.sources.map((x) => x.id)) out.add(`src:${sid}`);
      }
    }
    return out;
  }, [trace, view]);

  return (
    <div className="pl-ws">
      <div className="ws-head">
        <button className="link" onClick={onBack}>‹ Pipelines</button>
        <b className="ws-name">{view.name}</b>
        <span className="ws-kind">{view.kind_label} · def v{view.definition_version}</span>
        <Stepper view={view} page={nav.page} reached={nav.reached} onGo={nav.go} />
        <div className="ws-seg" role="tablist" aria-label="View">
          {[["canvas", "Canvas"], ["records", "Records"]].map(([k, t]) => (
            <button key={k} type="button" role="tab" aria-selected={mode === k} className={mode === k ? "on" : ""} onClick={() => setMode(k)}>{t}</button>
          ))}
        </div>
      </div>
      <DraftBar view={view} act={act} disabled={disabled} onCheck={() => { setOpen(true); setTab("check"); }}
                onAgent={() => { setMode("canvas"); setOpen(true); setTab("agent"); }} />
      {error && <div className="banner err ws-error" onClick={onClearError} role="alert">{error}</div>}
      {mode === "records" ? (
        <RecordsView view={view} act={act} disabled={disabled} selected={recordSel} onSelect={setRecordSel}
                     onShowOnCanvas={(recordId, field, step) => { setTrace({ recordId, field, step }); showRecords("final"); focusNodes([`step:${step}`]); }}
                     onAsk={askAgent} />
      ) : (
        <div className={"ws-body" + (sel && known(sel) ? " with-ins" : "")}>
          <div className="ws-canvas">
            <PipelineCanvas view={view} sel={sel} onSelect={setSel} act={act} disabled={disabled} fill
                            highlight={highlight} focus={focus} proposal={showProposal ? view.proposal : null}
                            trace={trace?.step ? `${trace.recordId} · ${trace.field}` : null} onClearTrace={() => setTrace(null)}
                            onAddStep={(m) => setDialog({ kind: "add", ...m })} onAddSource={setSrcDialog}
                            onEditStep={(id) => setDialog({ kind: "edit", stepId: id })}
                            onAskAgent={askAgent} onOpenSource={onOpenSource} onShowCheck={() => { setOpen(true); setTab("check"); }} />
          </div>
          <Drawer view={view} act={act} disabled={disabled} open={open} setOpen={setOpen} height={height} setHeight={setHeight}
                  tab={tab} setTab={setTab} compose={compose} context={context} contextLabel={contextLabel}
                  onClearContext={() => setNoContext(sel)} onFocus={focusNodes} onShowProposal={() => setShowProposal(true)}
                  recordsOf={recordsOf} setRecordsOf={setRecordsOf} trace={trace} setTrace={setTrace}
                  onOpenRecord={(id) => { setRecordSel(id); setMode("records"); }} />
          {sel && known(sel) && (
            <Inspector view={view} act={act} disabled={disabled} sel={sel} onClose={() => setSel(null)} onSelect={(id) => focusNodes([id])}
                       onEditStep={(id) => setDialog({ kind: "edit", stepId: id })} onOpenSource={onOpenSource}
                       onShowRecords={showRecords} onPublish={() => nav.go(3)} />
          )}
        </div>
      )}
      {dialog && <StepDialog view={view} mode={dialog} act={act} onAdded={dialog.onAdded} onClose={() => setDialog(null)} />}
      {srcDialog && <SourceDialog view={view} act={act} at={srcDialog.at} onAdded={srcDialog.onAdded} onClose={() => setSrcDialog(null)} />}
    </div>
  );
}
