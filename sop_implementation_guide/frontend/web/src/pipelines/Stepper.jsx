import { PAGES } from "./nav.js";

// The design stages as a stepper: done stages can be revisited, later ones wait.
// While the agent or a run is working, Stop ends it and frees the pipeline at once.
export default function Stepper({ view, page, reached, onGo, onStop }) {
  return (
    <nav className="pl-stepper" aria-label="Pipeline design stages">
      {PAGES.map((p, i) => (
        <button key={p.key} type="button" disabled={i > reached} onClick={() => onGo(i)}
                className={"pl-stage" + (i === page ? " cur" : "") + (i < reached ? " done" : "") + (i === reached ? " now" : "")}
                aria-current={i === page ? "step" : undefined}
                title={i > reached ? `Finish ${PAGES[reached].label.toLowerCase()} first` : p.label}>
          <span className="pl-stage-n">{i < reached ? "✓" : i + 1}</span>
          <span className="pl-stage-label">{p.label}</span>
        </button>
      ))}
      <span className="spacer" />
      {view.busy && <span className="pl-progress" title={view.lost || view.progress || ""}><span className="pl-spin" />
        {view.lost ? "Lost contact with the server, retrying…"
          : view.progress || (view.busy === "turn" ? "The agent is working…" : "Running…")}</span>}
      {view.busy && onStop && <button type="button" className="pl-stop" onClick={onStop}
        title={view.busy === "turn" ? "Stop the agent. What it finished so far is kept." : "Stop the run. What it finished so far is kept."}>Stop</button>}
    </nav>
  );
}
