import { useEffect, useRef, useState } from "react";
import { call, uploadFiles } from "../api.js";
import SessionHistory from "./SessionHistory.jsx";

export default function StartForm({ onStarted, banner }) {
  const [goal, setGoal] = useState("");
  const [sources, setSources] = useState("");
  const [uploads, setUploads] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState(null);
  const [fields, setFields] = useState(null);
  const [fieldsError, setFieldsError] = useState(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState(null);
  const goalRef = useRef(null);

  useEffect(() => {
    call("GET", "/funding/discovery/fields")
      .then((res) => setFields(res.fields || []))
      .catch((e) => setFieldsError(e.message));
  }, []);

  // Re-run from history only prefills — the user still presses Start, and the
  // result is a brand-new thread; the past session stays exactly as it was.
  function rerunFrom(s) {
    setGoal(s.goal && s.goal.startsWith("(") ? "" : s.goal || "");
    setSources((s.sources || []).join("\n"));
    goalRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    goalRef.current?.focus();
  }

  async function handleFiles(e) {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (!files.length) return;
    setUploading(true);
    setUploadError(null);
    try {
      const res = await uploadFiles(files);
      setUploads((prev) => [...prev, ...res.uploaded]);
      if (res.errors && res.errors.length) {
        setUploadError(res.errors.map((er) => `${er.filename}: ${er.error}`).join("; "));
      }
    } catch (e) {
      setUploadError(e.message);
    } finally {
      setUploading(false);
    }
  }

  async function startSession() {
    const trimmedGoal = goal.trim();
    const sourceList = [
      ...sources.split(/\r?\n/).map((s) => s.trim()).filter(Boolean),
      ...uploads.map((u) => u.url),
    ];
    if (!trimmedGoal) return alert("Enter a goal.");
    if (!sourceList.length) return alert("Add at least one source URL or uploaded file.");
    setStarting(true);
    setStartError(null);
    try {
      const state = await call("POST", "/funding/discovery", { goal: trimmedGoal, sources: sourceList });
      onStarted(state, trimmedGoal);
    } catch (e) {
      setStartError(e.message);
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="discovery-wrap">
      <h3>Funding discovery — new session</h3>
      <div className="sub">
        Paste one or more source URLs, or upload documents, for the agent to read. Nothing is written to the silver layer until you confirm every stage.
      </div>
      {banner}
      {startError && <div className="banner err">Failed to start — {startError}</div>}
      <div className="card">
        <div className="field">
          <label htmlFor="d-goal">Goal — what are you trying to fund?</label>
          <textarea
            id="d-goal" ref={goalRef} rows={3}
            placeholder="e.g. Find state and local funding to build a public garden on a residential block in Bowie, Maryland. The eligibility rules are in the sidebar, not the main text."
            value={goal} onChange={(e) => setGoal(e.target.value)}
          />
          <div className="muted-note">
            The goal is an instruction to the agent, not a search box. It is passed to the agent at every reading
            stage, so hints about how to read the pages belong here too — which sections matter, what to ignore,
            what counts as one programme. A short keyword is distilled from it separately for database searches,
            so writing in full sentences costs you nothing.
          </div>
        </div>
        <div className="field">
          <label htmlFor="d-sources">Sources — one URL per line</label>
          <textarea
            id="d-sources" rows={4}
            placeholder="https://dnr.maryland.gov/land/Pages/ProgramOpenSpace/home.aspx"
            value={sources} onChange={(e) => setSources(e.target.value)}
          />
          <div className="muted-note" style={{ marginTop: ".4rem" }}>
            Or upload your own documents (.txt, .csv, .pdf) — they are read the same way as a pasted URL.
          </div>
          <input type="file" multiple accept=".txt,.csv,.pdf" onChange={handleFiles} disabled={uploading} />
          {uploading && <div className="assess">Uploading…</div>}
          {uploadError && <div className="banner err">{uploadError}</div>}
          {uploads.length > 0 && (
            <ul className="sources-list" style={{ marginTop: ".4rem" }}>
              {uploads.map((u, i) => (
                <li key={i}>
                  <span className="url">{u.filename}</span>
                  <button className="rm" type="button" title="Remove"
                          onClick={() => setUploads((prev) => prev.filter((_, idx) => idx !== i))}>×</button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="field">
          <label>What gets recorded for each programme</label>
          {fieldsError && <div className="assess">Could not load the field list ({fieldsError}). The session will still run.</div>}
          {!fieldsError && fields === null && <div className="assess">Loading…</div>}
          {!fieldsError && fields && (
            <ul className="sources-list" style={{ margin: 0 }}>
              {fields.map((f, i) => (
                <li key={i}>
                  <code>{f.field}</code>
                  <span style={{ fontSize: ".75rem" }}>{f.description || ""}</span>
                  {f.optional && <span className="pill">left blank if unstated</span>}
                </li>
              ))}
            </ul>
          )}
          <div className="muted-note" style={{ marginTop: ".4rem" }}>
            Nothing outside this list is captured. Fields the source does not state are left blank rather than
            guessed, and you correct every value before it is saved.
          </div>
        </div>
        <div className="btn-row">
          <button className="primary" onClick={startSession} disabled={starting}>
            {starting ? "Starting…" : "Start session"}
          </button>
        </div>
        <div className="muted-note">
          The agent will fetch each source, ask any clarifying questions, report what the pages actually contain,
          propose extractions with verifiable excerpts, and recommend a geographic tie. You review each step.
        </div>
      </div>

      <SessionHistory onRerun={rerunFrom} />
    </div>
  );
}
