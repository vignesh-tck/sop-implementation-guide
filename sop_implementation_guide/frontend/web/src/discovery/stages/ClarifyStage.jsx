import { useEffect, useState } from "react";

export default function ClarifyStage({ payload, onValueChange }) {
  const [answer, setAnswer] = useState("");

  useEffect(() => {
    onValueChange("skip");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const questions = payload.questions || [];
  const sourcesRead = payload.sources_read || [];
  const failures = payload.fetch_failures || [];

  return (
    <>
      {sourcesRead.length > 0 && (
        <div className="field">
          <label>Sources read</label>
          <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
            {sourcesRead.map((s, i) => (
              <li key={i}><code>{s.url}</code> — {s.chars} chars{s.adapter ? ` via ${s.adapter}` : ""}</li>
            ))}
          </ul>
        </div>
      )}
      {payload.search_keyword && (
        <div className="muted-note">
          Database searches used the keyword <code>{payload.search_keyword}</code>, distilled from your goal.
          If that misses the subject, say so below — the agent reads your answer at every later stage.
        </div>
      )}
      {failures.length > 0 && (
        <div className="banner warn">
          Some sources failed:
          <ul style={{ margin: ".3rem 0 0", paddingLeft: "1.2rem" }}>
            {failures.map((f, i) => <li key={i} className="assess"><code>{f.url}</code> — {f.error}</li>)}
          </ul>
        </div>
      )}
      {payload.notes && <div className="muted-note">{payload.notes}</div>}
      {questions.length ? (
        questions.map((q, i) => (
          <div className="question-item" key={i}>
            <div className="q">{i + 1}. {q.question}</div>
            {q.why && <div className="why">{q.why}</div>}
          </div>
        ))
      ) : (
        <div className="assess">The agent has no clarifying questions.</div>
      )}
      <div className="field" style={{ marginTop: "1rem" }}>
        <label htmlFor="clarify-answer">
          Your answer (free text) — send <code>skip</code> to proceed without answering
        </label>
        <textarea
          id="clarify-answer"
          rows={4}
          placeholder="e.g. City of Bowie via Prince George's County; development on existing city land."
          value={answer}
          onChange={(e) => {
            setAnswer(e.target.value);
            onValueChange(e.target.value.trim() || "skip");
          }}
        />
      </div>
    </>
  );
}
