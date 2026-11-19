import { useState } from "react";

// The design stages, in the order the user set: schema, sources, the pipeline
// (each step designed and approved), then review and publish.

export const PAGES = [
  { key: "schema", label: "Target schema" },
  { key: "sources", label: "Sources" },
  { key: "steps", label: "Pipeline" },
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
  if (stage === "schema") return 0;
  if (stage === "sources") return 1;
  if (stage === "publish") return 3;
  return 2;
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
