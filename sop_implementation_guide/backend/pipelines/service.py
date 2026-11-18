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

from backend.pipelines import engine, kinds
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
    for s in p["definition"]["steps"]:
        ids = (s.get("config") or {}).get("source_ids")
        if s["type"] == "extract" and ids and source_id in ids:
            s["config"]["source_ids"] = [i for i in ids if i != source_id] or None
            if s["config"]["source_ids"] is None:
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

def _check_sources(p: dict, step_type: str, cfg: dict) -> dict:
    """An Extract step's wiring must name sources that exist. An empty list means all sources."""
    if step_type == "extract" and cfg.get("source_ids") is not None:
        known = [s["id"] for s in p["definition"]["sources"]]
        unknown = [i for i in cfg["source_ids"] if i not in known]
        if unknown:
            raise PipelineError(f"No source {', '.join(map(str, unknown))}; the sources are {', '.join(known) or 'none'}.")
        cfg["source_ids"] = [i for i in known if i in cfg["source_ids"]] or None
    return cfg


def add_step(db: Client, pipeline_id: int, step_type: str, config: dict, note: str | None = None,
             after: str | None = None, position: int | None = None, by_user: bool = False) -> dict:
    """Insert after the step `after`, or at index `position` (0 = first), or at the end."""
    p = get(db, pipeline_id)
    who = "you approve" if by_user else "the planner approves"
    if p["definition"]["schema"].get("status") != "approved":
        raise PipelineError(f"Steps are outlined after {who} the target schema."
                            + ("" if by_user else " Ask them to approve it."))
    if p["definition"].get("sources_status") != "final":
        raise PipelineError("Steps are added after the sources are marked final (the 'Sources are final' button)."
                            + ("" if by_user else " Ask the planner to finish adding sources and press it."))
    cfg = _check_sources(p, step_type, engine.validate_step(p["kind"], step_type, config, _fields(p)))
    steps = p["definition"]["steps"]
    step = {"id": _next_id(steps, "t"), "type": step_type, "config": cfg,
            "note": (note or "").strip(), "status": "proposed"}
    if position is not None:
        idx = max(0, min(int(position), len(steps)))
    else:
        idx = next((i + 1 for i, s in enumerate(steps) if s["id"] == after), len(steps))
    steps.insert(idx, step)
    reset = _invalidate_from(p, idx)   # later steps now work on this step's output
    _save(db, p, state=p["state"])
    if by_user:
        where = f"after {steps[idx - 1]['id']}" if idx > 0 else "as the first step"
        event(db, pipeline_id, f"You added step {step['id']} ({engine.CATALOG[step_type]['label']}) {where}. "
              "Preview it, then approve it." + _reset_text(reset))
    elif reset:
        event(db, pipeline_id, f"Step {step['id']} was added before approved steps." + _reset_text(reset))
    return step


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


def update_step(db: Client, pipeline_id: int, step_id: str, config: dict | None = None,
                note: str | None = None, by_user: bool = False) -> dict:
    p = get(db, pipeline_id)
    steps = p["definition"]["steps"]
    step = next((s for s in steps if s["id"] == step_id), None)
    if not step:
        raise PipelineError(f"No step {step_id}.")
    reset, changed = [], False
    if config is not None:
        clean = _check_sources(p, step["type"], engine.validate_step(p["kind"], step["type"], config, _fields(p)))
        if clean != step["config"]:
            step["config"], changed = clean, True
            reset = _invalidate_from(p, steps.index(step))
    if note is not None:
        step["note"] = note.strip()
    _save(db, p, state=p["state"])
    if by_user and changed:
        what = "prompt instructions" if step["type"] in engine.EDITABLE else "settings"
        event(db, pipeline_id, f"You changed step {step_id}'s {what}. Preview it again to check the output."
              + _reset_text(reset))
    return step


def remove_step(db: Client, pipeline_id: int, step_id: str, by_user: bool = False) -> None:
    p = get(db, pipeline_id)
    steps = p["definition"]["steps"]
    i = next((n for n, s in enumerate(steps) if s["id"] == step_id), None)
    if i is None:
        raise PipelineError(f"No step {step_id}.")
    steps.pop(i)
    (p["state"].get("outputs") or {}).pop(step_id, None)
    reset = _invalidate_from(p, i)
    _save(db, p, state=p["state"])
    if by_user:
        event(db, pipeline_id, f"You removed step {step_id}." + _reset_text(reset))


def approve_step(db: Client, pipeline_id: int, step_id: str) -> dict:
    """
    Approving a step means its prompt (or settings) and its output were checked.
    So it needs a preview with the current settings, and the steps before it
    approved first: its output depends on theirs.
    """
    p = get(db, pipeline_id)
    steps = p["definition"]["steps"]
    step = next((s for s in steps if s["id"] == step_id), None)
    if not step:
        raise PipelineError(f"No step {step_id}.")
    earlier = [s["id"] for s in steps[:steps.index(step)] if s.get("status") != "approved"]
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
             f" The pipeline still has no step for {', '.join(f'`{g}`' for g in gaps)}: ask the agent to add it."
             if gaps else " Every step is approved: run the pipeline to see the final output."),
          level="warn" if done and gaps else "info")
    return step


def move_step(db: Client, pipeline_id: int, step_id: str, direction: int) -> list[dict]:
    p = get(db, pipeline_id)
    steps = p["definition"]["steps"]
    i = next((n for n, s in enumerate(steps) if s["id"] == step_id), None)
    if i is None:
        raise PipelineError(f"No step {step_id}.")
    j = i + (1 if direction > 0 else -1)
    if 0 <= j < len(steps):
        steps[i], steps[j] = steps[j], steps[i]
        reset = _invalidate_from(p, min(i, j))
        _save(db, p, state=p["state"])
        event(db, pipeline_id, f"You moved step {step_id}." + _reset_text(reset))
    return steps


def place_step(db: Client, pipeline_id: int, step_id: str, position: int) -> list[dict]:
    """Move a step to index `position` in the run order (rewiring it on the canvas)."""
    p = get(db, pipeline_id)
    steps = p["definition"]["steps"]
    i = next((n for n, s in enumerate(steps) if s["id"] == step_id), None)
    if i is None:
        raise PipelineError(f"No step {step_id}.")
    j = max(0, min(int(position), len(steps) - 1))
    if i == j:
        return steps
    steps.insert(j, steps.pop(i))
    reset = _invalidate_from(p, min(i, j))
    _save(db, p, state=p["state"])
    where = f"after {steps[j - 1]['id']}" if j > 0 else "to the start"
    event(db, pipeline_id, f"You moved step {step_id} {where}." + _reset_text(reset))
    return steps


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
        "ref_tables": {k: {"description": v["description"], "columns": v["columns"]} for k, v in engine.REF_TABLES.items()},
        "sql_types": engine.SQL_TYPES,
        "catalog": {k: {"label": v["label"], "description": v["description"], "model": v["model"], "config": v["config"]}
                    for k, v in engine.catalog_for(p["kind"]).items()},
        "loads": PipelineRunRepository(db).for_pipeline(pipeline_id),
        "created_by": p.get("created_by"), "created_at": p.get("created_at"),
    }
