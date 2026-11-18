import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { call } from "../api.js";

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// The whole fetched text of one source, so the planner can see what the
// pipeline will actually read before marking the sources final.
export default function SourceReader({ pipelineId, source, meta }) {
  // Refetch when the source is replaced or refetched on the server.
  const key = source ? `${source.id}|${meta?.fetched_at || ""}|${meta?.chars ?? ""}` : null;
  const [got, setGot] = useState({ key: null, data: null, error: null });
  const [q, setQ] = useState("");
  const [cur, setCur] = useState(0);
  const pre = useRef(null);

  const sid = source?.id;
  const unreadable = !!meta?.error;
  useEffect(() => {
    if (!sid || unreadable) return;
    let live = true;
    call("GET", `/pipelines/${pipelineId}/sources/${sid}/text`)
      .then((data) => live && setGot({ key, data, error: null }))
      .catch((e) => live && setGot({ key, data: null, error: e.message }));
    return () => { live = false; };
  }, [pipelineId, sid, unreadable, key]);

  const text = got.key === key ? got.data?.text ?? "" : "";
  const term = q.trim().length >= 2 ? q.trim() : "";
  const parts = term ? text.split(new RegExp(`(${escapeRe(term)})`, "i")) : [text];
  const hits = (parts.length - 1) / 2;
  const at = hits ? ((cur % hits) + hits) % hits : 0;

  // Bring the current match into view inside the reader (not the whole page).
  useLayoutEffect(() => {
    const box = pre.current, m = box?.querySelector("mark.cur");
    if (box && m) box.scrollTop = Math.max(0, m.offsetTop - box.clientHeight / 3);
  }, [at, term, text]);

  if (!source) {
    return <div className="card pl-reader"><div className="assess">Add a source and its text appears here.</div></div>;
  }
  const data = got.key === key ? got.data : null;
  const loading = !meta?.error && got.key !== key;

  return (
    <div className="card pl-reader">
      <div className="pl-reader-head">
        <b className="pl-id">{source.id}</b>
        <b className="pl-reader-title" title={source.label}>{source.label}</b>
      </div>
      <div className="assess pl-reader-meta">
        {(data?.final_url || source.url) && <a href={data?.final_url || source.url} target="_blank" rel="noreferrer">{data?.final_url || source.url}</a>}
        {data && <span>{data.content_type || "text"} · {data.chars.toLocaleString()} chars{data.truncated ? " (truncated: only this much is read)" : ""}</span>}
      </div>
      {meta?.error ? <div className="banner err">{meta.error}</div>
        : got.key === key && got.error ? <div className="banner err">{got.error}</div>
        : loading ? <div className="assess">Loading the text…</div>
        : (
          <>
            <div className="pl-reader-bar">
              <input placeholder="Find in this source" value={q} onChange={(e) => { setQ(e.target.value); setCur(0); }}
                     onKeyDown={(e) => e.key === "Enter" && setCur((c) => c + (e.shiftKey ? -1 : 1))} />
              {term && <span className="assess">{hits ? `${at + 1} of ${hits}` : "no matches"}</span>}
              <button disabled={!hits} title="Previous match" onClick={() => setCur((c) => c - 1)}>‹</button>
              <button disabled={!hits} title="Next match" onClick={() => setCur((c) => c + 1)}>›</button>
            </div>
            <pre className="pl-reader-text" ref={pre}>
              {text ? parts.map((p, i) => (i % 2 ? <mark key={i} className={(i - 1) / 2 === at ? "cur" : ""}>{p}</mark> : p))
                : <i>The source was read but contains no text.</i>}
            </pre>
          </>
        )}
    </div>
  );
}
