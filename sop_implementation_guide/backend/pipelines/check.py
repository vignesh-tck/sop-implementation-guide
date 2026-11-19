"""
Check ("compile") a pipeline's steps before they are applied.

Errors stop Apply; warnings are shown but do not. Checked:
- the wiring: every step takes an input (an Extract step may start a branch),
  sends its output to exactly one place, and reaches the output; no loops;
- each step's settings, as the catalog and schema require (SQL is dry-run);
- the sources: each read by one Extract step, since records are keyed by source
  (a CSV source may instead be joined as a table by SQL steps);
- merges: every branch into a step (or into the output) must hand on the same
  schema. A step's port schema says, per field, whether the records carry it
  and whether it is already typed, so merging a branch that parsed a money
  field with one that did not is an error, not a quiet mix of numbers and text.
  A SQL step whose query reads its inputs as tables is a join, not a merge:
  the branches may differ, and fields it does not write keep its first input's
  schema (its rows are records of the inputs, usually the first).
"""

from __future__ import annotations

import re

from backend.pipelines import engine, graph
from backend.pipelines.engine import PipelineError

# Field state on a port. None: no step on the way produces it.
TEXT = "text"       # numeric field as read from a source: text such as "$250K" until a Parse step
SET = "set"         # produced, in its type
UNTYPED = ("money", "number", "percent")   # read as text; other types are normalised when read

STATE_TEXT = {None: "not produced", TEXT: "unparsed text", SET: "filled"}


def _fresh(fields: list[dict]) -> dict:
    """The schema of records an Extract step reads from its sources."""
    return {f["name"]: ((TEXT if f["type"] in UNTYPED else SET) if f.get("extract", True) else None) for f in fields}


def _after(step: dict, port: dict, fields: list[dict], warn, sources: list[dict]) -> dict:
    """The port schema after a step, from the one before it."""
    t, c = step["type"], step.get("config") or {}
    types = {f["name"]: f["type"] for f in fields}
    out = dict(port)
    writes = engine.step_io(step, fields, sources).get("writes") or []
    if t == "research":
        for w in writes:
            out[w] = TEXT if types.get(w) in UNTYPED else SET
    elif t == "parse":
        if port.get(c.get("field")) is None:
            warn(step["id"], f"{step['id']} parses {c.get('field')}, which no step before it produces.")
        out[c.get("field")] = SET
    elif t == "map_values":
        if port.get(c.get("field")) is None:
            warn(step["id"], f"{step['id']} maps {c.get('field')}, which no step before it produces.")
    elif t in ("set_value", "llm_fill", "geo_tie", "sql"):
        for w in writes:
            out[w] = SET
    return out


def _merge(node: str, ports: dict[str, dict], fields: list[dict], err) -> None:
    """Every branch into `node` must hand on the same schema."""
    names = list(ports)
    base_name, base = names[0], ports[names[0]]
    for other in names[1:]:
        diff = [f["name"] for f in fields if base.get(f["name"]) != ports[other].get(f["name"])]
        if not diff:
            continue
        shown = "; ".join(f"`{d}` is {STATE_TEXT[base.get(d)]} from {base_name} but {STATE_TEXT[ports[other].get(d)]} from {other}"
                          for d in diff[:3])
        more = f" (and {len(diff) - 3} more)" if len(diff) > 3 else ""
        where = "Output" if node == "output" else node
        err(node, f"{where} merges {base_name} and {other}, but their schemas differ: {shown}{more}. "
                  "Give both branches the same steps for these fields, merge before the step that changes them, "
                  "or join them in a SQL step that reads each input as its own table.")


def joins(step: dict) -> bool:
    """A SQL step with several inputs whose query reads them as tables (by step id)."""
    q = (step.get("config") or {}).get("query") or ""
    return (step["type"] == "sql" and len(step.get("inputs") or []) > 1
            and any(re.search(rf"\b{re.escape(i)}\b", q) for i in step["inputs"]))


def check(kind: str, d: dict) -> dict:
    """
    Check a definition (the draft or the applied one). Returns
    {"ok", "errors": [{"node", "text"}], "warnings": [...], "order", "ports": {id: {field: state}}, "output": {...}}.
    """
    errors, warnings = [], []
    err = lambda node, text: errors.append({"node": node, "text": text})
    warn = lambda node, text: warnings.append({"node": node, "text": text})
    fields = (d.get("schema") or {}).get("fields") or []
    sources = d.get("sources") or []
    g = graph.normalise(d)
    steps = g["steps"]
    by_id = {s["id"]: s for s in steps}
    result = {"errors": errors, "warnings": warnings, "order": [], "ports": {}, "output": {}}
    if not steps:
        err(None, "There are no steps.")
        return {**result, "ok": False}

    # settings
    for s in steps:
        try:
            cfg = engine.validate_step(kind, s["type"], s.get("config") or {}, fields, s["inputs"], sources)
            if s["type"] == "extract":
                unknown = [i for i in cfg.get("source_ids") or [] if i not in {x["id"] for x in sources}]
                if unknown:
                    err(s["id"], f"{s['id']} reads {', '.join(unknown)}, which {'is' if len(unknown) == 1 else 'are'} not a source.")
        except PipelineError as e:
            err(s["id"], f"{s['id']}: {e}")

    # wiring
    try:
        order = graph.order(g)
    except graph.GraphError as e:
        err(None, str(e))
        return {**result, "ok": False}
    result["order"] = order
    used = graph.consumers(g)
    for s in steps:
        if not s["inputs"] and s["type"] != "extract":
            err(s["id"], f"{s['id']} has no input: wire a step into it (only an Extract step can start a branch).")
        dest = used[s["id"]]
        if not dest:
            err(s["id"], f"{s['id']} is not connected to anything: wire its output into a step or Output, or remove it.")
        elif len(dest) > 1:
            err(s["id"], f"{s['id']}'s output goes to {' and '.join('Output' if x == 'output' else x for x in dest)}. "
                         "A step's output can go to one place; merge branches instead of splitting one.")
    if not g["output_from"]:
        err("output", "Nothing is wired into Output.")
    bad_out = [i for i in g["output_from"] if i not in by_id]
    if bad_out:
        err("output", f"Output takes {', '.join(bad_out)}, which {'is' if len(bad_out) == 1 else 'are'} not a step.")

    # sources: records are keyed by source, so one Extract step per source
    readers: dict[str, list[str]] = {}
    for s in steps:
        if s["type"] == "extract":
            for sid in (s.get("config") or {}).get("source_ids") or [x["id"] for x in sources]:
                readers.setdefault(sid, []).append(s["id"])
    for sid, rs in readers.items():
        if len(rs) > 1:
            err(rs[-1], f"{sid} is read by {' and '.join(rs)}. Each source can be read by one Extract step "
                        "(their records would share ids); give each Extract step its own sources.")
    # A CSV source a SQL step joins as a table needs no Extract step.
    joined: dict[str, list[str]] = {}
    for s in steps:
        if s["type"] == "sql":
            for t in (s.get("config") or {}).get("tables") or []:
                if t not in engine.REF_TABLES:
                    joined.setdefault(t, []).append(s["id"])
    for sid, js in joined.items():
        if sid in readers:
            everything = [r for r in readers[sid] if not (by_id[r].get("config") or {}).get("source_ids")]
            warn(readers[sid][0], f"{sid} is joined as a table by {', '.join(js)} and also extracted by {', '.join(readers[sid])}"
                 + (f" ({', '.join(everything)} read{'s' if len(everything) == 1 else ''} every source: set its sources if it should not read {sid})"
                    if everything else "") + ".")
    unread = [x["id"] for x in sources if x["id"] not in readers and x["id"] not in joined]
    if unread and any(s["type"] == "extract" for s in steps):
        warn(None, f"No Extract step reads {', '.join(unread)}, and no SQL step joins it.")

    # port schemas, in run order, and merges
    ports: dict[str, dict] = {}
    for sid in order:
        s = by_id[sid]
        ins = {i: ports[i] for i in s["inputs"] if i in ports}
        if s["type"] == "extract":
            if ins:
                ahead = sorted(graph.ancestors(g, sid) - {x for x in graph.ancestors(g, sid) if by_id[x]["type"] == "extract"})
                if ahead:
                    warn(sid, f"The records {sid} reads join after {', '.join(ahead)}, so those steps do not process them.")
            ins = {**ins, f"{sid}'s sources": _fresh(fields)}
        if len(ins) > 1 and not joins(s):
            _merge(sid, ins, fields, err)
        port = dict(next(iter(ins.values()))) if ins else {f["name"]: None for f in fields}
        ports[sid] = _after(s, port, fields, warn, sources)
    result["ports"] = ports

    outs = {i: ports[i] for i in g["output_from"] if i in ports}
    if len(outs) > 1:
        _merge("output", outs, fields, err)
    final = dict(next(iter(outs.values()))) if outs else {}
    result["output"] = final
    if outs:
        for f in fields:
            st = final.get(f["name"])
            if st is None and not f.get("accept_blank") and (f["required"] or not f.get("extract", True)):
                warn("output", f"No step on the way to Output produces `{f['name']}`"
                               + (" (a required field): publishing waits for it." if f["required"] else "."))
            elif st == TEXT:
                warn("output", f"`{f['name']}` reaches Output as unparsed text: add a Parse step for it.")
    return {**result, "ok": not errors}
