import { Fragment } from "react";

// Just enough formatting for the agent's replies: paragraphs, line breaks,
// **bold** and `code`. Built as elements, never as HTML.
export function Rich({ text }) {
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

export function Message({ m }) {
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
