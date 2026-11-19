import { outputList } from "./ports.js";

// One tab per output, for a pipeline with several (each its own table): its
// name, its record count from the last run, and a dot that is green when it has
// nothing left blocking it.

export default function OutputTabs({ view, value, onChange }) {
  return (
    <div className="out-tabs" role="tablist" aria-label="Outputs">
      {outputList(view).map((o) => {
        const n = o.last_run?.coverage?.records;
        return (
          <button key={o.id} type="button" role="tab" aria-selected={value === o.id} className={value === o.id ? "on" : ""}
                  onClick={() => onChange(o.id)} title={o.gate?.length ? o.gate.join("\n") : `${o.name}: nothing blocking`}>
            <span className={"dot" + (o.gate?.length ? "" : " ok")} />
            {o.name}
            {n != null && <span className="n">{n}</span>}
          </button>
        );
      })}
    </div>
  );
}
