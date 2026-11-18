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
    p["definition"]["schema"] = {"status": "proposed", "fields": clean}
    _save(db, p)
    if by_user:
        event(db, pipeline_id, f"You set the target schema ({len(clean)} fields).")
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
    src = {"id": _next_id(p["definition"]["sources"], "s"), "kind": kind,
           **({"url": url} if kind == "url" else {"query": query}),
           "label": (label or "").strip() or (url if kind == "url" else f"Grants.gov: {query}")}
    try:
        meta = engine.source_meta(engine.fetch_source(src))
    except PipelineError as e:
        meta = {"error": str(e)}
    p["definition"]["sources"].append(src)
    p["state"].setdefault("sources", {})[src["id"]] = meta
    _save(db, p, state=p["state"])
    if by_user:
        event(db, pipeline_id, f"You added source {src['id']}: {src['label']}"
              + (f" (could not be read: {meta['error']})" if meta.get("error") else f" ({meta['chars']:,} characters)."),
              level="warn" if meta.get("error") else "info")
    return {**src, "fetch": meta}


def remove_source(db: Client, pipeline_id: int, source_id: str, by_user: bool = False) -> None:
    p = get(db, pipeline_id)
    before = len(p["definition"]["sources"])
    p["definition"]["sources"] = [s for s in p["definition"]["sources"] if s["id"] != source_id]
    if len(p["definition"]["sources"]) == before:
        raise PipelineError(f"No source {source_id}.")
    p["state"].get("sources", {}).pop(source_id, None)
    _save(db, p, state=p["state"])
    if by_user:
        event(db, pipeline_id, f"You removed source {source_id}.")


def read_source(db: Client, pipeline_id: int, source_id: str, offset: int = 0, length: int = 4000) -> dict:
    p = get(db, pipeline_id)
    src = next((s for s in p["definition"]["sources"] if s["id"] == source_id), None)
    if not src:
        raise PipelineError(f"No source {source_id}.")
    text = engine.fetch_source(src).get("text") or ""
    offset, length = max(0, int(offset)), max(200, min(int(length), 8000))
    return {"source_id": source_id, "total_chars": len(text), "offset": offset,
            "text": text[offset:offset + length], "more": offset + length < len(text)}


# ── Steps ────────────────────────────────────────────────────────────────────

def add_step(db: Client, pipeline_id: int, step_type: str, config: dict, note: str | None = None,
             after: str | None = None) -> dict:
    p = get(db, pipeline_id)
    cfg = engine.validate_step(p["kind"], step_type, config, _fields(p))
    steps = p["definition"]["steps"]
    step = {"id": _next_id(steps, "t"), "type": step_type, "config": cfg,
            "note": (note or "").strip(), "status": "proposed"}
    idx = next((i + 1 for i, s in enumerate(steps) if s["id"] == after), len(steps))
    steps.insert(idx, step)
    _save(db, p)
    return step


def update_step(db: Client, pipeline_id: int, step_id: str, config: dict | None = None,
                note: str | None = None, by_user: bool = False) -> dict:
    p = get(db, pipeline_id)
    step = next((s for s in p["definition"]["steps"] if s["id"] == step_id), None)
    if not step:
        raise PipelineError(f"No step {step_id}.")
    if config is not None:
        step["config"] = engine.validate_step(p["kind"], step["type"], config, _fields(p))
        step["status"] = "proposed"   # a changed step needs approving again
    if note is not None:
        step["note"] = note.strip()
    _save(db, p)
    if by_user:
        event(db, pipeline_id, f"You edited step {step_id}; it needs approving again.")
    return step


def remove_step(db: Client, pipeline_id: int, step_id: str, by_user: bool = False) -> None:
    p = get(db, pipeline_id)
    before = len(p["definition"]["steps"])
    p["definition"]["steps"] = [s for s in p["definition"]["steps"] if s["id"] != step_id]
    if len(p["definition"]["steps"]) == before:
        raise PipelineError(f"No step {step_id}.")
    _save(db, p)
    if by_user:
        event(db, pipeline_id, f"You removed step {step_id}.")


def approve_step(db: Client, pipeline_id: int, step_id: str) -> dict:
    p = get(db, pipeline_id)
    step = next((s for s in p["definition"]["steps"] if s["id"] == step_id), None)
    if not step:
        raise PipelineError(f"No step {step_id}.")
    step["status"] = "approved"
    _save(db, p)
    event(db, pipeline_id, f"You approved step {step_id} ({engine.CATALOG[step['type']]['label']}).")
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
        _save(db, p)
    return steps


# ── Run, diff, load ──────────────────────────────────────────────────────────

def run(db: Client, pipeline_id: int, upto: str | None = None, fresh: bool = False,
        announce: bool = True) -> dict:
    """Run (or preview up to a step). Writes state; returns the last_run summary."""
    p = get(db, pipeline_id)
    repo = PipelineRepository(db)

    def progress(msg: str) -> None:
        p["state"]["progress"] = msg
        repo.update(pipeline_id, {"state": p["state"]})

    state = engine.run(p["kind"], p["definition"], p["state"], upto=upto, fresh=fresh,
                       context=p["definition"].get("goal") or p["name"], progress=progress)
    state.pop("progress", None)
    repo.update(pipeline_id, {"state": state})
    last = state["last_run"]
    if announce:
        cov = last["coverage"]
        gaps = ", ".join(f"`{g}`" for g in cov["blocking_gaps"])
        text = (f"{'Preview to ' + upto if upto else ('Fresh re-run' if fresh else 'Run')} finished: "
                f"{cov['records']} record(s)."
                + (f" Required fields with blanks: {gaps}." if gaps else "")
                + (f" Errors: {'; '.join(last['errors'])}" if last["errors"] else ""))
        event(db, pipeline_id, text, level="warn" if last["errors"] or gaps else "info")
    return summarise_run(last)


def summarise_run(last: dict | None) -> dict | None:
    if not last:
        return None
    return {k: last.get(k) for k in ("at", "upto", "complete", "coverage", "errors")}


def gate(p: dict) -> list[str]:
    return engine.load_gate(p["definition"], p["state"], kinds.spec(p["kind"])["required_fields"])


def preview_diff(db: Client, pipeline_id: int) -> dict:
    p = get(db, pipeline_id)
    last = p["state"].get("last_run") or {}
    return kinds.diff(p["kind"], db, p, last.get("records") or [], _fields(p))


def load(db: Client, pipeline_id: int, name: str | None, created_by: str | None) -> dict:
    """Write the last full run and snapshot it as the next dataset version. Refused while the gate has reasons."""
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
    if changed:
        PipelineRepository(db).update(pipeline_id, {"definition_version": def_version})
    label = f"{kinds.spec(p['kind'])['label']} v{v['version']}" + (f" “{v['name']}”" if v.get("name") else "")
    event(db, pipeline_id, f"Loaded {result['written']} record(s) as {label} (pipeline def v{def_version}). "
                           "Blocks stay on their analysed version until re-run.", level="ok")
    return {"written": result["written"], "dataset_version": v, "definition_version": def_version, "diff": diff}


# ── Read model for the UI ────────────────────────────────────────────────────

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
        "catalog": {k: {"label": v["label"], "description": v["description"], "model": v["model"]}
                    for k, v in engine.catalog_for(p["kind"]).items()},
        "loads": PipelineRunRepository(db).for_pipeline(pipeline_id),
        "created_by": p.get("created_by"), "created_at": p.get("created_at"),
    }
