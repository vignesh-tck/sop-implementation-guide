import { Fragment, useEffect, useRef, useState } from "react";
import { uploadFiles } from "../api.js";

// Just enough formatting for the agent's replies: paragraphs, line breaks,
// **bold** and `code`. Built as elements, never as HTML.
function Rich({ text }) {
  return (text || "").split(/\n{2,}/).map((para, i) => (
    <p key={i}>
      {para.split("\n").map((line, j) => (
        <Fragment key={j}>
          {j > 0 && <br />}
          {line.split(/(\*\*[^*]+\*\*|`[^`]+`)/).map((part, k) =>
            part.startsWith("**") && part.endsWith("**") ? <b key={k}>{part.slice(2, -2)}</b>
              : part.startsWith("`") && part.endsWith("`") ? <code key={k}>{part.slice(1, -1)}</code>
                : part)}
        </Fragment>
      ))}
    </p>
  ));
}

function Message({ m }) {
  if (m.kind === "event") {
    if (m.level === "intro") return <div className="pl-msg agent"><Rich text={m.text} /></div>;
    return <div className={"pl-event " + (m.level || "info")}>{m.text}</div>;
  }
  if (m.kind === "user") return <div className="pl-msg user"><Rich text={m.text} /></div>;
  return (
    <div className="pl-msg agent">
      {m.text && <Rich text={m.text} />}
      {m.tools?.length > 0 && (
        <div className="pl-tools">
          {m.tools.map((t) => (
            <span key={t.id} className={"pl-tool" + (t.error ? " err" : "")} title={t.error || t.detail}>
              {t.error ? "⚠ " : "✎ "}{t.label}{t.detail ? <span className="d"> · {t.detail}</span> : null}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export default function ChatPane({ view, act, onBack }) {
  const [text, setText] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState(null);
  const bottom = useRef(null);
  const busy = !!view.busy;

  useEffect(() => { bottom.current?.scrollIntoView({ block: "end" }); }, [view.messages.length, busy]);

  async function send() {
    const t = text.trim();
    if (!t || busy) return;
    const r = await act("POST", "/messages", { text: t });
    if (r) setText("");
  }

  async function attach(e) {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (!files.length) return;
    setUploading(true);
    setUploadError(null);
    try {
      const res = await uploadFiles(files);
      for (const u of res.uploaded) await act("POST", "/sources", { kind: "url", url: u.url, label: u.filename });
      if (res.errors?.length) setUploadError(res.errors.map((er) => `${er.filename}: ${er.error}`).join("; "));
    } catch (err) {
      setUploadError(err.message);
    } finally {
      setUploading(false);
    }
  }

  return (
    <>
      <div className="pl-chat-head">
        <button className="link" onClick={onBack}>← Pipelines</button>
        <span className="assess">{view.kind_label} pipeline · chat</span>
      </div>
      <div className="pl-messages">
        {view.messages.map((m) => <Message key={m.id} m={m} />)}
        {busy && (
          <div className="pl-event info">
            <span className="spin">●</span> {view.busy === "run" ? "Running the pipeline" : "The agent is working"}
            {view.progress ? ` · ${view.progress}` : "…"}
          </div>
        )}
        <div ref={bottom} />
      </div>
      {uploadError && <div className="banner err" style={{ margin: "0 .75rem .5rem" }}>{uploadError}</div>}
      <div className="pl-compose">
        <textarea
          rows={3} value={text} disabled={busy}
          placeholder={busy ? "Wait for the agent…" : "Describe the goal and fields, paste URLs, ask for changes… (Enter to send, Shift+Enter for a new line)"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
        />
        <div className="pl-compose-actions">
          <label className={"pl-attach" + (busy || uploading ? " disabled" : "")} title="Upload .pdf, .csv or .txt as a source">
            <input type="file" multiple accept=".txt,.csv,.pdf" onChange={attach} disabled={busy || uploading} />
            {uploading ? "Uploading…" : "Attach file"}
          </label>
          <button className="primary" onClick={send} disabled={busy || !text.trim()}>Send</button>
        </div>
      </div>
    </>
  );
}
