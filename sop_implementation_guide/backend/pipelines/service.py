"""
Operations on a pipeline's definition, shared by the chat agent's tools and the
panel's buttons, so both go through the same validation.

Who may do what is the guardrail's other half. The agent may propose: the
schema, sources, steps and previews. Only the user may approve the schema or a
step, accept blanks for a required field, and load. Every user action is also
written into the chat as an event, so the conversation shows what happened.
"""

from __future__ import annotations

import logging
from typing import Any

from supabase import Client

from backend.pipelines import check, engine, graph, kinds
from backend.pipelines.engine import PipelineError
from backend.repositories.pipelines import (
    PipelineMessageRepository,
    PipelineRepository,
    PipelineRunRepository,
)

log = logging.getLogger(__name__)


def get(db: Client, pipeline_id: int) -> dict:
    p = PipelineRepository(db).get(pipeline_id)
    if not p:
        raise LookupError(f"Pipeline {pipeline_id} not found")
    p["definition"] = {"schema": {"status": None, "fields": []}, "sources": [], "steps": [],
                       **(p.get("definition") or {})}
    p["state"] = p.get("state") or {}
    return p


def _save(db: Client, p: dict, **extra) -> None:
    PipelineRepository(db).update(p["id"], {"definition": p["definition"], **extra})


def event(db: Client, pipeline_id: int, text: str, level: str = "info") -> None:
    PipelineMessageRepository(db).add(pipeline_id, "event", {"text": text, "level": level})


def _next_id(items: list[dict], prefix: str) -> str:
    n = max([int(i["id"][len(prefix):]) for i in items if str(i.get("id", "")).startswith(prefix)
             and i["id"][len(prefix):].isdigit()] or [0])
    return f"{prefix}{n + 1}"


def _fields(p: dict) -> list[dict]:
    return p["definition"]["schema"].get("fields") or []


# ── Create ───────────────────────────────────────────────────────────────────

def create(db: Client, kind: str, name: str, created_by: str | None) -> dict:
    s = kinds.spec(kind)
    if not s["available"]:
        raise PipelineError(f"{s['label']} pipelines are not available yet.")
    p = PipelineRepository(db).create({
        "kind": kind, "name": (name or "").strip() or f"New {s['label'].lower()} pipeline",
        "definition": {"schema": {"status": None, "fields": []}, "sources": [], "steps": []},
        "state": {}, "created_by": (created_by or "").strip() or None,
    })
    # The opening message is shown, not sent: the model gets the same guidance in its system prompt.
    event(db, p["id"], s["intro"], level="intro")
    return p


# ── Schema ───────────────────────────────────────────────────────────────────

def set_schema(db: Client, pipeline_id: int, fields: list[dict], by_user: bool = False) -> dict:
    p = get(db, pipeline_id)
    prev = {f["name"]: f for f in _fields(p)}
    clean = engine.validate_fields(fields)
    for f in clean:
        # A user's "accept blanks" survives a re-proposal only for an unchanged field.
        old = prev.get(f["name"])
        if old and all(old.get(k) == f.get(k) for k in ("type", "required", "enum_values")) and old.get("accept_blank"):
            f["accept_blank"] = True
    keys = ("name", "type", "required", "enum_values", "extract", "description")
    changed = [{k: f.get(k) for k in keys} for f in clean] != [{k: f.get(k) for k in keys} for f in prev.values()]
    p["definition"]["schema"] = {"status": "proposed", "fields": clean}
    reset = _invalidate_from(p, 0) if changed else []
    _save(db, p, state=p["state"])
    if reset and not by_user:
        event(db, pipeline_id, "The target schema changed." + _reset_text(reset))
    if by_user:
        event(db, pipeline_id, f"You set the target schema ({len(clean)} fields)." + _reset_text(reset))
    return p["definition"]["schema"]


def use_default_schema(db: Client, pipeline_id: int) -> dict:
    p = get(db, pipeline_id)
    schema = set_schema(db, pipeline_id, kinds.spec(p["kind"])["default_fields"])
    event(db, pipeline_id, "You loaded the default fields. Review them, then approve.")
    return schema


def approve_schema(db: Client, pipeline_id: int) -> dict:
    p = get(db, pipeline_id)
    if not _fields(p):
        raise PipelineError("There is no schema to approve.")
    for req in kinds.spec(p["kind"])["required_fields"]:
        if req not in {f["name"] for f in _fields(p)}:
            raise PipelineError(f"The schema must include `{req}` before it can be approved.")
    p["definition"]["schema"]["status"] = "approved"
    _save(db, p)
    event(db, pipeline_id, "You approved the target schema.")
    return p["definition"]["schema"]


def set_goal(db: Client, pipeline_id: int, goal: str) -> str:
    """The planner's goal in their words. Every model step reads it, so extraction knows what counts."""
    goal = (goal or "").strip()
    if not goal:
        raise PipelineError("The goal is empty.")
    p = get(db, pipeline_id)
    p["definition"]["goal"] = goal[:2000]
    _save(db, p)
    return p["definition"]["goal"]


def accept_blank(db: Client, pipeline_id: int, field: str, accept: bool) -> dict:
    p = get(db, pipeline_id)
    f = next((x for x in _fields(p) if x["name"] == field), None)
    if not f:
        raise PipelineError(f"No field named {field!r}.")
    f["accept_blank"] = bool(accept)
    _save(db, p)
    event(db, pipeline_id, f"You {'accepted' if accept else 'stopped accepting'} blanks for `{field}`.")
    return f


# ── Sources ──────────────────────────────────────────────────────────────────

def add_source(db: Client, pipeline_id: int, kind: str, url: str | None = None, query: str | None = None,
               label: str | None = None, by_user: bool = False) -> dict:
    """Add a source and fetch it at once, so a bad URL is reported now rather than at run time."""
    p = get(db, pipeline_id)
    if kind == "url":
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise PipelineError("A source URL must start with http:// or https://.")
        if any(s.get("url") == url for s in p["definition"]["sources"]):
            raise PipelineError("That source is already in the pipeline.")
    elif kind == "grants_gov":
        query = (query or "").strip()
        if not query:
            raise PipelineError("A Grants.gov source needs a keyword query.")
    else:
        raise PipelineError("Source kind must be url or grants_gov.")
    reopened = _reopen_sources(p)
    src = {"id": _next_id(p["definition"]["sources"], "s"), "kind": kind,
           **({"url": url} if kind == "url" else {"query": query}),
           "label": (label or "").strip() or (url if kind == "url" else f"Grants.gov: {query}")}
    try:
        meta = engine.source_meta(engine.fetch_source(src, fresh=True))   # downloaded now, then read from its saved copy
    except PipelineError as e:
        meta = {"error": str(e)}
    p["definition"]["sources"].append(src)
    p["state"].setdefault("sources", {})[src["id"]] = meta
    _save(db, p, state=p["state"])
    if by_user:
        event(db, pipeline_id, f"You added source {src['id']}: {src['label']}"
              + (f" (could not be read: {meta['error']})" if meta.get("error") else f" ({meta['chars']:,} characters).")
              + reopened, level="warn" if meta.get("error") or reopened else "info")
    elif reopened:
        event(db, pipeline_id, f"Source {src['id']} was added." + reopened, level="warn")
    return {**src, "fetch": meta}


def remove_source(db: Client, pipeline_id: int, source_id: str, by_user: bool = False) -> None:
    p = get(db, pipeline_id)
    before = len(p["definition"]["sources"])
    p["definition"]["sources"] = [s for s in p["definition"]["sources"] if s["id"] != source_id]
    if len(p["definition"]["sources"]) == before:
        raise PipelineError(f"No source {source_id}.")
    p["state"].get("sources", {}).pop(source_id, None)
    # Extract steps wired to it stop naming it; one wired to nothing else falls back to all sources.
    rewired = []
    for s in p["definition"]["steps"] + ((p["definition"].get("draft") or {}).get("steps") or []):
        ids = (s.get("config") or {}).get("source_ids")
        if s["type"] == "extract" and ids and source_id in ids:
            s["config"]["source_ids"] = [i for i in ids if i != source_id] or None
            if s["config"]["source_ids"] is None and s["id"] not in rewired:
                rewired.append(s["id"])
    reopened = _reopen_sources(p)
    _save(db, p, state=p["state"])
    note = (f" {', '.join(rewired)} read only {source_id}, so {'it now reads' if len(rewired) == 1 else 'they now read'}"
            " every source: rewire on the canvas if that is wrong.") if rewired else ""
    if by_user or reopened or rewired:
        event(db, pipeline_id, f"{'You removed' if by_user else 'Removed'} source {source_id}." + reopened + note,
              level="warn" if reopened or rewired else "info")


def source_text(db: Client, pipeline_id: int, source_id: str) -> dict:
    """The full fetched text of one source, for the planner to read before marking sources final."""
    p = get(db, pipeline_id)
    src = next((s for s in p["definition"]["sources"] if s["id"] == source_id), None)
    if not src:
        raise LookupError(f"No source {source_id}.")
    fetched = engine.fetch_source(src)   # its saved copy; never downloaded again here
    return {**src, **engine.source_meta(fetched), "preview": None, "text": fetched.get("text") or "",
            "links": engine.source_links(fetched)}


def finalize_sources(db: Client, pipeline_id: int) -> dict:
    """
    The planner's word that the sources are complete. Steps are outlined only
    after this, because what the steps should do depends on what the sources are.
    """
    p = get(db, pipeline_id)
    if p["definition"]["schema"].get("status") != "approved":
        raise PipelineError("Approve the target schema first: the fields decide what to look for in the sources.")
    sources = p["definition"]["sources"]
    if not sources:
        raise PipelineError("Add at least one source first.")
    meta = p["state"].get("sources") or {}
    bad = [s["id"] for s in sources if (meta.get(s["id"]) or {}).get("error")]
    if bad:
        raise PipelineError(f"{', '.join(bad)} could not be read: remove or replace {'it' if len(bad) == 1 else 'them'} first.")
    p["definition"]["sources_status"] = "final"
    _save(db, p)
    event(db, pipeline_id, f"You marked the {len(sources)} source(s) as final.")
    return p["definition"]


def _reopen_sources(p: dict) -> str:
    """Sources changed after they were final: steps already approved worked on the old sources."""
    if p["definition"].get("sources_status") != "final":
        return ""
    p["definition"]["sources_status"] = "open"
    reset = _invalidate_from(p, 0)
    return (" The sources are no longer final: mark them final again when they are complete."
            + _reset_text(reset))


def read_source(db: Client, pipeline_id: int, source_id: str, offset: int = 0, length: int = 3000) -> dict:
    p = get(db, pipeline_id)
    src = next((s for s in p["definition"]["sources"] if s["id"] == source_id), None)
    if not src:
        raise PipelineError(f"No source {source_id}.")
    text = engine.fetch_source(src).get("text") or ""
    offset, length = max(0, int(offset)), max(200, min(int(length), 4000))
    return {"source_id": source_id, "total_chars": len(text), "offset": offset,
            "text": text[offset:offset + length], "more": offset + length < len(text)}


# ── Steps ────────────────────────────────────────────────────────────────────
#
# The applied steps are reviewed one at a time: tune a model step's
# instructions, preview, approve. Everything else about them (adding,
# removing, rewiring, settings) changes in a draft, below, which is checked
# and then applied in one go.

def _check_sources(p: dict, step_type: str, cfg: dict) -> dict:
    """An Extract step's wiring must name sources that exist. An empty list means all sources."""
    if step_type == "extract" and cfg.get("source_ids") is not None:
        known = [s["id"] for s in p["definition"]["sources"]]
        unknown = [i for i in cfg["source_ids"] if i not in known]
        if unknown:
            raise PipelineError(f"No source {', '.join(map(str, unknown))}; the sources are {', '.join(known) or 'none'}.")
        cfg["source_ids"] = [i for i in known if i in cfg["source_ids"]] or None
    return cfg


def _invalidate_from(p: dict, index: int) -> list[str]:
    """
    A step changed: it and every later step must be previewed and approved
    again, because their output depends on it. Returns the steps that lose
    their approval.
    """
    reset = []
    for s in p["definition"]["steps"][index:]:
        if s.get("status") == "approved":
            reset.append(s["id"])
        s["status"] = "proposed"
        (p["state"].get("outputs") or {}).pop(s["id"], None)
    return reset


def _reset_text(reset: list[str]) -> str:
    return f" Steps {', '.join(reset)} need approving again." if reset else ""


def _downstream(p: dict, step_id: str) -> list[str]:
    """The step and every step its records reach, in run order."""
    g = graph.normalise(p["definition"])
    return [sid for sid in graph.order(g) if sid == step_id or step_id in graph.ancestors(g, sid)]


def update_step(db: Client, pipeline_id: int, step_id: str, config: dict | None = None,
                note: str | None = None, by_user: bool = False) -> dict:
    """
    Tune an applied step while reviewing it: a model step's instructions, and
    its note. Other settings change in a draft. The step and every step after
    it go back for approval.
    """
    p = get(db, pipeline_id)
    steps = p["definition"]["steps"]
    step = next((s for s in steps if s["id"] == step_id), None)
    if not step:
        raise PipelineError(f"No step {step_id}.")
    reset, changed = [], False
    if config is not None:
        clean = _check_sources(p, step["type"], engine.validate_step(p["kind"], step["type"], config, _fields(p)))
        slot = engine.EDITABLE.get(step["type"])
        other = {k for k in set(clean) | set(step["config"]) if k != slot and clean.get(k) != step["config"].get(k)}
        if other:
            raise PipelineError(f"Only {step_id}'s instructions can change while reviewing it; change {', '.join(f'`{k}`' for k in sorted(other))} "
                                "in a draft (Edit pipeline), where the change is checked before it applies.")
        if clean != step["config"]:
            # An open draft holds a copy of this step: keep it in step, or Apply would undo the tuning.
            copy = next((s for s in (p["definition"].get("draft") or {}).get("steps") or [] if s["id"] == step_id), None)
            if copy and copy.get("type") == step["type"]:
                copy["config"] = {**copy["config"], **({slot: clean.get(slot)} if slot else {})}
            step["config"], changed = clean, True
            for sid in _downstream(p, step_id):
                s = next(x for x in steps if x["id"] == sid)
                if s.get("status") == "approved":
                    reset.append(sid)
                s["status"] = "proposed"
                (p["state"].get("outputs") or {}).pop(sid, None)
    if note is not None:
        step["note"] = note.strip()
    _save(db, p, state=p["state"])
    if changed:
        event(db, pipeline_id, f"{'You' if by_user else 'The agent'} changed step {step_id}'s prompt instructions. "
              "Preview it again to check the output." + _reset_text(reset))
    return step


def approve_step(db: Client, pipeline_id: int, step_id: str) -> dict:
    """
    Approving a step means its prompt (or settings) and its output were checked.
    So it needs a preview with the current settings, and the steps feeding it
    approved first: its output depends on theirs.
    """
    p = get(db, pipeline_id)
    steps = p["definition"]["steps"]
    step = next((s for s in steps if s["id"] == step_id), None)
    if not step:
        raise PipelineError(f"No step {step_id}.")
    before = graph.ancestors(graph.normalise(p["definition"]), step_id)
    earlier = [s["id"] for s in steps if s["id"] in before and s.get("status") != "approved"]
    if earlier:
        raise PipelineError(f"Approve {', '.join(earlier)} first: this step works on their output.")
    out = (p["state"].get("outputs") or {}).get(step_id)
    if not out:
        raise PipelineError(f"Preview {step_id} first, so you approve it having seen what it produces.")
    if out.get("error"):
        raise PipelineError(f"{step_id} failed in its last preview: {out['error']}")
    step["status"] = "approved"
    _save(db, p)
    gaps = engine.design_gaps(p["definition"])
    done = all(s.get("status") == "approved" for s in steps)
    event(db, pipeline_id, f"You approved step {step_id} ({engine.CATALOG[step['type']]['label']})."
          + ("" if not done else
             f" The pipeline still has no step for {', '.join(f'`{g}`' for g in gaps)}: add one in a draft."
             if gaps else " Every step is approved: run the pipeline to see the final output."),
          level="warn" if done and gaps else "info")
    return step


# ── Draft: change the steps, check, apply ────────────────────────────────────
#
# Structural edits (by the planner on the canvas or by the agent) go to
# definition["draft"]: a copy of the steps with their wiring. Nothing runs or
# resets while it is edited. Check reports errors and warnings; Apply refuses
# while there are errors, then replaces the steps and keeps the approval (and
# preview) of every step whose inputs and settings did not change.

def _require_design(p: dict) -> None:
    if p["definition"]["schema"].get("status") != "approved":
        raise PipelineError("Steps are designed after the target schema is approved.")
    if p["definition"].get("sources_status") != "final":
        raise PipelineError("Steps are designed after the sources are marked final (the 'Sources are final' button).")


def _draft(p: dict, open_: bool = False) -> dict:
    d = p["definition"].get("draft")
    if d is None:
        if not open_:
            raise PipelineError("There is no draft open. Press Edit pipeline to change the steps.")
        _require_design(p)
        g = graph.normalise(p["definition"])
        d = {"steps": [{k: s.get(k) for k in ("id", "type", "config", "note", "inputs")} for s in g["steps"]],
             "output_from": g["output_from"]}
        p["definition"]["draft"] = d
    return d


def _draft_definition(p: dict) -> dict:
    d = p["definition"]["draft"]
    return {**{k: v for k, v in p["definition"].items() if k != "draft"},
            "steps": d["steps"], "output_from": d["output_from"]}


def _draft_step(d: dict, step_id: str) -> dict:
    s = next((x for x in d["steps"] if x["id"] == step_id), None)
    if not s:
        raise PipelineError(f"No step {step_id} in the draft.")
    return s


def _who(by_user: bool) -> str:
    return "You" if by_user else "The agent"


def open_draft(db: Client, pipeline_id: int, by_user: bool = True) -> dict:
    p = get(db, pipeline_id)
    if p["definition"].get("draft") is not None:
        return p["definition"]["draft"]
    d = _draft(p, open_=True)
    _save(db, p)
    event(db, pipeline_id, f"{_who(by_user)} opened a draft of the steps. Changes apply only when you press Apply.")
    return d


def discard_draft(db: Client, pipeline_id: int) -> None:
    p = get(db, pipeline_id)
    if p["definition"].pop("draft", None) is None:
        raise PipelineError("There is no draft open.")
    _save(db, p)
    event(db, pipeline_id, "You discarded the draft: the steps are as they were.")


def check_draft(p: dict) -> dict:
    return check.check(p["kind"], _draft_definition(p))


def _splice_after(d: dict, after: str, new_id: str) -> None:
    """Put new_id between `after` and whatever `after` fed."""
    for s in d["steps"]:
        if s["id"] != new_id:
            s["inputs"] = [new_id if i == after else i for i in s["inputs"]]
    d["output_from"] = [new_id if i == after else i for i in d["output_from"]]


def draft_add_step(db: Client, pipeline_id: int, step_type: str, config: dict, note: str | None = None,
                   after: str | None = None, before: str | None = None, inputs: list[str] | None = None,
                   connect: bool = True, by_user: bool = False) -> dict:
    """
    Add a step to the draft. Placed after `after` (taking its place in the
    wiring), before `before`, with explicit `inputs`, or (connect) at the end:
    it then takes what fed Output and feeds Output itself. connect=False leaves
    it unwired, for the planner to wire on the canvas.
    """
    p = get(db, pipeline_id)
    d = _draft(p, open_=True)
    cfg = _check_sources(p, step_type, engine.validate_step(p["kind"], step_type, config or {}, _fields(p)))
    taken = d["steps"] + p["definition"]["steps"]
    step = {"id": _next_id(taken, "t"), "type": step_type, "config": cfg, "note": (note or "").strip(), "inputs": []}
    ids = {s["id"] for s in d["steps"]}
    pos = len(d["steps"])
    if after:
        if after not in ids:
            raise PipelineError(f"No step {after} in the draft.")
        _splice_after(d, after, step["id"])
        step["inputs"] = [after]
        pos = next(i for i, s in enumerate(d["steps"]) if s["id"] == after) + 1
        where = f" after {after}"
    elif before:
        target = _draft_step(d, before)
        step["inputs"], target["inputs"] = list(target["inputs"]), [step["id"]]
        pos = next(i for i, s in enumerate(d["steps"]) if s["id"] == before)
        where = f" before {before}"
    elif inputs is not None:
        bad = [i for i in inputs if i not in ids]
        if bad:
            raise PipelineError(f"No step {', '.join(bad)} in the draft.")
        step["inputs"] = list(dict.fromkeys(inputs))
        where = f" taking {', '.join(step['inputs'])}" if step["inputs"] else " (not wired yet)"
    elif connect:
        step["inputs"], d["output_from"] = list(d["output_from"]), [step["id"]]
        where = " at the end" if step["inputs"] else ""
    else:
        where = " (not wired yet)"
    d["steps"].insert(pos, step)
    _save(db, p)
    if by_user:
        event(db, pipeline_id, f"You added {step['id']} ({engine.CATALOG[step_type]['label']}) to the draft{where}.")
    return step


def draft_update_step(db: Client, pipeline_id: int, step_id: str, config: dict | None = None,
                      note: str | None = None, by_user: bool = False) -> dict:
    p = get(db, pipeline_id)
    d = _draft(p, open_=True)
    s = _draft_step(d, step_id)
    if config is not None:
        s["config"] = _check_sources(p, s["type"], engine.validate_step(p["kind"], s["type"], config, _fields(p)))
    if note is not None:
        s["note"] = note.strip()
    _save(db, p)
    return s


def draft_remove_step(db: Client, pipeline_id: int, step_id: str, by_user: bool = False) -> None:
    """Remove a step from the draft. In a plain run of steps, its input is wired straight to what it fed."""
    p = get(db, pipeline_id)
    d = _draft(p, open_=True)
    s = _draft_step(d, step_id)
    d["steps"].remove(s)
    fed = [x for x in d["steps"] if step_id in x["inputs"]] + (["output"] if step_id in d["output_from"] else [])
    heal = s["inputs"] if len(s["inputs"]) == 1 and len(fed) == 1 else []
    for x in d["steps"]:
        if step_id in x["inputs"]:
            x["inputs"] = list(dict.fromkeys(i for j in x["inputs"] for i in (heal if j == step_id else [j])))
    d["output_from"] = list(dict.fromkeys(i for j in d["output_from"] for i in (heal if j == step_id else [j])))
    _save(db, p)
    if by_user:
        event(db, pipeline_id, f"You removed {step_id} from the draft.")


def draft_link(db: Client, pipeline_id: int, source: str, target: str, by_user: bool = False) -> None:
    """Wire `source`'s output into `target` (a step, or "output"). A step with several inputs merges them."""
    p = get(db, pipeline_id)
    d = _draft(p, open_=True)
    _draft_step(d, source)
    if source == target:
        raise PipelineError("A step cannot take its own output.")
    if target == "output":
        if source not in d["output_from"]:
            d["output_from"].append(source)
    else:
        t = _draft_step(d, target)
        if source not in t["inputs"]:
            t["inputs"].append(source)
    _save(db, p)


def draft_unlink(db: Client, pipeline_id: int, source: str, target: str, by_user: bool = False) -> None:
    p = get(db, pipeline_id)
    d = _draft(p, open_=True)
    if target == "output":
        d["output_from"] = [i for i in d["output_from"] if i != source]
    else:
        t = _draft_step(d, target)
        t["inputs"] = [i for i in t["inputs"] if i != source]
    _save(db, p)


def apply_draft(db: Client, pipeline_id: int) -> dict:
    """
    The planner applies the draft. Refused while Check reports errors. Steps
    whose settings and inputs are unchanged keep their approval and preview;
    the rest are proposed again.
    """
    p = get(db, pipeline_id)
    d = _draft(p)
    _require_design(p)
    result = check_draft(p)
    if not result["ok"]:
        listed = "; ".join(e["text"] for e in result["errors"][:3])
        raise PipelineError(f"The draft has {len(result['errors'])} error(s), so it was not applied: {listed}"
                            + (" …" if len(result["errors"]) > 3 else ""))
    context = p["definition"].get("goal") or p["name"]
    old_steps = {s["id"]: s for s in p["definition"]["steps"]}
    old_g = {s["id"]: s for s in graph.normalise(p["definition"])["steps"]}
    try:
        old_sigs = engine.step_signatures(p["definition"], context)
    except graph.GraphError:
        old_sigs = {}
    by_id = {s["id"]: s for s in d["steps"]}
    g = {"steps": [dict(by_id[sid]) for sid in result["order"]], "output_from": list(d["output_from"])}
    new_def = {**_draft_definition(p), "steps": g["steps"], "output_from": g["output_from"]}
    new_sigs = engine.step_signatures(new_def, context)

    kept, reset = [], []
    for s in g["steps"]:
        old = old_steps.get(s["id"])
        if old and old_sigs.get(s["id"]) == new_sigs[s["id"]]:
            s["status"] = old.get("status") or "proposed"
            kept.append(s["id"])
        else:
            if old and old.get("status") == "approved":
                reset.append(s["id"])
            s["status"] = "proposed"
            (p["state"].get("outputs") or {}).pop(s["id"], None)
    added = [s["id"] for s in g["steps"] if s["id"] not in old_steps]
    removed = [sid for sid in old_steps if sid not in by_id]
    for sid in removed:
        (p["state"].get("outputs") or {}).pop(sid, None)
    changed = [s["id"] for s in g["steps"] if s["id"] in old_g and s["id"] not in kept
               and ({k: s.get(k) for k in ("type", "config", "inputs")} != {k: old_g[s["id"]].get(k) for k in ("type", "config", "inputs")})]

    stored = graph.stored(g)
    p["definition"]["steps"] = stored["steps"]
    if "output_from" in stored:
        p["definition"]["output_from"] = stored["output_from"]
    else:
        p["definition"].pop("output_from", None)
    p["definition"].pop("draft", None)
    _save(db, p, state=p["state"])
    parts = [f"added {', '.join(added)}" if added else "", f"removed {', '.join(removed)}" if removed else "",
             f"changed {', '.join(changed)}" if changed else ""]
    summary = "; ".join(x for x in parts if x) or "no changes to the steps"
    event(db, pipeline_id, f"You applied the draft: {summary}." + _reset_text(reset)
          + (f" {len(result['warnings'])} warning(s) from the check remain." if result["warnings"] else ""),
          level="warn" if reset or result["warnings"] else "info")
    return {"added": added, "removed": removed, "changed": changed, "reset": reset, "warnings": result["warnings"]}


# Where the planner put each node on the canvas. Display only: not part of the
# definition hash or the agent's snapshot, and saving it resets nothing.
MAX_LAYOUT_NODES = 300


def set_layout(db: Client, pipeline_id: int, positions: dict) -> dict:
    if not isinstance(positions, dict) or len(positions) > MAX_LAYOUT_NODES:
        raise PipelineError("Layout must be an object of at most 300 node positions.")
    clean = {}
    for k, v in positions.items():
        try:
            clean[str(k)[:64]] = {"x": round(float(v["x"]), 1), "y": round(float(v["y"]), 1)}
        except (TypeError, KeyError, ValueError):
            raise PipelineError(f"Position for {k!r} needs numeric x and y.")
    p = get(db, pipeline_id)
    p["definition"]["layout"] = clean
    _save(db, p)
    return clean


# ── Overrides on the final output ────────────────────────────────────────────

def output_locked(p: dict) -> str | None:
    """Why the final output cannot be overridden right now, or None if it can."""
    steps = p["definition"]["steps"]
    if not steps:
        return "There are no steps yet."
    gaps = engine.design_gaps(p["definition"])
    if gaps:
        return (f"The steps do not produce {', '.join(f'`{g}`' for g in gaps)} yet: ask the agent to add the "
                "steps the pipeline still needs.")
    pending = [s["id"] for s in steps if s.get("status") != "approved"]
    if pending:
        return f"Approve every step first (still to approve: {', '.join(pending)})."
    last = p["state"].get("last_run") or {}
    if not last or last.get("upto") or last.get("definition_hash") != engine.definition_hash(p["definition"]):
        return "Run the pipeline to produce the final output."
    if last.get("errors"):
        return "The last run had errors."
    return None


def edit_output(db: Client, pipeline_id: int, op: str, record_id: str | None = None,
                values: dict | None = None) -> dict:
    """
    The planner's overrides on the final output: set values on a row, remove
    it, add one, or undo every override on a row. Saved on the definition and
    re-applied to every full run; applied to the current output now.
    """
    p = get(db, pipeline_id)
    locked = output_locked(p)
    if locked:
        raise PipelineError(locked)
    fields = _fields(p)
    last = p["state"]["last_run"]
    edits = {k: list(v) for k, v in {**engine.EMPTY_EDITS, **(p["definition"].get("overrides") or {})}.items()}
    shown = {r["id"]: r for r in last["records"]}
    base = {r["id"]: r for r in engine.unapply_edits(last)}
    name = lambda rid: (shown.get(rid) or base.get(rid) or {}).get("values", {}).get(fields[0]["name"]) or rid

    if op == "add":
        clean = engine.clean_edit_values(values or {}, fields)
        if not any(v is not None for v in clean.values()):
            raise PipelineError("Fill at least one field.")
        n = max([int(e["id"].rsplit("-u", 1)[1]) for e in edits["added"]] or [0]) + 1
        record_id = f"you-u{n}"
        edits["added"].append({"id": record_id, "values": clean})
        text = f"added {record_id} ({clean.get(fields[0]['name']) or 'unnamed'})"
    elif op == "set":
        clean = engine.clean_edit_values(values or {}, fields)
        added = next((e for e in edits["added"] if e["id"] == record_id), None)
        if added:
            added["values"].update(clean)
        elif record_id in base:
            orig = base[record_id]
            change = next((e for e in edits["changed"] if e["id"] == record_id), None)
            if not change:
                change = {"id": record_id, "key": engine.record_key(orig, fields), "values": {}}
                edits["changed"].append(change)
            for k, v in clean.items():
                if v == orig["values"].get(k):
                    change["values"].pop(k, None)   # set back to what the pipeline produced
                else:
                    change["values"][k] = v
            if not change["values"]:
                edits["changed"].remove(change)
        else:
            raise PipelineError(f"No row {record_id} in the final output.")
        text = f"changed {record_id} ({name(record_id)}): {', '.join(clean)}"
    elif op == "remove":
        if any(e["id"] == record_id for e in edits["added"]):
            edits["added"] = [e for e in edits["added"] if e["id"] != record_id]
        elif record_id in shown:
            if not any(e["id"] == record_id for e in edits["removed"]):
                edits["removed"].append({"id": record_id, "key": engine.record_key(shown[record_id], fields)})
        else:
            raise PipelineError(f"No row {record_id} in the final output.")
        text = f"removed {record_id} ({name(record_id)})"
    elif op == "undo":
        label = name(record_id)
        for k in edits:
            edits[k] = [e for e in edits[k] if e["id"] != record_id]
        text = f"undid your overrides on {record_id} ({label})"
    else:
        raise PipelineError("op must be add, set, remove or undo.")

    if any(edits.values()):
        p["definition"]["overrides"] = edits
    else:
        p["definition"].pop("overrides", None)
    records, removed, notes = engine.apply_edits(p["definition"].get("overrides"), list(base.values()), fields)
    last.update({"records": records, "removed": removed, "override_notes": notes,
                 "coverage": engine.coverage(records, fields),
                 "definition_hash": engine.definition_hash(p["definition"])})
    review(db, p)   # the output changed, so it is ready for review again
    _save(db, p, state=p["state"])
    event(db, pipeline_id, f"Final output: you {text}.")
    return {"record_id": record_id, "overrides": edits}


# ── Run, diff, load ──────────────────────────────────────────────────────────

def run(db: Client, pipeline_id: int, upto: str | None = None, fresh: bool = False,
        announce: bool = True) -> dict:
    """Run (or preview up to a step). Writes state; returns the last_run summary."""
    p = get(db, pipeline_id)
    repo = PipelineRepository(db)
    steps = p["definition"]["steps"]
    if upto and steps and upto == steps[-1]["id"]:
        upto = None   # previewing the last step runs the whole pipeline

    def progress(msg: str) -> None:
        p["state"]["progress"] = msg
        repo.update(pipeline_id, {"state": p["state"]})

    state = engine.run(p["kind"], p["definition"], p["state"], upto=upto, fresh=fresh,
                       context=p["definition"].get("goal") or p["name"], progress=progress,
                       refs=reference_rows(db))
    state.pop("progress", None)
    last = state["last_run"]
    if upto is None:
        review(db, p)
    repo.update(pipeline_id, {"state": state})
    if announce:
        cov = last["coverage"]
        gaps = ", ".join(f"`{g}`" for g in cov["blocking_gaps"])
        text = (f"{'Preview to ' + upto if upto else ('Fresh re-run' if fresh else 'Run')} finished: "
                f"{cov['records']} record(s)."
                + (f" Started after {last['reused'][-1]}, reusing its saved output." if last.get("reused") else "")
                + (f" Required fields with blanks: {gaps}." if gaps else "")
                + (f" Errors: {'; '.join(last['errors'])}" if last["errors"] else ""))
        if upto is None:
            reasons = gate(p)
            text += (" Ready for review: " + review_text(last["review"], kinds.spec(p["kind"])["label"]) + " Nothing is published until you approve it."
                     if not reasons else " Not ready to publish yet: " + " ".join(reasons))
        event(db, pipeline_id, text, level="warn" if last["errors"] or gaps else "info")
    return summarise_run(last)


def review(db: Client, p: dict) -> dict | None:
    """
    After a full run the output waits for review: what it changes since this
    pipeline last published. Stored on last_run; publishing stamps it.
    """
    last = p["state"].get("last_run") or {}
    if not last or last.get("upto"):
        return None
    prev = PipelineRunRepository(db).latest_published(p["id"])
    last["review"] = {
        "status": "ready",
        "since": ({"dataset_version": prev["dataset_version"], "name": prev.get("name"), "at": prev["created_at"]}
                  if prev else None),
        **engine.changes_since((prev or {}).get("records"), last.get("records") or [], _fields(p)),
    }
    return last["review"]


def review_text(rv: dict, label: str = "Funding") -> str:
    since = f"{label} v{rv['since']['dataset_version']}" if rv.get("since") else None
    if not since:
        return f"{len(rv['added'])} record(s), all new (nothing published from this pipeline yet)."
    return (f"{len(rv['added'])} new, {len(rv['removed'])} gone, {len(rv['changed'])} changed, "
            f"{rv['unchanged']} unchanged since {since}, the last version this pipeline published.")


def summarise_run(last: dict | None) -> dict | None:
    if not last:
        return None
    return {k: last.get(k) for k in ("at", "upto", "reused", "complete", "coverage", "errors", "review", "removed",
                                     "override_notes")}


def gate(p: dict) -> list[str]:
    return engine.load_gate(p["definition"], p["state"], kinds.spec(p["kind"])["required_fields"])


def preview_diff(db: Client, pipeline_id: int) -> dict:
    p = get(db, pipeline_id)
    last = p["state"].get("last_run") or {}
    return kinds.diff(p["kind"], db, p, last.get("records") or [], _fields(p))


def load(db: Client, pipeline_id: int, name: str | None, created_by: str | None) -> dict:
    """
    Publish: write the last full run and snapshot it as the next dataset version.
    This is the planner's approval of the reviewed output; runs never publish.
    Refused while the gate has reasons, including when this output is already published.
    """
    p = get(db, pipeline_id)
    reasons = gate(p)
    if reasons:
        raise PipelineError("Not ready to load: " + " ".join(reasons))
    last = p["state"]["last_run"]
    fields = _fields(p)
    runs = PipelineRunRepository(db)
    prev = runs.latest(pipeline_id)
    changed = not prev or engine.definition_hash(prev["definition"]) != engine.definition_hash(p["definition"])
    def_version = (p.get("definition_version") or 0) + (1 if changed else 0)
    diff = kinds.diff(p["kind"], db, p, last["records"], fields)
    result = kinds.load(p["kind"], db, p, last["records"], fields, name, created_by, def_version)
    v = result["dataset_version"]
    runs.create({
        "pipeline_id": pipeline_id, "definition_version": def_version, "definition": p["definition"],
        "name": (name or "").strip() or None, "records": last["records"], "coverage": last["coverage"],
        "diff": diff, "dataset_version_id": v["id"], "dataset_version": v["version"],
        "created_by": (created_by or "").strip() or None,
    })
    last.setdefault("review", {})["published"] = {"dataset_version": v["version"], "name": v.get("name"),
                                                  "at": v.get("created_at"), "by": (created_by or "").strip() or None}
    PipelineRepository(db).update(pipeline_id, {"state": p["state"],
                                                **({"definition_version": def_version} if changed else {})})
    label = f"{kinds.spec(p['kind'])['label']} v{v['version']}" + (f" “{v['name']}”" if v.get("name") else "")
    event(db, pipeline_id, f"You approved the output: published {result['written']} record(s) as {label} (pipeline def v{def_version}). "
                           "Blocks stay on their analysed version until re-run.", level="ok")
    return {"written": result["written"], "dataset_version": v, "definition_version": def_version, "diff": diff}


# ── Read model for the UI ────────────────────────────────────────────────────

def reference_rows(db: Client):
    """Reads a SQL step's reference tables, once per run, with only the allowed columns."""
    loaded: dict[str, list[dict]] = {}

    def rows(table: str) -> list[dict]:
        if table not in loaded:
            cols = engine.REF_TABLES[table]["columns"]
            try:
                loaded[table] = db.table(table).select(",".join(cols)).limit(engine.SQL_REF_ROWS).execute().data or []
            except Exception as e:  # noqa: BLE001 — reported on the step
                raise PipelineError(f"Reference table {table} could not be read: {e}")
        return loaded[table]
    return rows


def explore_sql(db: Client, pipeline_id: int, query: str, tables: list[str] | None = None,
                on: str | None = None, limit: int = engine.EXPLORE_ROWS) -> dict:
    """
    A read-only query over one step's last previewed output (`on`, default the
    last step that has one; "output" for the last run's final records) and the
    reference tables. Also says whether the query would work as a SQL step.
    """
    p = get(db, pipeline_id)
    fields = _fields(p)
    if not fields:
        raise PipelineError("Set the target schema first: the records table's columns come from it.")
    outputs = p["state"].get("outputs") or {}
    steps = p["definition"]["steps"]
    if on == "output":
        recs, count = (p["state"].get("last_run") or {}).get("records") or [], None
        where = "the last run's final output"
    else:
        if on is None:
            on = next((s["id"] for s in reversed(steps) if outputs.get(s["id"]) and not outputs[s["id"]].get("error")), None)
        elif not any(s["id"] == on for s in steps):
            raise PipelineError(f"No step {on}.")
        out = outputs.get(on) if on else None
        recs, count = (out or {}).get("records") or [], (out or {}).get("count")
        where = (f"the output of {on}" if out and not out.get("error")
                 else f"nothing: {on} has not been previewed with its current settings" if on
                 else "nothing: no step has been previewed yet")
    result = engine.explore_sql(query, tables, recs, fields, reference_rows(db), limit)
    if count is not None and count > len(recs):
        result["notes"].append(f"records holds the first {len(recs)} of {count} records")
    try:
        cols = engine.sql_columns({"query": query, "tables": tables or None}, fields)
        as_step = {"ok": True, "writes": [n for n, _ in cols if n != "id"]}
    except PipelineError as e:
        as_step = {"ok": False, "why": str(e)}
    return {"records_from": where, "records_count": len(recs), **result, "as_step": as_step}


def _prompts(p: dict) -> dict:
    """Each model step's prompt, as the planner reads and tunes it."""
    out = {}
    for st in p["definition"]["steps"]:
        if st["type"] in engine.EDITABLE and _fields(p):
            try:
                out[st["id"]] = engine.prompt_view(st, _fields(p), p["definition"].get("goal") or p["name"])
            except PipelineError as e:
                out[st["id"]] = {"error": str(e)}
    return out


def _graph_view(definition: dict) -> dict:
    g = graph.normalise(definition)
    try:
        order = graph.order(g)
    except graph.GraphError:
        order = [s["id"] for s in g["steps"]]
    return {"inputs": {s["id"]: s["inputs"] for s in g["steps"]}, "output_from": g["output_from"], "order": order}


def _draft_view(p: dict) -> dict | None:
    """The open draft as the canvas draws it, with its check and each step's ports."""
    d = p["definition"].get("draft")
    if d is None:
        return None
    live = {s["id"]: s for s in graph.normalise(p["definition"])["steps"]}
    steps = []
    for s in d["steps"]:
        old = live.get(s["id"])
        state = ("new" if not old else "unchanged" if all(old.get(k) == s.get(k) for k in ("type", "config", "inputs"))
                 else "changed")
        steps.append({**s, "draft_state": state})
    return {"steps": steps, "output_from": d["output_from"], "check": check_draft(p),
            "removed": [sid for sid in live if sid not in {s["id"] for s in d["steps"]}],
            "ports": {s["id"]: engine.step_io(s, _fields(p)) for s in d["steps"]}}


def view(db: Client, pipeline_id: int) -> dict[str, Any]:
    p = get(db, pipeline_id)
    s = kinds.spec(p["kind"])
    state = p["state"]
    outputs = state.get("outputs") or {}
    return {
        "id": p["id"], "kind": p["kind"], "name": p["name"], "kind_label": s["label"],
        "definition": p["definition"], "definition_version": p.get("definition_version") or 0,
        "busy": p.get("busy") if PipelineRepository.is_busy(p) else None,
        "progress": state.get("progress") if PipelineRepository.is_busy(p) else None,
        "sources": state.get("sources") or {},
        "outputs": outputs,
        "last_run": summarise_run(state.get("last_run")),
        "last_records": (state.get("last_run") or {}).get("records", [])[:engine.PREVIEW_RECORDS],
        "gate": gate(p),
        "prompts": _prompts(p),
        "output_locked": output_locked(p),
        "design_gaps": engine.design_gaps(p["definition"]),
        "overrides": p["definition"].get("overrides"),
        "ports": {st["id"]: engine.step_io(st, _fields(p)) for st in p["definition"]["steps"]},
        "graph": _graph_view(p["definition"]),
        "draft": _draft_view(p),
        "ref_tables": {k: {"description": v["description"], "columns": v["columns"]} for k, v in engine.REF_TABLES.items()},
        "sql_types": engine.SQL_TYPES,
        "catalog": {k: {"label": v["label"], "description": v["description"], "model": v["model"], "config": v["config"]}
                    for k, v in engine.catalog_for(p["kind"]).items()},
        "loads": PipelineRunRepository(db).for_pipeline(pipeline_id),
        "created_by": p.get("created_by"), "created_at": p.get("created_at"),
    }
