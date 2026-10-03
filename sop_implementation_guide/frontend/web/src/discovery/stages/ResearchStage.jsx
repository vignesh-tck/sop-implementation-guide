import { useEffect, useRef, useState } from "react";
import { uploadFiles } from "../../api.js";

export default function ResearchStage({ payload, onValueChange }) {
  const initial = useRef((payload.current_sources || []).slice());
  const [sources, setSources] = useState(initial.current.slice());
  const [newUrl, setNewUrl] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState(null);

  async function handleFiles(e) {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (!files.length) return;
    setUploading(true);
    setUploadError(null);
    try {
      const res = await uploadFiles(files);
      setSources((prev) => [...prev, ...res.uploaded.map((u) => u.url)]);
      if (res.errors && res.errors.length) {
        setUploadError(res.errors.map((er) => `${er.filename}: ${er.error}`).join("; "));
      }
    } catch (err) {
      setUploadError(err.message);
    } finally {
      setUploading(false);
    }
  }

  useEffect(() => {
    const same =
      sources.length === initial.current.length && sources.every((u, i) => u === initial.current[i]);
    onValueChange(same ? "approved" : { sources });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sources]);

  const assessments = payload.assessments || [];
  const suggested = payload.suggested_sources || [];

  return (
    <>
      {payload.summary && <div className="muted-note" style={{ marginBottom: ".75rem" }}>{payload.summary}</div>}
      {payload.search_keyword && (
        <div className="muted-note" style={{ marginBottom: ".75rem" }}>
          Web search ran on <code>{payload.search_keyword}</code>
          {!payload.web_search_available &&
            " — but web search was unavailable, so suggestions come from the fetched pages only"}
          .
        </div>
      )}
      <div className="field">
        <label>Source assessments (from agent)</label>
        {assessments.length === 0 ? (
          <div className="assess">No assessments returned.</div>
        ) : (
          assessments.map((a, i) => (
            <div className={"assess-item " + (a.usable ? "usable" : "unusable")} key={i}>
              <div className="assess-url">{a.url} {!a.usable && <span className="pill no">unusable</span>}</div>
              <div className="assess-body"><b>Contains:</b> {a.contains || "—"}</div>
              <div className="assess-body"><b>Missing:</b> {a.missing || "—"}</div>
            </div>
          ))
        )}
      </div>
      <div className="field">
        <label>Expected program count: <b>{payload.expected_program_count ?? "?"}</b></label>
      </div>
      {suggested.length > 0 && (
        <div className="field">
          <label>Suggested additional sources — click + to add</label>
          <ul className="sources-list">
            {suggested.map((s, i) => (
              <li key={i}>
                <button className="rm" type="button" title="Add to source list"
                        onClick={() => setSources((prev) => [...prev, s.url])}>+</button>
                <span className="url">{s.url}</span>
                <span className="assess" style={{ fontSize: ".75rem" }}>{s.why || ""}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="field">
        <label>Sources to extract from (edit freely)</label>
        <ul className="sources-list">
          {sources.length === 0 ? (
            <li className="assess">(empty)</li>
          ) : (
            sources.map((u, i) => (
              <li key={i}>
                <span className="url">{u}</span>
                <button className="rm" title="Remove"
                        onClick={() => setSources((prev) => prev.filter((_, idx) => idx !== i))}>×</button>
              </li>
            ))
          )}
        </ul>
        <div className="btn-row" style={{ marginTop: ".5rem" }}>
          <input placeholder="https://…" style={{ flex: 1 }} value={newUrl}
                 onChange={(e) => setNewUrl(e.target.value)} />
          <button type="button" onClick={() => {
            if (newUrl.trim()) { setSources((prev) => [...prev, newUrl.trim()]); setNewUrl(""); }
          }}>Add</button>
        </div>
        <div className="btn-row" style={{ marginTop: ".5rem" }}>
          <input type="file" multiple accept=".txt,.csv,.pdf" onChange={handleFiles} disabled={uploading} />
          {uploading && <span className="assess">Uploading…</span>}
        </div>
        {uploadError && <div className="banner err">{uploadError}</div>}
        <div className="muted-note">Submitting the current list unchanged sends <code>"approved"</code>. Any edit sends the new list.</div>
      </div>
    </>
  );
}
