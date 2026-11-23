import { useEffect, useRef, useState } from "react";
import { call } from "../api.js";
import { Message } from "../pipelines/ChatPane.jsx";

// The playbook agent's chat, in the panel beside the canvas. A message goes to
// the agent with the playbooks as they are open here (and its pending
// proposal, so a follow-up builds on it); the agent's changes come back as a
// proposal, which the Playbooks tab draws on the canvas for the planner to add
// to their draft or discard. Nothing is saved from here.
//
// The conversation is the parent's state (it adds notes such as "You added the
// proposal"), kept in this browser between visits.

const SUGGESTIONS = [
  "Add a playbook for bike share stations, and move “Bike share system” into it",
  "Make sure every Agent step has an SME reviewing its output",
  "In Traffic control and crossings, split the engineering study into a crossing study and a signal warrant study",
];

const POLL_MS = 1500;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

export const EMPTY_CHAT = { messages: [], display: [] };

export default function PlaybookAgent({ chat, setChat, draft, proposal, selectedKey, onProposal, onBusy, hidden }) {
  const [text, setText] = useState("");
  const [pending, setPending] = useState(null);
  const [error, setError] = useState(null);
  const live = useRef(true);
  const end = useRef(null);

  useEffect(() => () => { live.current = false; }, []);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [chat.display.length, pending, hidden]);

  async function send(message) {
    const say = (message ?? text).trim();
    if (!say || pending) return;
    setText("");
    setError(null);
    setPending(say);
    onBusy?.(true);
    const sentDraft = draft;
    try {
      const { job } = await call("POST", "/pathways/agent", {
        text: say, messages: chat.messages, draft, proposal: proposal?.doc || null, selected: selectedKey,
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
      setChat((c) => ({ messages: out.messages, display: [...c.display, ...out.display] }));
      onProposal(out.proposal, sentDraft);
    } catch (e) {
      if (!live.current) return;
      setError(e.message);
      setText(say);
    } finally {
      if (live.current) {
        setPending(null);
        onBusy?.(false);
      }
    }
  }

  return (
    <div className="pb-agent" hidden={hidden}>
      <div className="pb-msgs">
        {chat.display.length === 0 && !pending && (
          <div className="pl-msg agent">
            <p>
              I can change the playbooks for you: add or split steps, change who owns a step or how long it takes, set
              up a new playbook, or move recommendation types between playbooks.
            </p>
            <p>What I change shows on the canvas as a proposal. You add it to your draft, and Save keeps it.</p>
            <div className="pb-suggest">
              {SUGGESTIONS.map((s) => <button key={s} type="button" onClick={() => send(s)}>{s}</button>)}
            </div>
          </div>
        )}
        {chat.display.map((m, i) => <Message key={i} m={m} />)}
        {pending && (
          <>
            <div className="pl-msg user"><p>{pending}</p></div>
            <div className="pl-event"><span className="pl-spin" /> Working on it…</div>
          </>
        )}
        {error && <div className="pl-event error">The agent hit an error: {error}</div>}
        <div ref={end} />
      </div>
      <div className="pb-input">
        <textarea rows={2} value={text} placeholder="Ask for a change to the playbooks…" disabled={!!pending}
                  onChange={(e) => setText(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
        <div className="pb-input-row">
          <button type="button" className="link" disabled={!!pending || !chat.display.length}
                  onClick={() => { if (window.confirm("Start a new conversation? The current one is cleared.")) setChat(EMPTY_CHAT); }}>
            New chat
          </button>
          <span className="spacer" />
          <button type="button" className="primary" onClick={() => send()} disabled={!!pending || !text.trim()}>
            {pending ? "Working…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}
