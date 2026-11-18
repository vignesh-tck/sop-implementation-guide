import { useEffect, useState } from "react";
import { call } from "../api.js";
import PipelineBuilder from "./PipelineBuilder.jsx";

// #pipelines lists them; #pipelines/12 opens one, so a pipeline can be linked to.
function idFromHash() {
  const id = Number(window.location.hash.slice(1).split("/")[1]);
  return Number.isFinite(id) && id > 0 ? id : null;
}

function setHash(id) {
  window.history.replaceState(null, "", id ? `#pipelines/${id}` : "#pipelines");
}

export default function PipelinesView() {
  const [openId, setOpenId] = useState(idFromHash);
  const [list, setList] = useState(null);
  const [kinds, setKinds] = useState([]);
  const [error, setError] = useState(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("funding");
  const [creating, setCreating] = useState(false);

  function open(id) {
    setOpenId(id);
    setHash(id);
  }

  useEffect(() => {
    if (openId) return;
    setError(null);
    call("GET", "/pipelines").then(setList).catch((e) => setError(e.message));
    call("GET", "/pipelines/kinds").then(setKinds).catch(() => {});
  }, [openId]);

  async function create() {
    setCreating(true);
    setError(null);
    try {
      const p = await call("POST", "/pipelines", { kind, name: name.trim() || null });
      setName("");
      open(p.id);
    } catch (e) {
      setError(e.message);
    } finally {
      setCreating(false);
    }
  }

  if (openId) return <PipelineBuilder id={openId} onBack={() => open(null)} />;

  return (
    <main className="view">
      <div className="discovery-wrap">
        <h3>Pipeline development</h3>
        <div className="sub">
          Build a data pipeline by chatting with an agent: say which fields you need, share URLs or documents, and
          approve each step it proposes. Each load becomes a numbered dataset version that block analyses can be
          tied to and re-run against.
        </div>
        {error && <div className="banner err">{error}</div>}
        <div className="card">
          <h4>New pipeline</h4>
          <div className="pl-new">
            <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Pipeline kind">
              {(kinds.length ? kinds : [{ kind: "funding", label: "Funding", available: true }]).map((k) => (
                <option key={k.kind} value={k.kind} disabled={!k.available}>
                  {k.label}{k.available ? "" : " (coming next)"}
                </option>
              ))}
            </select>
            <input placeholder="Name (optional, the agent can suggest one)" value={name}
                   onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && create()} />
            <button className="primary" onClick={create} disabled={creating}>{creating ? "Creating…" : "Start"}</button>
          </div>
        </div>
        <div className="card">
          <h4>Pipelines</h4>
          {list === null && !error && <div className="assess">Loading…</div>}
          {list && list.length === 0 && <div className="assess">None yet.</div>}
          {list && list.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Name</th><th>Kind</th><th>Fields</th><th>Sources</th><th>Steps</th><th>Def</th><th>Updated</th></tr></thead>
                <tbody>
                  {list.map((p) => (
                    <tr key={p.id} className="pl-row" onClick={() => open(p.id)}>
                      <td><b>{p.name}</b>{p.busy && <span className="tag edited" style={{ marginLeft: ".4rem" }}>working</span>}</td>
                      <td className="assess">{p.kind}</td>
                      <td className="assess">{p.fields}</td>
                      <td className="assess">{p.sources}</td>
                      <td className="assess">{p.steps}</td>
                      <td className="assess">v{p.definition_version}</td>
                      <td className="assess">{(p.updated_at || "").slice(0, 16).replace("T", " ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
