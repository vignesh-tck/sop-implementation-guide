import { PAGES } from "./nav.js";

// The design stages as a stepper: done stages can be revisited, later ones wait.
export default function Stepper({ view, page, reached, onGo }) {
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
      {view.busy && <span className="pl-progress"><span className="pl-spin" />
        {view.progress || (view.busy === "turn" ? "The agent is working…" : "Running…")}</span>}
    </nav>
  );
}
