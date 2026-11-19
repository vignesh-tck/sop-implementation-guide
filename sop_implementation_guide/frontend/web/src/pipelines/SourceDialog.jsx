import { useEffect, useState } from "react";
import { uploadFiles } from "../api.js";

// Add a source from the canvas: a URL, a Grants.gov search (funding), or an
// uploaded file. While sources are being collected it is added straight away;
// once they are final it goes into the draft, read by no step until it is wired.

export default function SourceDialog({ view, act, at, onClose, onAdded }) {
  const [tab, setTab] = useState("url");
  const [url, setUrl] = useState("");
  const [query, setQuery] = useState("");
  const [label, setLabel] = useState("");
  const [files, setFiles] = useState([]);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const final = view.definition.sources_status === "final";

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const known = () => new Set([...(view.definition.sources || []), ...(view.draft?.sources || [])].map((s) => s.id));
  const newIds = (r, before) => [...(r?.definition?.sources || []), ...(r?.draft?.sources || [])]
    .map((s) => s.id).filter((id, i, all) => !before.has(id) && all.indexOf(id) === i);

  async function add() {
    setSaving(true);
    setError(null);
    const before = known();
    try {
      let r;
      if (tab === "url") {
        r = await act("POST", "/sources", { kind: "url", url: url.trim(), label: label.trim() || null }, { throwErrors: true });
      } else if (tab === "grants_gov") {
        r = await act("POST", "/sources", { kind: "grants_gov", query: query.trim(), label: label.trim() || null }, { throwErrors: true });
      } else {
        const res = await uploadFiles(files);
        for (const u of res.uploaded) r = await act("POST", "/sources", { kind: "url", url: u.url, label: u.filename }, { throwErrors: true });
        if (res.errors?.length) throw new Error(res.errors.map((er) => `${er.filename}: ${er.error}`).join("; "));
      }
      newIds(r, before).forEach((id, i) => onAdded?.(id, at ? { x: at.x, y: at.y + i * 80 } : null));
      onClose();
    } catch (e) {
      setError(e.message.replace(/^\d+ — /, ""));
    } finally {
      setSaving(false);
    }
  }

  const ready = tab === "url" ? /^https?:\/\//i.test(url.trim()) : tab === "grants_gov" ? !!query.trim() : files.length > 0;
  return (
    <div className="pl-modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="pl-modal narrow" role="dialog" aria-modal="true" aria-label="Add a source">
        <div className="pl-modal-head">
          <b>Add a source</b>
          <span className="spacer" />
          <button className="link" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {error && <div className="banner err" style={{ margin: ".5rem .9rem 0" }}>{error}</div>}
        <div className="pl-modal-body">
          <section className="pl-modal-col">
            <div className="pl-tabs" role="tablist">
              {[["url", "URL"], ...(view.kind === "funding" ? [["grants_gov", "Grants.gov search"]] : []), ["file", "Upload a file"]].map(([k, t]) => (
                <button key={k} type="button" role="tab" aria-selected={tab === k} className={"pl-tab" + (tab === k ? " on" : "")} onClick={() => setTab(k)}>{t}</button>
              ))}
            </div>
            {tab === "url" && (
              <label className="pl-form-row"><span className="pl-form-label">URL</span>
                <input autoFocus placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)}
                       onKeyDown={(e) => e.key === "Enter" && ready && add()} /></label>
            )}
            {tab === "grants_gov" && (
              <label className="pl-form-row"><span className="pl-form-label">Keywords</span>
                <input autoFocus placeholder="e.g. pedestrian safety" value={query} onChange={(e) => setQuery(e.target.value)}
                       onKeyDown={(e) => e.key === "Enter" && ready && add()} /></label>
            )}
            {tab === "file" && (
              <label className="pl-form-row"><span className="pl-form-label">Files</span>
                <input type="file" multiple accept=".txt,.csv,.pdf" onChange={(e) => setFiles(Array.from(e.target.files || []))} />
                <span className="assess">.pdf, .csv or .txt; each becomes a source.</span></label>
            )}
            {tab !== "file" && (
              <label className="pl-form-row"><span className="pl-form-label">Label</span>
                <input placeholder="optional" value={label} onChange={(e) => setLabel(e.target.value)} /></label>
            )}
            <div className="muted-note" style={{ marginTop: 0 }}>
              {final ? "The sources are final, so this goes into the draft: it is fetched now, but no step reads it until you wire it to an Extract step (or add one for it), and it applies with the draft."
                : "It is fetched now, so a bad link shows straight away."}
            </div>
            <div className="btn-row">
              <button className="primary" disabled={!ready || saving} onClick={add}>{saving ? "Fetching…" : final ? "Add to draft" : "Add source"}</button>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
