import { useState } from "react";

// Two pages: the Pipeline canvas, where the design happens in the order the
// user set (approve the schema on the Output node, mark the sources final, then
// design and approve each step), and Review & publish.

export const PAGES = [
  { key: "pipeline", label: "Pipeline" },
  { key: "review", label: "Review & publish" },
];

// Where the design currently waits.
export function stageOf(view) {
  const d = view.definition;
  const steps = d.steps || [];
  if (d.schema?.status !== "approved") return "schema";
  if (d.sources_status !== "final") return "sources";
  if (steps.length === 0) return "steps";
  const open = steps.find((s) => s.status !== "approved");
  if (open) return `step:${open.id}`;
  return "publish";
}

export function pageOf(stage) {
  return stage === "publish" ? 1 : 0;
}

// The page follows the design: finishing a stage slides the next one in, and a
// change that reopens an earlier stage (from the chat, say) slides back to it.
export function usePageNav(view) {
  const stage = view ? stageOf(view) : "schema";
  const reached = pageOf(stage);
  const [nav, setNav] = useState({ page: reached, dir: "fwd" });
  const [seenReached, setSeenReached] = useState(reached);
  if (reached !== seenReached) {
    setSeenReached(reached);
    setNav({ page: reached, dir: reached >= nav.page ? "fwd" : "back" });
  }
  const page = Math.min(nav.page, reached);
  const go = (i) => i !== page && setNav({ page: i, dir: i > page ? "fwd" : "back" });
  return { page, reached, stage, dir: nav.dir, go };
}
