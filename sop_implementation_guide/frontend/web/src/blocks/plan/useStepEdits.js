import { useState } from "react";
import { nextId, numbers, topoOrder, wouldLoop } from "./graph.js";

// Editing steps on the canvas: the same moves in an implementation plan and in
// a playbook. setSteps takes an updater function, like a useState setter.
// markEdited flags each touched step as the SME's (plans do; playbooks don't
// keep the flag).
export function useStepEdits(steps, setSteps, { onError, markEdited = true } = {}) {
  const [selected, setSelected] = useState(null);
  const num = numbers(steps);
  const flag = markEdited ? { edited: true } : {};

  function patch(id, fields) {
    setSteps((b) => {
      const next = b.map((s) => (s.id === id ? { ...s, ...fields, ...flag } : s));
      return "depends_on" in fields ? topoOrder(next) : next;
    });
  }

  // A new step goes after the given one (or at the end) and waits on it.
  function addAfter(id) {
    const at = id ? steps.findIndex((s) => s.id === id) : steps.length - 1;
    const prev = steps[at];
    const step = {
      id: nextId(steps), title: "", how: "", actor: prev?.actor || "city", owner: "", secondary_owner: "", time: "",
      depends_on: prev ? [prev.id] : [], depends_mode: "all", filled_by: "template", ...flag,
    };
    setSteps((b) => {
      const i = id ? b.findIndex((s) => s.id === id) : b.length - 1;
      const next = [...b];
      next.splice(i + 1, 0, step);
      return next;
    });
    setSelected(step.id);
  }

  // A copy goes right after the step, waiting on what it waits on.
  function duplicate(id) {
    const src = steps.find((s) => s.id === id);
    if (!src) return;
    const copy = { ...src, id: nextId(steps), title: `${src.title} (copy)`, ...flag };
    setSteps((b) => {
      const i = b.findIndex((s) => s.id === id);
      const next = [...b];
      next.splice(i + 1, 0, copy);
      return next;
    });
    setSelected(copy.id);
  }

  // A removed step's followers take over what it waited on, so the chain stays joined.
  function remove(id) {
    const gone = steps.find((x) => x.id === id);
    if (!gone) return;
    if (!window.confirm(`Remove step ${num[id]}${gone.title ? ` ("${gone.title}")` : ""}? Steps after it will wait on what it waited on.`)) return;
    setSteps((b) => topoOrder(b.filter((x) => x.id !== id).map((x) => {
      if (!(x.depends_on || []).includes(id)) return x;
      const deps = [...new Set([...(x.depends_on || []).filter((d) => d !== id), ...(gone.depends_on || [])])];
      return { ...x, depends_on: deps, ...flag };
    })));
    setSelected(null);
  }

  function connect(from, to) {
    const target = steps.find((s) => s.id === to);
    if (!target || (target.depends_on || []).includes(from)) return;
    if (wouldLoop(steps, from, to)) {
      onError?.(`Step ${num[from]} already waits on step ${num[to]}, so step ${num[to]} can't wait on it.`);
      return;
    }
    onError?.(null);
    patch(to, { depends_on: [...(target.depends_on || []), from] });
  }

  function disconnect(from, to) {
    const target = steps.find((s) => s.id === to);
    if (!target || !window.confirm(`Step ${num[to]} no longer waits on step ${num[from]}?`)) return;
    const deps = (target.depends_on || []).filter((d) => d !== from);
    patch(to, { depends_on: deps, ...(deps.length < 2 ? { depends_mode: "all" } : {}) });
  }

  return { selected, setSelected, patch, addAfter, duplicate, remove, connect, disconnect };
}
