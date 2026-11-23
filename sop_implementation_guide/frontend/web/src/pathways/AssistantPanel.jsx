import { useEffect, useImperativeHandle, useRef, useState } from "react";
import { call, uploadFiles } from "../api.js";
import { Message } from "../pipelines/ChatPane.jsx";
import { numbers } from "../blocks/plan/graph.js";
import { describe } from "./suggestions.js";

// The playbook assistant, in the panel beside the canvas.
//
// "Propose changes" sends the message with the playbooks as they stand
// (draft plus pending suggestions); whatever the assistant changes comes back
// to the parent (onResult) as new suggestions. "Just answer" only answers.
// Below the chat: the pending suggestions as cards to accept or reject one by
// one, and gaps a rule found in the open playbook ("Also noticed").
//
// The conversation is the parent's state, kept in this browser. The parent can
// send a message on the planner's behalf (a toolbar action) with ref.send().

const POLL_MS = 1500;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
export const EMPTY_CHAT = { messages: [], display: [] };

const ICON = { add_playbook: "+", add_step: "+", edit_step: "✎", edit_playbook: "✎", link: "⇢", unlink: "⇢",
               remove_step: "−", remove_playbook: "−", map_rec: "→" };

function Card({ op, doc, onAccept, onReject, onShow }) {
  const d = describe(op, doc);
  return (
    <div className={"sg-card k-" + op.kind}>
      <span className="sg-icon" aria-hidden="true">{ICON[op.kind] || "•"}</span>
      <div className="sg-main">
        <div className="sg-tag">{d.tag}</div>
        <div className="sg-title">{d.title}</div>
        {d.detail && <div className="sg-detail">{d.detail}</div>}
        {op.reason && <div className="sg-why">{op.reason}</div>}
        <div className="sg-actions">
          <button type="button" className="primary" onClick={() => onAccept(op)}>Accept</button>
          <button type="button" onClick={() => onReject(op)}>Reject</button>
          {onShow && <button type="button" className="link" onClick={() => onShow(op)}>Show on canvas</button>}
        </div>
      </div>
    </div>
  );
}

export default function AssistantPanel({
  ref, chat, setChat, mode, setMode, draft, working, playbook, version, others, pending, gaps,
  onResult, onAccept, onReject, onShow, onAcceptAll, onDiscardAll, onBusy, hidden,
}) {
  const [text, setText] = useState("");
  const [pendingMsg, setPendingMsg] = useState(null);
  const [error, setError] = useState(null);
  const [files, setFiles] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [mention, setMention] = useState(null);   // {query, start}
  const live = useRef(true);
  const end = useRef(null);
  const input = useRef(null);
  const fileInput = useRef(null);

  useEffect(() => () => { live.current = false; }, []);
  useEffect(() => { if (!hidden) end.current?.scrollIntoView({ block: "end" }); }, [chat.display.length, pendingMsg, hidden, pending.length]);

  async function send(message, asMode = mode) {
    const say = (message ?? text).trim();
    if (!say || pendingMsg) return;
    const attached = files;
    setText("");
    setFiles([]);
    setMention(null);
    setError(null);
    setPendingMsg(say);
    onBusy?.(true);
    const sentDraft = draft, sentWorking = working;
    try {
      const { job } = await call("POST", "/pathways/agent", {
        text: say, messages: chat.messages, working: sentWorking, selected: playbook?.key || null,
        mode: asMode, files: attached,
      });
      let r;
      for (;;) {
        await wait(POLL_MS);
        if (!live.current) return;
        r = await call("GET", `/pathways/agent/${job}`);
        if (r.status !== "running") break;
      }
      if (r.status === "failed") throw new Error(r.error);
      const out = r.result;
      const shown = attached.length ? `${say}\n\n📎 ${attached.map((f) => f.name).join(", ")}` : say;
      setChat((c) => ({ messages: out.messages, display: [...c.display, { kind: "user", text: shown }, ...out.display] }));
      if (asMode !== "answer") onResult(out, sentDraft, sentWorking);
    } catch (e) {
      if (!live.current) return;
      setError(e.message);
      setText(say);
      setFiles(attached);
    } finally {
      if (live.current) {
        setPendingMsg(null);
        onBusy?.(false);
      }
    }
  }

  useImperativeHandle(ref, () => ({ send, focus: () => input.current?.focus() }));

  async function attach(list) {
    if (!list?.length) return;
    setUploading(true);
    setError(null);
    try {
      const r = await uploadFiles([...list]);
      setFiles((f) => [...f, ...r.uploaded.map((u) => ({ name: u.filename, url: u.url }))]);
      if (r.errors?.length) setError(r.errors.map((e) => `${e.filename}: ${e.error}`).join("; "));
    } catch (e) {
      setError(e.message);
    } finally {
      setUploading(false);
    }
  }

  // "@" starts a step mention; picking one writes "@step 3 (title)".
  const steps = playbook?.steps || [];
  const num = numbers(steps);
  function onType(e) {
    const v = e.target.value;
    setText(v);
    const upto = v.slice(0, e.target.selectionStart);
    const m = upto.match(/@([\w ]{0,20})$/);
    setMention(m ? { query: m[1].toLowerCase(), start: upto.length - m[0].length } : null);
  }
  const matches = mention
    ? steps.filter((s) => `${num[s.id]} ${s.title}`.toLowerCase().includes(mention.query.replace(/^step\s*/, ""))).slice(0, 6)
    : [];
  function pickMention(s) {
    const before = text.slice(0, mention.start), after = text.slice(mention.start + 1 + mention.query.length);
    setText(`${before}@step ${num[s.id]} (${s.title}) ${after}`);
    setMention(null);
    input.current?.focus();
  }

  const compare = others?.[0];
  const chips = [
    ["Find gaps", "Find gaps in this playbook: missing steps, owners or durations, and branches that lead nowhere. Suggest fixes.", "propose"],
    ["Estimate missing durations", "Estimate a duration for every step in this playbook that has none.", "propose"],
    ["Explain to a new planner", "Explain this playbook to a planner new to Bowie: what happens, who does what, and where it can branch or stall.", "answer"],
    ...(compare ? [[`Compare with ${compare.name}`, `Compare this playbook with the ${compare.name} playbook: steps, owners and durations that differ and whether they should.`, "answer"]] : []),
  ];

  return (
    <div className="pb-agent" hidden={hidden}>
      <div className="pb-head">
        <div className="pb-sees">Sees: <b>{playbook?.name || "no playbook"}</b>{playbook ? ` · ${steps.length} steps` : ""} · {version}</div>
        <span className="seg" role="group" aria-label="Mode">
          <button type="button" aria-pressed={mode === "propose"} onClick={() => setMode("propose")}>Propose changes</button>
          <button type="button" aria-pressed={mode === "answer"} onClick={() => setMode("answer")}>Just answer</button>
        </span>
      </div>

      <div className="pb-msgs">
        {chat.display.length === 0 && !pendingMsg && (
          <div className="pl-msg agent">
            <p>
              Ask me to change this playbook (add a path, split a step, fix owners or durations) or to draft a new one.
              What I suggest shows on the canvas; you accept or reject each change, then save.
            </p>
            <p>Type @ to point at a step. Attach a document and I'll cite its pages.</p>
          </div>
        )}
        {chat.display.map((m, i) => <Message key={i} m={m} />)}
        {pendingMsg && (
          <>
            <div className="pl-msg user"><p>{pendingMsg}</p></div>
            <div className="pl-event"><span className="pl-spin" /> {mode === "answer" ? "Thinking…" : "Working on suggestions…"}</div>
          </>
        )}
        {error && <div className="pl-event error">{error}</div>}

        {pending.length > 0 && (
          <div className="sg-list">
            <div className="sg-list-head">
              <b>Proposed changes</b><span className="sg-count">{pending.length} pending</span>
              <span className="spacer" />
              <button type="button" className="link" onClick={onDiscardAll}>Discard all</button>
              <button type="button" className="link" onClick={onAcceptAll}>Accept all</button>
            </div>
            {pending.map((op) => (
              <Card key={op.id} op={op} doc={working} onAccept={onAccept} onReject={onReject}
                    onShow={op.step_id || op.to || op.playbook ? onShow : null} />
            ))}
          </div>
        )}

        {gaps.length > 0 && (
          <div className="pb-noticed">
            <div className="pb-noticed-h">⚠ Also noticed</div>
            {gaps.slice(0, 5).map((g) => (
              <div key={g.id} className="pb-noticed-row">
                <span>{g.text}</span>
                <button type="button" onClick={() => send(g.ask, "propose")} disabled={!!pendingMsg}>{g.action}</button>
              </div>
            ))}
          </div>
        )}
        <div ref={end} />
      </div>

      <div className="pb-chips">
        {chips.map(([label, ask, m]) => (
          <button key={label} type="button" onClick={() => send(ask, m)} disabled={!!pendingMsg || !playbook}>{label}</button>
        ))}
      </div>

      <div className="pb-input">
        {mention && matches.length > 0 && (
          <div className="pb-mentions" role="listbox">
            {matches.map((s) => (
              <button key={s.id} type="button" role="option" onMouseDown={(e) => { e.preventDefault(); pickMention(s); }}>
                <b>Step {num[s.id]}</b> {s.title}
              </button>
            ))}
          </div>
        )}
        <textarea ref={input} rows={2} value={text} disabled={!!pendingMsg}
                  placeholder={mode === "answer" ? "Ask about the playbooks…" : "Ask or describe a change. Type @ to point at a step."}
                  onChange={onType}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") setMention(null);
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      if (mention && matches.length) pickMention(matches[0]);
                      else send();
                    }
                  }} />
        {files.length > 0 && (
          <div className="pb-files">
            {files.map((f, i) => (
              <span key={f.url} className="pb-file">📎 {f.name}
                <button type="button" className="link" onClick={() => setFiles((x) => x.filter((_, j) => j !== i))} aria-label={`Remove ${f.name}`}>✕</button>
              </span>
            ))}
          </div>
        )}
        <div className="pb-input-row">
          <input ref={fileInput} type="file" multiple accept=".pdf,.csv,.txt" hidden onChange={(e) => { attach(e.target.files); e.target.value = ""; }} />
          <button type="button" className="pb-attach" onClick={() => fileInput.current?.click()} disabled={uploading || !!pendingMsg}
                  title="Attach a document (.pdf, .csv, .txt)" aria-label="Attach a document">{uploading ? "…" : "📎"}</button>
          <button type="button" className="link" disabled={!!pendingMsg || !chat.display.length}
                  onClick={() => { if (window.confirm("Start a new conversation? The chat is cleared; pending suggestions stay.")) setChat(EMPTY_CHAT); }}>
            New chat
          </button>
          <span className="spacer" />
          <button type="button" className="primary" onClick={() => send()} disabled={!!pendingMsg || !text.trim()}>
            {pendingMsg ? "Working…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}
