"""
A pipeline's outputs: one or more Output nodes, each its own table.

Most pipelines have one Output. A pipeline whose data has several shapes (a
zoning pipeline produces block zones, use permissions and recommendation
readings) has one Output per shape. Each output has its own schema, the steps
that feed it, its load mapping, its overrides, its record review and its own
last run. Publishing writes every output into one dataset version.

Storage. The first output is the one a definition has always had: its schema,
output_from, load_map, overrides and record_review sit at the top of the
definition, its last run is state["last_run"], and its id is "output". Further
outputs are definition["outputs"], a list of

    {"id": "o2", "target", "name", "schema", "output_from",
     "load_map"?, "overrides"?, "record_review"?}

with their last runs in state["output_runs"][id]. "target" says what the
output loads into (kinds.TARGETS); the first output's is definition["target"],
else the kind's first target.

A definition without "outputs" is exactly what it was, and every function here
hands it through unchanged, so a one-output pipeline runs, hashes and checks as
before.

Ownership. A step's output goes to one place, so each step feeds exactly one
output; it works in that output's schema. A step that reaches no output yet
belongs to the first output, whose check reports it as unconnected.
"""

from __future__ import annotations

from typing import Callable

from backend.pipelines import check as checker
from backend.pipelines import engine, graph
from backend.pipelines.engine import PipelineError

MAIN = "output"
# What each output keeps for itself; everything else in a definition is shared.
OWN = ("target", "name", "schema", "output_from", "load_map", "overrides", "record_review", "write")
# Never part of an output's view: the draft and proposal are edited whole.
SHARED_ONLY = ("outputs", "draft", "proposal", "layout")


def extras(definition: dict) -> list[dict]:
    return definition.get("outputs") or []


def multi(definition: dict) -> bool:
    return bool(extras(definition))


def ids(definition: dict) -> list[str]:
    return [MAIN] + [o["id"] for o in extras(definition)]


def entry(definition: dict, oid: str) -> dict:
    """Where an output's own keys live: the definition itself for the first, its entry for the others."""
    if oid == MAIN:
        return definition
    o = next((x for x in extras(definition) if x["id"] == oid), None)
    if o is None:
        raise PipelineError(f"No output {oid}.")
    return o


def output_from(definition: dict) -> dict[str, list[str]]:
    """Each output's inputs: {output id: [step ids]}."""
    out = {MAIN: graph.normalise(definition)["output_from"]}
    for o in extras(definition):
        out[o["id"]] = list(o.get("output_from") or [])
    return out


def owners(definition: dict) -> dict[str, str]:
    """Which output each step feeds: {step id: output id}. Steps that reach none belong to the first."""
    g = graph.normalise(definition)
    owner: dict[str, str] = {}
    for oid, froms in output_from(definition).items():
        for sid in froms:
            for x in graph.ancestors(g, sid) | {sid}:
                owner.setdefault(x, oid)
    for s in g["steps"]:
        owner.setdefault(s["id"], MAIN)
    return owner


def owner(definition: dict, step_id: str) -> str:
    return owners(definition).get(step_id, MAIN)


def project(definition: dict, oid: str = MAIN) -> dict:
    """
    The definition as one output sees it: its own schema, mapping, overrides
    and review, the shared goal and sources, and only the steps that feed it.
    The steps are the definition's own step objects, so a change to a step's
    status made through the view is a change to the step.
    """
    if not multi(definition):
        if oid != MAIN:
            raise PipelineError(f"No output {oid}.")
        return definition
    src = entry(definition, oid)
    owner_of = owners(definition)
    view = {k: v for k, v in definition.items() if k not in OWN and k not in SHARED_ONLY}
    view.update({k: src[k] for k in OWN if k in src})
    view.setdefault("schema", {"status": None, "fields": []})
    view["steps"] = [s for s in definition.get("steps") or [] if owner_of.get(s["id"]) == oid]
    # A step that feeds this output but belongs to another (a split) is reported once, by check(), not here.
    view["output_from"] = [i for i in src.get("output_from") or [] if owner_of.get(i, oid) == oid]
    return view


def sync(definition: dict, oid: str, view: dict) -> None:
    """Write an output's own keys back from its view (the inverse of project)."""
    if view is definition:
        return
    dest = entry(definition, oid)
    for k in OWN:
        if k == "output_from":
            continue   # wiring changes only through the draft
        if k in view:
            dest[k] = view[k]
        elif k in dest and k not in ("target", "name"):
            dest.pop(k)


def fields(definition: dict, oid: str = MAIN) -> list[dict]:
    return (entry(definition, oid).get("schema") or {}).get("fields") or []


def step_fields(definition: dict, step_id: str | None, fallback: str = MAIN) -> list[dict]:
    """The schema a step works in: its output's. A step not in the definition (being added) uses `fallback`'s."""
    if not multi(definition):
        return fields(definition)
    known = step_id and any(s["id"] == step_id for s in definition.get("steps") or [])
    return fields(definition, owner(definition, step_id) if known else fallback)


def label(definition: dict, oid: str) -> str:
    e = entry(definition, oid)
    return e.get("name") or ("Output" if oid == MAIN else oid)


# ── Run state per output ─────────────────────────────────────────────────────

def last_run(state: dict, oid: str = MAIN) -> dict:
    if oid == MAIN:
        return state.get("last_run") or {}
    return (state.get("output_runs") or {}).get(oid) or {}


def set_last_run(state: dict, oid: str, run: dict | None) -> None:
    if oid == MAIN:
        if run is None:
            state.pop("last_run", None)
        else:
            state["last_run"] = run
        return
    runs = state.setdefault("output_runs", {})
    if run is None:
        runs.pop(oid, None)
    else:
        runs[oid] = run


SHARED_STATE = ("cache", "dead_links", "sources")


def substate(state: dict, definition: dict, oid: str) -> dict:
    """The state one output's run reads: its steps' outputs and last run, and the shared caches."""
    if not multi(definition):
        return state
    owner_of = owners(definition)
    sub = {k: state[k] for k in SHARED_STATE if k in state}
    sub["outputs"] = {sid: o for sid, o in (state.get("outputs") or {}).items() if owner_of.get(sid) == oid}
    run = last_run(state, oid)
    if run:
        sub["last_run"] = run
    return sub


def merge_state(state: dict, definition: dict, oid: str, sub: dict) -> None:
    """Put one output's run back: its steps' outputs and last run; the caches it refreshed."""
    if sub is state:
        return
    owner_of = owners(definition)
    state["outputs"] = {**{sid: o for sid, o in (state.get("outputs") or {}).items() if owner_of.get(sid) != oid},
                        **(sub.get("outputs") or {})}
    for k in SHARED_STATE:
        if k in sub:
            state[k] = sub[k]
    set_last_run(state, oid, sub.get("last_run"))


def run(kind: str, definition: dict, state: dict, upto: str | None = None, fresh: bool = False,
        context: str = "", progress: Callable[[str], None] | None = None,
        refs: Callable[[str], list[dict]] | None = None, only: str | None = None) -> dict:
    """
    engine.run for every output (or the one that `upto` feeds, or `only`),
    one after another. Mutates and returns `state`.
    """
    if not multi(definition):
        return engine.run(kind, definition, state, upto=upto, fresh=fresh, context=context, progress=progress, refs=refs)
    owner_of = owners(definition)
    which = [owner_of.get(upto, MAIN)] if upto else [only] if only else ids(definition)
    for oid in which:
        view = project(definition, oid)
        name = label(definition, oid)
        sub = substate(state, definition, oid)
        say = (lambda m, n=name: progress(f"{n}: {m}")) if progress else None
        engine.run(kind, view, sub, upto=upto, fresh=fresh, context=context, progress=say, refs=refs)
        merge_state(state, definition, oid, sub)
        fresh = False   # sources were refetched for the first output; the others read the fresh copies
    return state


def rerun_record(kind: str, definition: dict, state: dict, step_id: str, record_id: str, context: str = "",
                 progress: Callable[[str], None] | None = None,
                 refs: Callable[[str], list[dict]] | None = None) -> dict:
    if not multi(definition):
        return engine.rerun_record(kind, definition, state, step_id, record_id, context, progress, refs)
    oid = owner(definition, step_id)
    sub = substate(state, definition, oid)
    engine.rerun_record(kind, project(definition, oid), sub, step_id, record_id, context, progress, refs)
    merge_state(state, definition, oid, sub)
    return state


# ── Fingerprints and checks across outputs ───────────────────────────────────

def signatures(definition: dict, context: str = "") -> dict[str, str]:
    """engine.step_signatures, each step in its own output's schema."""
    if not multi(definition):
        return engine.step_signatures(definition, context)
    out: dict[str, str] = {}
    for oid in ids(definition):
        out.update(engine.step_signatures(project(definition, oid), context))
    return out


def definition_hash(definition: dict) -> str:
    """What the whole pipeline's results depend on (every output's)."""
    if not multi(definition):
        return engine.definition_hash(definition)
    return engine._hash({oid: engine.definition_hash(project(definition, oid)) for oid in ids(definition)})


def design_gaps(definition: dict) -> dict[str, list[str]]:
    return {oid: engine.design_gaps(project(definition, oid)) for oid in ids(definition)}


def _prefixed(definition: dict, oid: str, text: str) -> str:
    return text if not multi(definition) else f"{label(definition, oid)}: {text}"


def check(kind: str, definition: dict) -> dict:
    """
    check.check for each output, merged: errors and warnings name their output,
    ports cover every step, and "outputs" holds each output's final port. Plus
    the one rule that spans outputs: a step feeds one output.
    """
    if not multi(definition):
        result = checker.check(kind, definition)
        return {**result, "outputs": {MAIN: result.get("output") or {}}}
    errors, warnings, ports, finals = [], [], {}, {}
    for oid in ids(definition):
        view = project(definition, oid)
        if not view["steps"]:
            errors.append({"node": oid, "text": f"Nothing is wired into {label(definition, oid)} yet."})
            continue
        r = checker.check(kind, view)
        for bucket, items in ((errors, r["errors"]), (warnings, r["warnings"])):
            for e in items:
                node = oid if e["node"] == "output" else e["node"]
                bucket.append({"node": node, "text": _prefixed(definition, oid, e["text"]).replace(
                    "into Output", f"into {label(definition, oid)}")})
        ports.update(r["ports"])
        finals[oid] = r.get("output") or {}

    # A step's output goes to one place, across outputs too.
    g = graph.normalise(definition)
    dest: dict[str, list[str]] = {s["id"]: [] for s in g["steps"]}
    for s in g["steps"]:
        for i in s["inputs"]:
            if i in dest:
                dest[i].append(s["id"])
    for oid, froms in output_from(definition).items():
        for i in froms:
            if i in dest:
                dest[i].append(label(definition, oid))
    for sid, ds in dest.items():
        if len(ds) > 1 and not any(e["node"] == sid and "goes to" in e["text"] for e in errors):
            errors.append({"node": sid, "text": f"{sid}'s output goes to {' and '.join(ds)}. A step's output can go to "
                                                "one place: give each output its own branch."})
    try:
        order = graph.order(g)
    except graph.GraphError as e:
        errors.append({"node": None, "text": str(e)})
        order = []
    return {"ok": not errors, "errors": errors, "warnings": warnings, "order": order, "ports": ports,
            "output": finals.get(MAIN, {}), "outputs": finals}
