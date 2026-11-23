"""
Operations on a pipeline's definition, shared by the chat agent's tools and the
panel's buttons, so both go through the same validation.

Who may do what is the guardrail's other half. The agent may propose: the
schema, sources, steps and previews. Only the user may approve the schema or a
step, accept blanks for a required field, and load. Every user action is also
written into the chat as an event, so the conversation shows what happened.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from postgrest.exceptions import APIError
from supabase import Client

from backend.pipelines import engine, general, graph, jobs, kinds, outputs, zoning_steps
from backend.pipelines.engine import PipelineError
from backend.repositories.pipelines import (
    PipelineMessageRepository,
    PipelineRepository,
    PipelineRunRepository,
)
from backend.tools import datasets

log = logging.getLogger(__name__)


def get(db: Client, pipeline_id: int) -> dict:
    p = PipelineRepository(db).get(pipeline_id)
    if not p:
        raise LookupError(f"Pipeline {pipeline_id} not found")
    p["definition"] = {"schema": {"status": None, "fields": []}, "sources": [], "steps": [],
                       **(p.get("definition") or {})}
    p["state"] = p.get("state") or {}
    if _proposing():
        _as_proposal(p)
    return p


def _save(db: Client, p: dict, **extra) -> None:
    if "_scope" in p:   # one output's view (see _out): write its keys back into the whole pipeline
        real, oid = p["_scope"]
        outputs.sync(real["definition"], oid, p["definition"])
        outputs.merge_state(real["state"], real["definition"], oid, p["state"])
        if "state" in extra:
            extra["state"] = real["state"]
        p = real
    PipelineRepository(db).update(p["id"], {"definition": _stored(p), **extra})


def _out(p: dict, output: str | None = None) -> dict:
    """
    The pipeline as one of its outputs sees it: that output's schema, load
    mapping, overrides, record review and last run, and only the steps that
    feed it (see outputs.py). _save writes changes back. For a one-output
    pipeline this is p itself.
    """
    oid = output or outputs.MAIN
    d = p["definition"]
    if not outputs.multi(d):
        if oid != outputs.MAIN:
            raise PipelineError(f"No output {oid}.")
        return p
    return {**p, "definition": outputs.project(d, oid), "state": outputs.substate(p["state"], d, oid),
            "_scope": (p, oid)}


def _out_name(p: dict) -> str:
    """ " in <output>" when the pipeline has several outputs, for event texts."""
    if "_scope" not in p:
        return ""
    real, oid = p["_scope"]
    return f" in {outputs.label(real['definition'], oid)}"


def _all_schemas_approved(p: dict) -> bool:
    d = p["definition"]
    return all((outputs.entry(d, oid).get("schema") or {}).get("status") == "approved" for oid in outputs.ids(d))


# ── The agent's proposal ─────────────────────────────────────────────────────
#
# The agent does not edit the draft: inside proposing() every draft edit (steps,
# wiring, and sources once they are final) goes to definition["proposal"], a
# copy of the draft (or of the applied steps when no draft is open). The planner
# sees it as dashed nodes and a list of changes, then adds it to the draft or
# discards it. It records what it was based on, so a proposal made before the
# planner changed the steps is refused rather than undoing their change.

_LOCAL = threading.local()


@contextlib.contextmanager
def proposing():
    """Route draft edits made in this thread to the agent's proposal."""
    prev = getattr(_LOCAL, "proposing", False)
    _LOCAL.proposing = True
    try:
        yield
    finally:
        _LOCAL.proposing = prev


def _proposing() -> bool:
    return getattr(_LOCAL, "proposing", False)


def _base_hash(definition: dict) -> str:
    """The steps, wiring and sources the planner has now: the draft's, else the applied ones."""
    d = definition.get("draft")
    if d is None:
        g = graph.normalise(definition)
        steps, out, srcs = g["steps"], g["output_from"], definition.get("sources") or []
        more = {o["id"]: o.get("output_from") or [] for o in outputs.extras(definition)}
    else:
        steps, out = d["steps"], d["output_from"]
        srcs = d["sources"] if d.get("sources") is not None else definition.get("sources") or []
        more = d.get("outputs_from") or {}
    return engine._hash({"steps": [{k: s.get(k) for k in ("id", "type", "config", "inputs")} for s in steps],
                         "output_from": out, "sources": [s["id"] for s in srcs],
                         **({"outputs_from": more} if more else {})})


def _wired(definition: dict, draft: dict, sources: list[dict]) -> dict:
    """The definition with a draft's (or proposal's) steps, wiring and sources in place of the applied ones."""
    out = {**{k: v for k, v in definition.items() if k not in ("draft", "proposal")},
           "sources": sources, "steps": draft["steps"], "output_from": draft["output_from"]}
    if outputs.multi(definition):
        froms = draft.get("outputs_from") or {}
        out["outputs"] = [{**o, "output_from": list(froms.get(o["id"], o.get("output_from") or []))}
                          for o in outputs.extras(definition)]
    return out


def _as_proposal(p: dict) -> None:
    """Show the proposal to the draft functions as if it were the draft."""
    d = p["definition"]
    prop = d.get("proposal")
    p["_real_draft"] = d.get("draft")
    p["_proposal_base"] = prop["base"] if prop else _base_hash(d)
    start = prop["draft"] if prop else d.get("draft")
    if start is None:
        d.pop("draft", None)
    else:
        d["draft"] = copy.deepcopy(start)


def _stored(p: dict) -> dict:
    """The definition as saved: in proposal mode the edited "draft" is the proposal."""
    d = p["definition"]
    if "_real_draft" not in p:
        return d
    out = {k: v for k, v in d.items() if k not in ("draft", "proposal")}
    edited, real = d.get("draft"), p["_real_draft"]
    if edited is not None and (d.get("proposal") or edited != real):
        out["proposal"] = {"draft": edited, "base": p["_proposal_base"],
                           "at": datetime.now(timezone.utc).isoformat()}
    if real is not None:
        out["draft"] = real
    return out


def _proposal_parts(p: dict) -> tuple[dict, dict] | None:
    """(the planner's current steps as a definition, the proposal as a definition)."""
    d = p["definition"]
    prop = d.get("proposal")
    if not prop:
        return None
    base = _draft_definition(p) if d.get("draft") is not None else {
        **{k: v for k, v in d.items() if k not in ("draft", "proposal")},
        "steps": graph.normalise(d)["steps"], "output_from": graph.normalise(d)["output_from"]}
    pd = prop["draft"]
    mine = _wired(d, pd, pd["sources"] if pd.get("sources") is not None else base["sources"])
    return base, mine


def _label(s: dict) -> str:
    return f"{s['id']} {engine.CATALOG.get(s['type'], {}).get('label', s['type'])}"


def proposal_changes(p: dict) -> list[dict]:
    """What adding the proposal would change, one line per node: {op: + ~ -, node, text}."""
    parts = _proposal_parts(p)
    if not parts:
        return []
    base, mine = parts
    old = {s["id"]: s for s in base["steps"]}
    new = {s["id"]: s for s in mine["steps"]}
    old_src = {s["id"]: s for s in base["sources"]}
    new_src = {s["id"]: s for s in mine["sources"]}
    out = []
    for sid, s in new_src.items():
        if sid not in old_src:
            out.append({"op": "+", "node": f"src:{sid}", "text": f"Source {sid} · {s.get('label') or s.get('url') or s.get('query')}"})
    for sid in old_src:
        if sid not in new_src:
            out.append({"op": "-", "node": f"src:{sid}", "text": f"Source {sid} removed"})
    all_old = [x["id"] for x in base["sources"]]
    all_new = [x["id"] for x in mine["sources"]]
    for sid, s in new.items():
        ins = s.get("inputs") or []
        if sid not in old:
            where = f" after {', '.join(ins)}" if ins else " (starts a branch)"
            out.append({"op": "+", "node": f"step:{sid}", "text": f"{_label(s)}{where}"})
            continue
        o = old[sid]
        bits = []
        if s["type"] == "extract":
            was = set((o.get("config") or {}).get("source_ids") or all_old)
            now = set((s.get("config") or {}).get("source_ids") or all_new)
            if now - was:
                bits.append(f"also reads {', '.join(sorted(now - was))}")
            if was - now:
                bits.append(f"no longer reads {', '.join(sorted(was - now))}")
        oc, nc = dict(o.get("config") or {}), dict(s.get("config") or {})
        oc.pop("source_ids", None), nc.pop("source_ids", None)
        keys = sorted(k for k in set(oc) | set(nc) if oc.get(k) != nc.get(k))
        if keys:
            slot = engine.EDITABLE.get(s["type"])
            bits.append("instructions changed" if keys == [slot] else f"settings changed ({', '.join(keys)})")
        if (o.get("inputs") or []) != ins:
            bits.append(f"now takes {', '.join(ins) or 'nothing'}")
        if bits:
            out.append({"op": "~", "node": f"step:{sid}", "text": f"{_label(s)} {'; '.join(bits)}"})
    for sid, o in old.items():
        if sid not in new:
            out.append({"op": "-", "node": f"step:{sid}", "text": f"{_label(o)} removed"})
    for oid, froms in outputs.output_from(mine).items():
        if outputs.output_from(base).get(oid) != froms:
            out.append({"op": "~", "node": oid, "text": f"{outputs.label(mine, oid)} now takes {', '.join(froms) or 'nothing'}"})
    return out


def _proposal_view(p: dict) -> dict | None:
    parts = _proposal_parts(p)
    if not parts:
        return None
    base, mine = parts
    prop = p["definition"]["proposal"]
    old = {s["id"]: s for s in base["steps"]}
    changes = proposal_changes(p)
    changed = {c["node"][5:] for c in changes if c["op"] == "~" and c["node"].startswith("step:")}
    result = outputs.check(p["kind"], mine)
    return {
        "at": prop.get("at"), "stale": prop.get("base") != _base_hash(p["definition"]), "changes": changes,
        "steps": [{**s, "prop_state": "new" if s["id"] not in old else "changed" if s["id"] in changed else "unchanged"}
                  for s in mine["steps"]],
        "sources": [{**x, "prop_state": "unchanged" if x["id"] in {y["id"] for y in base["sources"]} else "new"}
                    for x in mine["sources"]],
        "removed": [sid for sid in old if sid not in {s["id"] for s in mine["steps"]}],
        "removed_sources": [x["id"] for x in base["sources"] if x["id"] not in {y["id"] for y in mine["sources"]}],
        "output_from": mine["output_from"],
        "outputs_from": {o["id"]: o["output_from"] for o in outputs.extras(mine)},
        "check": {"ok": result["ok"], "errors": result["errors"], "warnings": result["warnings"]},
    }


def accept_proposal(db: Client, pipeline_id: int) -> dict:
    """The planner adds the agent's proposal to the draft (opening one). Nothing runs or resets until Apply."""
    p = get(db, pipeline_id)
    d = p["definition"]
    prop = d.get("proposal")
    if not prop:
        raise PipelineError("There is no proposal from the agent.")
    _require_design(p)
    if prop.get("base") != _base_hash(d):
        raise PipelineError("The steps changed after the agent made this proposal, so adding it would undo your change. "
                            "Discard it and ask the agent again.")
    changes = proposal_changes(p)
    d["draft"] = prop["draft"]
    d.pop("proposal")
    _save(db, p)
    event(db, pipeline_id, f"You added the agent's proposal to the draft ({len(changes)} change{'s' if len(changes) != 1 else ''}): "
          + "; ".join(c["text"] for c in changes[:6]) + ("…" if len(changes) > 6 else "") + ". Check it, then Apply.")
    return d["draft"]


def discard_proposal(db: Client, pipeline_id: int) -> None:
    p = get(db, pipeline_id)
    d = p["definition"]
    prop = d.pop("proposal", None)
    if not prop:
        raise PipelineError("There is no proposal from the agent.")
    keep = {s["id"] for s in d["sources"]} | {s["id"] for s in (d.get("draft") or {}).get("sources") or []}
    for sid in [x["id"] for x in prop["draft"].get("sources") or [] if x["id"] not in keep]:
        (p["state"].get("sources") or {}).pop(sid, None)
    _save(db, p, state=p["state"])
    event(db, pipeline_id, "You discarded the agent's proposal.")


# ── Reviewing the output's records ───────────────────────────────────────────
#
# definition["record_review"]: values the planner accepted (a fingerprint per
# field, so a value that changes on a re-run needs accepting again) and records
# they flagged for follow-up. Keyed by record id, with the record's key (its
# first field) to notice a re-run that numbered records differently. Not part
# of the definition hash: reviewing changes no step.

def _review(p: dict) -> dict:
    rr = p["definition"].setdefault("record_review", {})
    rr.setdefault("accepted", {})
    rr.setdefault("flags", {})
    return rr


def _final_record(p: dict, record_id: str) -> dict:
    rec = next((r for r in (p["state"].get("last_run") or {}).get("records") or [] if r["id"] == record_id), None)
    if not rec:
        raise PipelineError(f"No record {record_id} in the last run's output.")
    return rec


def accept_values(db: Client, pipeline_id: int, record_id: str, fields: list[str] | None = None,
                  accept: bool = True, output: str | None = None) -> dict:
    """Accept (or stop accepting) a record's values as they are now: the named fields, or all of them."""
    p = _out(get(db, pipeline_id), output)
    rec = _final_record(p, record_id)
    names = [f["name"] for f in _fields(p)]
    for n in fields or []:
        if n not in names:
            raise PipelineError(f"No field {n!r}.")
    rr = _review(p)
    mark = rr["accepted"].get(record_id)
    if not mark or mark.get("key") != engine.record_key(rec, _fields(p)):
        mark = rr["accepted"][record_id] = {"key": engine.record_key(rec, _fields(p)), "fields": {}}
    for n in fields or names:
        if accept:
            mark["fields"][n] = engine._hash(rec["values"].get(n))
        else:
            mark["fields"].pop(n, None)
    if not mark["fields"]:
        rr["accepted"].pop(record_id)
    _save(db, p)
    if not fields:
        event(db, pipeline_id, f"You {'accepted' if accept else 'reopened'} record {record_id} ({rec['values'].get(names[0]) or 'unnamed'}).")
    return review_marks(p).get(record_id) or {}


def flag_record(db: Client, pipeline_id: int, record_id: str, note: str | None = None, flag: bool = True,
                output: str | None = None) -> dict:
    p = _out(get(db, pipeline_id), output)
    rec = _final_record(p, record_id)
    rr = _review(p)
    name = rec["values"].get(_fields(p)[0]["name"]) if _fields(p) else None
    if flag:
        rr["flags"][record_id] = {"key": engine.record_key(rec, _fields(p)), "note": (note or "").strip()[:500],
                                  "at": datetime.now(timezone.utc).isoformat()}
    else:
        rr["flags"].pop(record_id, None)
    _save(db, p)
    event(db, pipeline_id, f"You {'flagged' if flag else 'cleared the flag on'} record {record_id} ({name or 'unnamed'})"
          + (f": {note.strip()}" if flag and (note or "").strip() else "."), level="warn" if flag else "info")
    return review_marks(p).get(record_id) or {}


def review_marks(p: dict) -> dict:
    """Per record of the last output: which fields are accepted as they are now, and its flag."""
    rr = p["definition"].get("record_review") or {}
    fields = _fields(p)
    out = {}
    for rec in (p["state"].get("last_run") or {}).get("records") or []:
        key = engine.record_key(rec, fields)
        a = (rr.get("accepted") or {}).get(rec["id"])
        accepted = [n for n, fp in (a or {}).get("fields", {}).items()
                    if a.get("key") == key and fp == engine._hash(rec["values"].get(n))]
        flag = (rr.get("flags") or {}).get(rec["id"])
        if flag and flag.get("key") != key:
            flag = None
        if accepted or flag:
            out[rec["id"]] = {"accepted": accepted, "flag": flag}
    return out


def rerun_record(db: Client, pipeline_id: int, step_id: str, record_id: str) -> dict:
    """Run one model step again on one record, without the cache, then the steps after it."""
    p = get(db, pipeline_id)
    repo = PipelineRepository(db)
    stopped = jobs.checker()

    def progress(msg: str) -> None:
        stopped()
        p["state"]["progress"] = msg
        repo.update(pipeline_id, {"state": p["state"]})

    outputs.rerun_record(p["kind"], p["definition"], p["state"], step_id, record_id,
                         context=p["definition"].get("goal") or p["name"], progress=progress,
                         refs=reference_rows(db))
    stopped()
    p["state"].pop("progress", None)
    po = _out(p, outputs.owner(p["definition"], step_id))
    last = po["state"].get("last_run") or {}
    if last.get("upto") is None:
        review(db, po)
    repo.update(pipeline_id, {"state": p["state"]})
    rec = next((r for r in last.get("records") or [] if r["id"] == record_id), None)
    event(db, pipeline_id, f"You re-ran {step_id} for record {record_id}"
          + (f" ({rec['values'].get(_fields(po)[0]['name'])})" if rec and _fields(po) else "")
          + (f". Errors: {'; '.join(last['errors'])}" if last["errors"] else ": the steps after it were recomputed."),
          level="warn" if last["errors"] else "info")
    return summarise_run(last)


def event(db: Client, pipeline_id: int, text: str, level: str = "info") -> None:
    if _proposing():
        return   # the agent's proposal lists its own changes; the draft's events would misdescribe them
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
    definition = {"schema": {"status": None, "fields": []}, "sources": [], "steps": []}
    if len(s["targets"]) > 1:   # a kind with several outputs names the first after what it loads into
        first = next(iter(s["targets"]))
        definition.update(target=first, name=s["targets"][first]["label"])
    try:
        p = PipelineRepository(db).create({
            "kind": kind, "name": (name or "").strip() or f"New {s['label'].lower()} pipeline",
            "definition": definition,
            "state": {}, "created_by": (created_by or "").strip() or None,
        })
    except APIError as e:
        if kind == general.KIND and getattr(e, "code", None) == "23514":   # the kind check predates general pipelines
            raise PipelineError(general.MIGRATION) from e
        raise
    # The opening message is shown, not sent: the model gets the same guidance in its system prompt.
    event(db, p["id"], s["intro"], level="intro")
    return p


# ── Schema ───────────────────────────────────────────────────────────────────

def set_schema(db: Client, pipeline_id: int, fields: list[dict], by_user: bool = False,
               output: str | None = None) -> dict:
    p = _out(get(db, pipeline_id), output)
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
        event(db, pipeline_id, f"The target schema{_out_name(p)} changed." + _reset_text(reset))
    if by_user:
        event(db, pipeline_id, f"You set the target schema{_out_name(p)} ({len(clean)} fields)." + _reset_text(reset))
    return p["definition"]["schema"]


def use_default_schema(db: Client, pipeline_id: int, output: str | None = None) -> dict:
    p = _out(get(db, pipeline_id), output)
    schema = set_schema(db, pipeline_id, kinds.default_fields(p["kind"], p["definition"], db), output=output)
    event(db, pipeline_id, f"You loaded the default fields{_out_name(p)}. Review them, then approve.")
    return schema


def approve_schema(db: Client, pipeline_id: int, output: str | None = None) -> dict:
    p = _out(get(db, pipeline_id), output)
    if not _fields(p):
        raise PipelineError("There is no schema to approve.")
    p["definition"]["schema"]["status"] = "approved"
    _save(db, p)
    unmapped = kinds.load_problems(p["kind"], p["definition"])
    event(db, pipeline_id, f"You approved the target schema{_out_name(p)}."
          + (f" Before publishing: {' '.join(unmapped)}" if unmapped else ""))
    return p["definition"]["schema"]


def view_load_map(p: dict) -> dict | None:
    """p: the pipeline, or one output's view of it (_out)."""
    cat = kinds.target_spec(p["kind"], p["definition"])["catalogue"]
    return kinds.load_map(p["definition"], cat) if cat else None


def set_load_map(db: Client, pipeline_id: int, mapping: dict, by_user: bool = False, output: str | None = None) -> dict:
    """
    How the output's fields map onto the catalogue's columns, {column: field or
    None}; columns left out keep their current choice. Read at Load only, so
    changing it resets no step.
    """
    p = _out(get(db, pipeline_id), output)
    cat = kinds.target_spec(p["kind"], p["definition"])["catalogue"]
    if not cat:
        raise PipelineError(f"{kinds.spec(p['kind'])['label']} pipelines do not load into a catalogue yet.")
    current = {c["column"]: c["field"] for c in kinds.load_map(p["definition"], cat)["columns"]}
    clean = kinds.clean_load_map({**current, **(mapping or {})}, _fields(p), cat)
    p["definition"]["load_map"] = {k: v for k, v in clean.items() if k in (mapping or {}) or k in (p["definition"].get("load_map") or {})}
    _save(db, p)
    shown = ", ".join(f"{v or 'nothing'} → {k}" for k, v in clean.items() if k in (mapping or {}))
    event(db, pipeline_id, f"{_who(by_user)} mapped {shown} for Load{_out_name(p)}.")
    return kinds.load_map(p["definition"], cat)


def set_goal(db: Client, pipeline_id: int, goal: str) -> str:
    """The planner's goal in their words. Every model step reads it, so extraction knows what counts."""
    goal = (goal or "").strip()
    if not goal:
        raise PipelineError("The goal is empty.")
    p = get(db, pipeline_id)
    p["definition"]["goal"] = goal[:2000]
    _save(db, p)
    return p["definition"]["goal"]


def accept_blank(db: Client, pipeline_id: int, field: str, accept: bool, output: str | None = None) -> dict:
    p = _out(get(db, pipeline_id), output)
    f = next((x for x in _fields(p) if x["name"] == field), None)
    if not f:
        raise PipelineError(f"No field named {field!r}.")
    f["accept_blank"] = bool(accept)
    _save(db, p)
    event(db, pipeline_id, f"You {'accepted' if accept else 'stopped accepting'} blanks for `{field}`{_out_name(p)}.")
    return f


# ── Sources ──────────────────────────────────────────────────────────────────

def _source(kind: str, url: str | None, query: str | None, label: str | None, taken: list[dict]) -> dict:
    """A new source, checked, with an id unused by the applied sources and the draft's."""
    if kind == "url":
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise PipelineError("A source URL must start with http:// or https://.")
        if any(s.get("url") == url for s in taken):
            raise PipelineError("That source is already in the pipeline.")
    elif kind == "grants_gov":
        query = (query or "").strip()
        if not query:
            raise PipelineError("A Grants.gov source needs a keyword query.")
    elif kind == "arcgis":
        url = zoning_steps.check_arcgis_url(url)
        if any(s.get("url") == url for s in taken):
            raise PipelineError("That source is already in the pipeline.")
    else:
        raise PipelineError("Source kind must be url, grants_gov or arcgis.")
    return {"id": _next_id(taken, "s"), "kind": kind, **({"query": query} if kind == "grants_gov" else {"url": url}),
            "label": (label or "").strip() or (f"Grants.gov: {query}" if kind == "grants_gov" else url)}


def _fetch_meta(src: dict) -> dict:
    try:
        return engine.source_meta(engine.fetch_source(src, fresh=True))   # downloaded now, then read from its saved copy
    except PipelineError as e:
        return {"error": str(e)}


def _fetch_text(src: dict, meta: dict) -> str:
    return f" (could not be read: {meta['error']})" if meta.get("error") else f" ({meta['chars']:,} characters)."


def add_source(db: Client, pipeline_id: int, kind: str, url: str | None = None, query: str | None = None,
               label: str | None = None, by_user: bool = False) -> dict:
    """
    Add a source and fetch it at once, so a bad URL is reported now rather than
    at run time. While the sources are still being collected it is added
    straight away. Once they are final and steps are designed on them, it goes
    into the draft instead (opening one), to be wired to an Extract step and
    applied with the rest; nothing resets until then.
    """
    p = get(db, pipeline_id)
    if p["definition"].get("sources_status") == "final":
        return _draft_add_source(db, p, kind, url, query, label, by_user)
    src = _source(kind, url, query, label, p["definition"]["sources"] + ((p["definition"].get("draft") or {}).get("sources") or []))
    meta = _fetch_meta(src)
    p["definition"]["sources"].append(src)
    p["state"].setdefault("sources", {})[src["id"]] = meta
    _save(db, p, state=p["state"])
    if by_user:
        event(db, pipeline_id, f"You added source {src['id']}: {src['label']}" + _fetch_text(src, meta),
              level="warn" if meta.get("error") else "info")
    return {**src, "fetch": meta}


def _draft_add_source(db: Client, p: dict, kind: str, url: str | None, query: str | None,
                      label: str | None, by_user: bool) -> dict:
    d = _draft(p, open_=True)
    sources = _draft_sources(p)
    src = _source(kind, url, query, label, sources + p["definition"]["sources"])
    # An Extract step reading "every source" keeps reading the ones it reads now: the new
    # source is read only by the Extract step it is wired to.
    pinned = []
    for s in d["steps"]:
        if s["type"] == "extract" and not (s.get("config") or {}).get("source_ids"):
            s["config"] = {**(s.get("config") or {}), "source_ids": [x["id"] for x in sources]}
            pinned.append(s["id"])
    meta = _fetch_meta(src)
    d["sources"] = sources + [src]
    p["state"].setdefault("sources", {})[src["id"]] = meta
    _save(db, p, state=p["state"])
    event(db, p["id"], f"{_who(by_user)} added source {src['id']} to the draft: {src['label']}" + _fetch_text(src, meta)
          + (f" {', '.join(pinned)} keep{'s' if len(pinned) == 1 else ''} reading the sources {'it' if len(pinned) == 1 else 'they'} read before."
             if pinned else "")
          + " Wire it to an Extract step, then Apply.", level="warn" if meta.get("error") else "info")
    return {**src, "fetch": meta, "draft": True}


def remove_source(db: Client, pipeline_id: int, source_id: str, by_user: bool = False) -> None:
    """Remove a source: straight away while sources are collected, in the draft once they are final."""
    p = get(db, pipeline_id)
    if p["definition"].get("sources_status") == "final":
        d = _draft(p, open_=True)
        sources = _draft_sources(p)
        if not any(s["id"] == source_id for s in sources):
            raise PipelineError(f"No source {source_id}.")
        d["sources"] = [s for s in sources if s["id"] != source_id]
        readers = [s["id"] for s in d["steps"] if s["type"] == "extract"
                   and source_id in ((s.get("config") or {}).get("source_ids") or [])]
        _save(db, p)
        event(db, pipeline_id, f"{_who(by_user)} removed source {source_id} in the draft."
              + (f" {', '.join(readers)} still name{'s' if len(readers) == 1 else ''} it: rewire before applying." if readers else "")
              + " It goes when you Apply.")
        return
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
    _save(db, p, state=p["state"])
    note = (f" {', '.join(rewired)} read only {source_id}, so {'it now reads' if len(rewired) == 1 else 'they now read'}"
            " every source: rewire on the canvas if that is wrong.") if rewired else ""
    if by_user or rewired:
        event(db, pipeline_id, f"{'You removed' if by_user else 'Removed'} source {source_id}." + note,
              level="warn" if rewired else "info")


def _any_source(p: dict, source_id: str) -> dict | None:
    """An applied source, or one only in the draft."""
    return next((s for s in p["definition"]["sources"] + ((p["definition"].get("draft") or {}).get("sources") or [])
                 if s["id"] == source_id), None)


def source_text(db: Client, pipeline_id: int, source_id: str) -> dict:
    """The full fetched text of one source, for the planner to read before marking sources final."""
    p = get(db, pipeline_id)
    src = _any_source(p, source_id)
    if not src:
        raise LookupError(f"No source {source_id}.")
    fetched = engine.fetch_source(src)   # its saved copy; never downloaded again here
    text = fetched.get("text") or ""
    # A CSV is fetched whole and can run to megabytes: the reader shows its start.
    return {**src, **engine.source_meta(fetched), "preview": None, "text": text[:READER_CHARS],
            "shown_chars": min(len(text), READER_CHARS), "links": engine.source_links(fetched)}


READER_CHARS = 300_000


def finalize_sources(db: Client, pipeline_id: int) -> dict:
    """
    The planner's word that the sources are complete. Steps are outlined only
    after this, because what the steps should do depends on what the sources are.
    """
    p = get(db, pipeline_id)
    if not _all_schemas_approved(p):
        raise PipelineError("Approve the target schema first: the fields decide what to look for in the sources."
                            + (" Every output's schema needs approving." if outputs.multi(p["definition"]) else ""))
    sources = p["definition"]["sources"]
    if not sources and not kinds.spec(p["kind"]).get("sources_optional"):
        raise PipelineError("Add at least one source first.")
    meta = p["state"].get("sources") or {}
    bad = [s["id"] for s in sources if (meta.get(s["id"]) or {}).get("error")]
    if bad:
        raise PipelineError(f"{', '.join(bad)} could not be read: remove or replace {'it' if len(bad) == 1 else 'them'} first.")
    p["definition"]["sources_status"] = "final"
    _save(db, p)
    event(db, pipeline_id, f"You marked the {len(sources)} source(s) as final." if sources
          else "You marked the sources final with none: the pipeline reads our tables only.")
    return p["definition"]


def read_source(db: Client, pipeline_id: int, source_id: str, offset: int = 0, length: int = 3000) -> dict:
    p = get(db, pipeline_id)
    src = _any_source(p, source_id)
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

def _check_sources(p: dict, step_type: str, cfg: dict, sources: list[dict] | None = None) -> dict:
    """An Extract step's wiring must name sources that exist (the draft's, for a draft step). Empty means all."""
    if step_type == "extract" and cfg.get("source_ids") is not None:
        known = [s["id"] for s in (p["definition"]["sources"] if sources is None else sources)]
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
        inputs = next(s["inputs"] for s in graph.normalise(p["definition"])["steps"] if s["id"] == step_id)
        clean = _check_sources(p, step["type"], engine.validate_step(p["kind"], step["type"], config,
                                                                     outputs.step_fields(p["definition"], step_id),
                                                                     inputs, p["definition"]["sources"]))
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
    done, tail, level = _approved_tail(p, [step_id])
    event(db, pipeline_id, f"You approved step {step_id} ({engine.CATALOG[step['type']]['label']})." + tail, level=level)
    return step


def _approved_tail(p: dict, approved: list[str]) -> tuple[bool, str, str]:
    """What to add to an approval notice once every step is approved: a gap to fill, or the run to do."""
    d = p["definition"]
    if not all(s.get("status") == "approved" for s in d["steps"]):
        return False, "", "info"
    gaps = sorted({g for sid in approved for g in engine.design_gaps(outputs.project(d, outputs.owner(d, sid)))})
    if gaps:
        return True, f" The pipeline still has no step for {', '.join(f'`{g}`' for g in gaps)}: add one in a draft.", "warn"
    return True, " Every step is approved: run the pipeline to see the final output.", "info"


def approve_all(db: Client, pipeline_id: int) -> dict:
    """
    Approve every step that can be, in run order, on the same terms as one at
    a time: a current preview without errors, and the steps feeding it
    approved (in this pass or before). A step that cannot be approved is
    left, and so is every step its records reach. Returns {approved, left:
    [{step, why}]}.
    """
    p = get(db, pipeline_id)
    steps = {s["id"]: s for s in p["definition"]["steps"]}
    g = graph.normalise(p["definition"])
    outs = p["state"].get("outputs") or {}
    approved, left, blocked = [], [], set()
    for sid in graph.order(g):
        s = steps[sid]
        if s.get("status") == "approved":
            continue
        feeding = [x for x in graph.ancestors(g, sid) if x in blocked]
        out = outs.get(sid)
        why = (f"waits on {', '.join(feeding)}" if feeding
               else "no preview with its current settings" if not out
               else f"its last preview failed: {out['error']}" if out.get("error") else None)
        if why:
            blocked.add(sid)
            left.append({"step": sid, "why": why})
            continue
        s["status"] = "approved"
        approved.append(sid)
    if not approved:
        if not left:
            raise PipelineError("Every step is already approved.")
        raise PipelineError("No step could be approved: " + "; ".join(f"{x['step']} {x['why']}" for x in left)
                            + ". Run the whole pipeline to preview every step, then approve them.")
    _save(db, p)
    done, tail, level = _approved_tail(p, approved)
    still = (" Still to approve: " + "; ".join(f"{x['step']} ({x['why']})" for x in left)
             + ". Run the whole pipeline to preview them." if left else "")
    event(db, pipeline_id, f"You approved {len(approved)} step{'s' if len(approved) > 1 else ''} at once "
          f"({', '.join(approved)}), each on its last preview." + still + tail,
          level="warn" if left or level == "warn" else "info")
    return {"approved": approved, "left": left}


# ── Draft: change the steps, check, apply ────────────────────────────────────
#
# Structural edits (by the planner on the canvas or by the agent) go to
# definition["draft"]: a copy of the steps with their wiring. Nothing runs or
# resets while it is edited. Check reports errors and warnings; Apply refuses
# while there are errors, then replaces the steps and keeps the approval (and
# preview) of every step whose inputs and settings did not change.

def _require_design(p: dict) -> None:
    if not _all_schemas_approved(p):
        raise PipelineError("Steps are designed after the target schema is approved"
                            + (" (every output's)." if outputs.multi(p["definition"]) else "."))
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
        if outputs.multi(p["definition"]):
            d["outputs_from"] = {o["id"]: list(o.get("output_from") or []) for o in outputs.extras(p["definition"])}
        p["definition"]["draft"] = d
    return d


def _draft_sources(p: dict) -> list[dict]:
    """The draft's sources: its own list once it has changed them, else the applied ones."""
    d = p["definition"].get("draft") or {}
    return list(d["sources"]) if d.get("sources") is not None else list(p["definition"]["sources"])


def _draft_definition(p: dict) -> dict:
    return {**_wired(p["definition"], p["definition"]["draft"], _draft_sources(p)),
            **({"proposal": p["definition"]["proposal"]} if "proposal" in p["definition"] else {})}


def _froms(d: dict) -> dict[str, list[str]]:
    """A draft's wiring into each output: {output id: [step ids]}, the first output's list included."""
    return {outputs.MAIN: d["output_from"], **(d.get("outputs_from") or {})}


def _set_from(d: dict, oid: str, froms: list[str]) -> None:
    if oid == outputs.MAIN:
        d["output_from"] = froms
    elif oid in (d.get("outputs_from") or {}):
        d["outputs_from"][oid] = froms
    else:
        raise PipelineError(f"No output {oid}.")


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
    result = outputs.check(p["kind"], _draft_definition(p))
    # As when sources are marked final: a source that could not be fetched cannot go in.
    meta = p["state"].get("sources") or {}
    bad = [s["id"] for s in _draft_sources(p) if (meta.get(s["id"]) or {}).get("error")]
    for sid in bad:
        result["errors"].append({"node": None, "text": f"{sid} could not be read ({meta[sid]['error']}): remove or replace it."})
    result["ok"] = result["ok"] and not bad
    return result


def _splice_after(d: dict, after: str, new_id: str) -> None:
    """Put new_id between `after` and whatever `after` fed."""
    for s in d["steps"]:
        if s["id"] != new_id:
            s["inputs"] = [new_id if i == after else i for i in s["inputs"]]
    for oid, froms in _froms(d).items():
        _set_from(d, oid, [new_id if i == after else i for i in froms])


def draft_add_step(db: Client, pipeline_id: int, step_type: str, config: dict, note: str | None = None,
                   after: str | None = None, before: str | None = None, inputs: list[str] | None = None,
                   connect: bool = True, by_user: bool = False, output: str | None = None) -> dict:
    """
    Add a step to the draft. Placed after `after` (taking its place in the
    wiring), before `before`, with explicit `inputs`, or (connect) at the end
    of `output` (default the first): it then takes what fed that output and
    feeds it itself. connect=False leaves it unwired, for the planner to wire
    on the canvas. Its settings are checked against the schema of the output
    it is placed towards.
    """
    p = get(db, pipeline_id)
    d = _draft(p, open_=True)
    ids = {s["id"] for s in d["steps"]}
    oid = output or outputs.MAIN
    if oid not in _froms(d):
        raise PipelineError(f"No output {oid}.")
    wired = ([after] if after else list(_draft_step(d, before)["inputs"]) if before
             else [i for i in inputs if i in ids] if inputs is not None else list(_froms(d)[oid]) if connect else [])
    near = after or before or next(iter(wired), None)
    fields = outputs.step_fields(_draft_definition(p), near, fallback=oid)
    cfg = _check_sources(p, step_type, engine.validate_step(p["kind"], step_type, config or {}, fields, wired,
                                                            _draft_sources(p)), _draft_sources(p))
    taken = d["steps"] + p["definition"]["steps"]
    step = {"id": _next_id(taken, "t"), "type": step_type, "config": cfg, "note": (note or "").strip(), "inputs": []}
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
        step["inputs"] = list(_froms(d)[oid])
        _set_from(d, oid, [step["id"]])
        where = (" at the end" if step["inputs"] else "") + (f" of {outputs.label(p['definition'], oid)}"
                                                              if outputs.multi(p["definition"]) else "")
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
        fields = outputs.step_fields(_draft_definition(p), step_id)
        s["config"] = _check_sources(p, s["type"], engine.validate_step(p["kind"], s["type"], config, fields, s["inputs"],
                                                                        _draft_sources(p)), _draft_sources(p))
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
    fed = [x for x in d["steps"] if step_id in x["inputs"]] + [oid for oid, froms in _froms(d).items() if step_id in froms]
    heal = s["inputs"] if len(s["inputs"]) == 1 and len(fed) == 1 else []
    for x in d["steps"]:
        if step_id in x["inputs"]:
            x["inputs"] = list(dict.fromkeys(i for j in x["inputs"] for i in (heal if j == step_id else [j])))
    for oid, froms in _froms(d).items():
        _set_from(d, oid, list(dict.fromkeys(i for j in froms for i in (heal if j == step_id else [j]))))
    _save(db, p)
    if by_user:
        event(db, pipeline_id, f"You removed {step_id} from the draft.")


def draft_link(db: Client, pipeline_id: int, source: str, target: str, by_user: bool = False) -> None:
    """Wire `source`'s output into `target` (a step, or an output: "output" or o2, o3 ...). A step with several inputs merges them."""
    p = get(db, pipeline_id)
    d = _draft(p, open_=True)
    _draft_step(d, source)
    if source == target:
        raise PipelineError("A step cannot take its own output.")
    if target in _froms(d):
        if source not in _froms(d)[target]:
            _set_from(d, target, _froms(d)[target] + [source])
    else:
        t = _draft_step(d, target)
        if source not in t["inputs"]:
            t["inputs"].append(source)
    _save(db, p)


def draft_unlink(db: Client, pipeline_id: int, source: str, target: str, by_user: bool = False) -> None:
    p = get(db, pipeline_id)
    d = _draft(p, open_=True)
    if target in _froms(d):
        _set_from(d, target, [i for i in _froms(d)[target] if i != source])
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
        old_sigs = outputs.signatures(p["definition"], context)
    except graph.GraphError:
        old_sigs = {}
    by_id = {s["id"]: s for s in d["steps"]}
    g = {"steps": [dict(by_id[sid]) for sid in result["order"]], "output_from": list(d["output_from"])}
    new_def = {**_draft_definition(p), "steps": g["steps"], "output_from": g["output_from"]}
    new_sigs = outputs.signatures(new_def, context)

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

    old_src = {x["id"] for x in p["definition"]["sources"]}
    new_sources = _draft_sources(p)
    src_added = [x["id"] for x in new_sources if x["id"] not in old_src]
    src_removed = [i for i in old_src if i not in {x["id"] for x in new_sources}]
    for sid in src_removed:
        p["state"].get("sources", {}).pop(sid, None)
    p["definition"]["sources"] = new_sources
    if outputs.multi(p["definition"]):
        # Several outputs: always stored wired, with each output's inputs.
        stored = {"steps": [dict(s) for s in g["steps"]], "output_from": g["output_from"]}
        for o in outputs.extras(p["definition"]):
            o["output_from"] = list((d.get("outputs_from") or {}).get(o["id"], o.get("output_from") or []))
    else:
        stored = graph.stored(g)
    p["definition"]["steps"] = stored["steps"]
    if "output_from" in stored:
        p["definition"]["output_from"] = stored["output_from"]
    else:
        p["definition"].pop("output_from", None)
    p["definition"].pop("draft", None)
    _save(db, p, state=p["state"])
    parts = [f"added {', '.join(added)}" if added else "", f"removed {', '.join(removed)}" if removed else "",
             f"changed {', '.join(changed)}" if changed else "",
             f"added source {', '.join(src_added)}" if src_added else "",
             f"removed source {', '.join(src_removed)}" if src_removed else ""]
    summary = "; ".join(x for x in parts if x) or "no changes to the steps"
    event(db, pipeline_id, f"You applied the draft: {summary}." + _reset_text(reset)
          + (f" {len(result['warnings'])} warning(s) from the check remain." if result["warnings"] else ""),
          level="warn" if reset or result["warnings"] else "info")
    return {"added": added, "removed": removed, "changed": changed, "reset": reset, "warnings": result["warnings"],
            "sources_added": src_added, "sources_removed": src_removed}


# ── Outputs ──────────────────────────────────────────────────────────────────
#
# A pipeline starts with one output. More are added when its data has several
# shapes (see outputs.py): each loads into its own target and has its own schema.
# Adding or renaming one resets nothing; wiring steps into it goes through the
# draft like any other wiring.

def _wire_explicitly(d: dict) -> None:
    """Store a plain chain as explicit wiring, which several outputs need. The hash and fingerprints do not change."""
    g = graph.normalise(d)
    for s, gs in zip(d.get("steps") or [], g["steps"]):
        s["inputs"] = list(gs["inputs"])
    d["output_from"] = list(g["output_from"])
    draft = d.get("draft")
    if draft is not None:
        draft.setdefault("outputs_from", {})


def add_output(db: Client, pipeline_id: int, target: str, name: str | None = None, by_user: bool = False) -> dict:
    """A new, empty output loading into `target`. Each target takes one output."""
    p = get(db, pipeline_id)
    d = p["definition"]
    targets = kinds.spec(p["kind"])["targets"]
    if target not in targets:
        raise PipelineError(f"{target!r} is not something a {kinds.spec(p['kind'])['label'].lower()} output can load into; "
                            f"the targets are {', '.join(targets)}.")
    many = kinds.spec(p["kind"]).get("many_outputs")
    used = {kinds.target_id(p["kind"], outputs.project(d, oid)) for oid in outputs.ids(d)}
    if target in used and not many:
        raise PipelineError(f"An output already loads into {targets[target]['label']}.")
    _wire_explicitly(d)
    oid = _next_id([{"id": "o1"}] + outputs.extras(d), "o")
    entry = {"id": oid, "target": target,
             "name": (name or "").strip() or (f"Output {oid[1:]}" if many else targets[target]["label"]),
             "schema": {"status": None, "fields": []}, "output_from": []}
    d.setdefault("outputs", []).append(entry)
    if d.get("draft") is not None:
        d["draft"].setdefault("outputs_from", {})[oid] = []
    if d.get("proposal") is not None:
        d["proposal"]["draft"].setdefault("outputs_from", {})[oid] = []
    _save(db, p)
    event(db, pipeline_id, f"{_who(by_user)} added an output, {entry['name']} ({oid}), loading into "
          f"{targets[target]['label']}. Set its schema, then wire steps into it in a draft.")
    return entry


def update_output(db: Client, pipeline_id: int, output: str, name: str | None = None, target: str | None = None,
                  by_user: bool = False) -> dict:
    """Rename an output, or change what it loads into while its schema is not approved yet."""
    p = get(db, pipeline_id)
    d = p["definition"]
    e = outputs.entry(d, output)
    targets = kinds.spec(p["kind"])["targets"]
    if target is not None and target != kinds.target_id(p["kind"], outputs.project(d, output)):
        if target not in targets:
            raise PipelineError(f"{target!r} is not a target; they are {', '.join(targets)}.")
        if (e.get("schema") or {}).get("status") == "approved":
            raise PipelineError("Change what an output loads into before approving its schema.")
        if not kinds.spec(p["kind"]).get("many_outputs") and any(
                kinds.target_id(p["kind"], outputs.project(d, o)) == target for o in outputs.ids(d) if o != output):
            raise PipelineError(f"An output already loads into {targets[target]['label']}.")
        old = kinds.target_id(p["kind"], outputs.project(d, output))
        if not e.get("name") or e.get("name") == (targets.get(old) or {}).get("label"):
            e["name"] = targets[target]["label"]   # a name that was the target's follows it
        e["target"] = target
        e.pop("load_map", None)
        if (e.get("schema") or {}).get("fields"):
            e["schema"] = {"status": None, "fields": []}   # the old target's fields would not fit
            _invalidate_from(_out(p, output), 0)
        event(db, pipeline_id, f"{_who(by_user)} set {output} to load into {targets[target]['label']}.")
    if name is not None and name.strip():
        e["name"] = name.strip()[:60]
    _save(db, p, state=p["state"])
    return e


def remove_output(db: Client, pipeline_id: int, output: str, by_user: bool = False) -> None:
    """Remove an output that nothing is wired into (in the steps or the draft)."""
    p = get(db, pipeline_id)
    d = p["definition"]
    if output == outputs.MAIN:
        raise PipelineError("The first output cannot be removed.")
    e = outputs.entry(d, output)
    wired = e.get("output_from") or ((d.get("draft") or {}).get("outputs_from") or {}).get(output)
    if wired:
        raise PipelineError(f"{', '.join(wired)} feed{'s' if len(wired) == 1 else ''} {e['name']}: unwire "
                            f"{'it' if len(wired) == 1 else 'them'} in a draft first.")
    d["outputs"] = [o for o in outputs.extras(d) if o["id"] != output]
    if not d["outputs"]:
        d.pop("outputs")
    for holder in (d.get("draft"), (d.get("proposal") or {}).get("draft")):
        if holder and output in (holder.get("outputs_from") or {}):
            holder["outputs_from"].pop(output)
    outputs.set_last_run(p["state"], output, None)
    _save(db, p, state=p["state"])
    event(db, pipeline_id, f"{_who(by_user)} removed the output {e['name']} ({output}).")


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
    """Why the final output cannot be overridden right now, or None if it can. p: the pipeline or one output's view."""
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
                values: dict | None = None, output: str | None = None) -> dict:
    """
    The planner's overrides on the final output: set values on a row, remove
    it, add one, or undo every override on a row. Saved on the definition and
    re-applied to every full run; applied to the current output now.
    """
    p = _out(get(db, pipeline_id), output)
    if PipelineRepository.is_busy(p):   # checked here, on the row this reads anyway, to save a round trip
        raise PipelineError("The pipeline is busy (the agent or a run is working). Wait for it to finish.")
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
    event(db, pipeline_id, f"Final output{_out_name(p)}: you {text}.")
    return {"record_id": record_id, "overrides": edits}


# ── Run, diff, load ──────────────────────────────────────────────────────────

def run(db: Client, pipeline_id: int, upto: str | None = None, fresh: bool = False,
        announce: bool = True) -> dict:
    """
    Run (or preview up to a step). Writes state; returns the last_run summary
    (of the output `upto` feeds, else of the first output). A full run runs
    every output; previewing the step that feeds an output runs that output whole.
    """
    p = get(db, pipeline_id)
    repo = PipelineRepository(db)
    d = p["definition"]
    froms = outputs.output_from(d)
    only = None
    if upto and not outputs.multi(d):
        if d["steps"] and upto == d["steps"][-1]["id"]:
            upto = None   # previewing the last step runs the whole pipeline
    elif upto and froms.get(outputs.owner(d, upto)) == [upto]:
        upto, only = None, outputs.owner(d, upto)   # previewing the step that feeds an output runs that output whole
    which = [only] if only else [outputs.owner(d, upto)] if upto else outputs.ids(d)
    stopped = jobs.checker()

    def progress(msg: str) -> None:
        stopped()
        p["state"]["progress"] = msg
        repo.update(pipeline_id, {"state": p["state"]})

    outputs.run(p["kind"], d, p["state"], upto=upto, fresh=fresh, context=d.get("goal") or p["name"],
                progress=progress, refs=reference_rows(db), only=only)
    stopped()
    p["state"].pop("progress", None)
    if upto is None:
        for oid in which:
            review(db, _out(p, oid))
    repo.update(pipeline_id, {"state": p["state"]})
    lasts = {oid: outputs.last_run(p["state"], oid) for oid in which}
    if announce:
        parts, warn = [], False
        for oid, last in lasts.items():
            cov = last["coverage"]
            gaps = ", ".join(f"`{g}`" for g in cov["blocking_gaps"])
            warn = warn or bool(last["errors"] or gaps)
            parts.append((f"{outputs.label(d, oid)}: " if outputs.multi(d) else "")
                         + f"{cov['records']} record(s)."
                         + (f" Started after {last['reused'][-1]}, reusing its saved output." if last.get("reused") else "")
                         + (f" Required fields with blanks: {gaps}." if gaps else "")
                         + (f" Errors: {'; '.join(last['errors'])}" if last["errors"] else ""))
        text = f"{'Preview to ' + upto if upto else ('Fresh re-run' if fresh else 'Run')} finished: " + " ".join(parts)
        if upto is None:
            reasons = gate(p)
            ready = " ".join(review_text(last["review"], kinds.spec(p["kind"])["label"]) for last in lasts.values())
            text += (" Ready for review: " + ready + " Nothing is published until you approve it."
                     if not reasons else " Not ready to publish yet: " + " ".join(reasons))
        event(db, pipeline_id, text, level="warn" if warn else "info")
    return summarise_run(next(iter(lasts.values())))


def _published_records(prev: dict | None, oid: str) -> list[dict] | None:
    """One output's records in a publish: a list for a one-output pipeline, {output id: list} for several."""
    recs = (prev or {}).get("records")
    if isinstance(recs, dict):
        return recs.get(oid)
    return recs if oid == outputs.MAIN else None


def review(db: Client, p: dict) -> dict | None:
    """
    After a full run the output waits for review: what it changes since this
    pipeline last published. Stored on last_run; publishing stamps it.
    p: the pipeline, or one output's view of it.
    """
    last = p["state"].get("last_run") or {}
    if not last or last.get("upto"):
        return None
    oid = p["_scope"][1] if "_scope" in p else outputs.MAIN
    prev = PipelineRunRepository(db).latest_published(p["id"])
    last["review"] = {
        "status": "ready",
        "since": ({"dataset_version": prev["dataset_version"], "name": prev.get("name"), "at": prev["created_at"]}
                  if prev else None),
        **engine.changes_since(_published_records(prev, oid), last.get("records") or [], _fields(p)),
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


def gate(p: dict, output: str | None = None) -> list[str]:
    """Every reason publishing is refused: for one output, or (default) for the whole pipeline."""
    d = p["definition"]
    if outputs.multi(d) and output is None and "_scope" not in p:
        return [f"{outputs.label(d, oid)}: {r}" for oid in outputs.ids(d) for r in gate(p, oid)]
    po = _out(p, output) if "_scope" not in p else p
    last = po["state"].get("last_run") or {}
    records = last.get("records") if last and not last.get("upto") else None
    spec = kinds.spec(po["kind"])
    out = (engine.load_gate(po["definition"], po["state"], [], sources_optional=bool(spec.get("sources_optional")))
           + kinds.load_problems(po["kind"], po["definition"], records))
    if po["kind"] == general.KIND:
        out += general.write_problems(_write(po), _fields(po), records, _tables_taken(po))
    return out


# ── Where a general pipeline's output is written ─────────────────────────────

def _scope_of(po: dict) -> tuple[dict, str]:
    """(the whole pipeline, the output id) of a pipeline or one output's view of it."""
    return po["_scope"] if "_scope" in po else (po, outputs.MAIN)


def _write(po: dict) -> dict:
    """The write settings in force for one output (general pipelines): table, mode, key."""
    real, oid = _scope_of(po)
    return general.resolve_write(po["definition"].get("write"), _fields(po), real, oid,
                                 outputs.label(real["definition"], oid))


def _tables_taken(po: dict) -> list[str]:
    """The tables the pipeline's other outputs write."""
    real, oid = _scope_of(po)
    return [_write(_out(real, o))["table"] for o in outputs.ids(real["definition"]) if o != oid]


def set_write(db: Client, pipeline_id: int, table: str | None = None, mode: str | None = None,
              key: list[str] | None = None, by_user: bool = False, output: str | None = None) -> dict:
    """
    Where a general pipeline's output is written: the gp_ table, merge or
    replace, and the key fields. Read at publish only, so it resets no step.
    """
    p = _out(get(db, pipeline_id), output)
    if p["kind"] != general.KIND:
        raise PipelineError("Only general pipelines write to a table of their own.")
    clean = general.clean_write(table, mode, key, _fields(p), _write(p))
    if clean["table"] in _tables_taken(p):
        raise PipelineError(f"Another output of this pipeline writes {clean['table']}: give each output its own table.")
    p["definition"]["write"] = clean
    _save(db, p)
    event(db, pipeline_id, f"{_who(by_user)} set where the output{_out_name(p)} is written: {clean['table']} "
          f"({clean['mode']}, key {' + '.join(clean['key']) or 'none'}).")
    return _write(p)


def write_view(po: dict) -> dict | None:
    """The Output node's "Writes to" panel (general pipelines), else None."""
    if po["kind"] != general.KIND:
        return None
    last = po["state"].get("last_run") or {}
    records = last.get("records") if last and not last.get("upto") else None
    return general.write_view(_write(po), _fields(po), records, _tables_taken(po))


def _parts(p: dict) -> list[dict]:
    """What a publish hands to kinds.diff / kinds.load: one part per output, with its last run's records."""
    parts = []
    for oid in outputs.ids(p["definition"]):
        po = _out(p, oid)
        last = po["state"].get("last_run") or {}
        parts.append({"output": oid, "target": kinds.target_id(p["kind"], po["definition"]),
                      "name": outputs.label(p["definition"], oid),
                      "definition": po["definition"], "records": last.get("records") or [], "fields": _fields(po),
                      **({"write": _write(po)} if p["kind"] == general.KIND else {})})
    return parts


def preview_diff(db: Client, pipeline_id: int) -> dict:
    p = get(db, pipeline_id)
    return kinds.diff(p["kind"], db, p, _parts(p))


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
    d = p["definition"]
    runs = PipelineRunRepository(db)
    prev = runs.latest(pipeline_id)
    changed = not prev or outputs.definition_hash(prev["definition"]) != outputs.definition_hash(d)
    def_version = (p.get("definition_version") or 0) + (1 if changed else 0)
    parts = _parts(p)
    diff = kinds.diff(p["kind"], db, p, parts)
    result = kinds.load(p["kind"], db, p, parts, name, created_by, def_version)
    v = result["dataset_version"]
    lasts = {oid: outputs.last_run(p["state"], oid) for oid in outputs.ids(d)}
    several = outputs.multi(d)
    runs.create({
        "pipeline_id": pipeline_id, "definition_version": def_version, "definition": d,
        "name": (name or "").strip() or None,
        # A one-output pipeline keeps the shape it always had; several outputs are stored by output id.
        "records": {oid: l["records"] for oid, l in lasts.items()} if several else lasts[outputs.MAIN]["records"],
        "coverage": {oid: l["coverage"] for oid, l in lasts.items()} if several else lasts[outputs.MAIN]["coverage"],
        "diff": diff, "dataset_version_id": v["id"], "dataset_version": v["version"],
        "created_by": (created_by or "").strip() or None,
    })
    for last in lasts.values():
        last.setdefault("review", {})["published"] = {"dataset_version": v["version"], "name": v.get("name"),
                                                      "at": v.get("created_at"), "by": (created_by or "").strip() or None}
    PipelineRepository(db).update(pipeline_id, {"state": p["state"],
                                                **({"definition_version": def_version} if changed else {})})
    label = f"{kinds.spec(p['kind'])['label']} v{v['version']}" + (f" “{v['name']}”" if v.get("name") else "")
    event(db, pipeline_id, f"You approved the output: published {result['written']} record(s) as {label} (pipeline def v{def_version}). "
                           + (result.get("note") or "Blocks stay on their analysed version until re-run."), level="ok")
    return {"written": result["written"], "dataset_version": v, "definition_version": def_version, "diff": diff}


# ── Read model for the UI ────────────────────────────────────────────────────

def reference_rows(db: Client):
    """Reads a SQL step's reference tables, once per run, with only the allowed columns."""
    loaded: dict[str, list[dict]] = {}

    def rows(table: str) -> list[dict]:
        if table not in loaded:
            ref = engine.ref_spec(table)
            cols = ref["columns"]
            try:
                if ref.get("rows"):   # data on file that is not a plain table (zoning_steps.ON_FILE)
                    loaded[table] = ref["rows"](db)[:engine.SQL_REF_ROWS]
                else:
                    loaded[table] = db.table(table).select(",".join(cols)).limit(engine.SQL_REF_ROWS).execute().data or []
            except Exception as e:  # noqa: BLE001 — reported on the step
                raise PipelineError(f"Reference table {table} could not be read: {e}")
        return loaded[table]
    return rows


def explore_sql(db: Client, pipeline_id: int, query: str, tables: list[str] | None = None,
                on: str | None = None, limit: int = engine.EXPLORE_ROWS, inputs: list[str] | None = None,
                output: str | None = None) -> dict:
    """
    A read-only query over one step's last previewed output (`on`, default the
    last step that has one; "output" for the last run's final records), or over
    several steps' outputs (`inputs`, as a SQL step with those inputs sees them:
    all in `records`, and each as its own table), plus the reference tables and
    CSV sources. Also says whether the query would work as a SQL step.
    """
    p = get(db, pipeline_id)
    d = p["definition"]
    near = (inputs or [None])[0] or (on if on not in (None, "output") else None)
    fields = outputs.step_fields(d, near) if near else _fields(_out(p, output))
    if not fields:
        raise PipelineError("Set the target schema first: the records table's columns come from it.")
    step_out = p["state"].get("outputs") or {}
    steps = p["definition"]["steps"]
    sources = _draft_sources(p)
    ins = None
    if inputs and len(inputs) == 1 and on is None:
        on = inputs[0]
    if inputs and len(inputs) > 1:
        ins, missing = {}, []
        for i in inputs:
            out = step_out.get(i)
            if not out or out.get("error"):
                missing.append(i)
            ins[i] = (out or {}).get("records") or []
        recs, count = [r for i in inputs for r in ins[i]], None
        where = f"the outputs of {' + '.join(inputs)}" + (f" ({', '.join(missing)} not previewed, so empty)" if missing else "")
    elif on == "output":
        recs, count = outputs.last_run(p["state"], output or outputs.MAIN).get("records") or [], None
        where = "the last run's final output"
    else:
        if on is None:
            on = next((s["id"] for s in reversed(steps) if step_out.get(s["id"]) and not step_out[s["id"]].get("error")), None)
            fields = outputs.step_fields(d, on) if on else fields
        elif not any(s["id"] == on for s in steps):
            raise PipelineError(f"No step {on}.")
        out = step_out.get(on) if on else None
        recs, count = (out or {}).get("records") or [], (out or {}).get("count")
        where = (f"the output of {on}" if out and not out.get("error")
                 else f"nothing: {on} has not been previewed with its current settings" if on
                 else "nothing: no step has been previewed yet")
    result = engine.explore_sql(query, tables, recs, fields, reference_rows(db), limit, ins, sources)
    if count is not None and count > len(recs):
        result["notes"].append(f"records holds the first {len(recs)} of {count} records")
    try:
        cols = engine.sql_columns({"query": query, "tables": tables or None}, fields, inputs, sources)
        as_step = {"ok": True, "writes": [n for n, _ in cols if n != "id"]}
    except PipelineError as e:
        as_step = {"ok": False, "why": str(e)}
    return {"records_from": where, "records_count": len(recs), **result, "as_step": as_step}


def _prompts(p: dict) -> dict:
    """Each model step's prompt, as the planner reads and tunes it."""
    out = {}
    for st in p["definition"]["steps"]:
        fields = outputs.step_fields(p["definition"], st["id"])
        if st["type"] in engine.EDITABLE and fields:
            try:
                out[st["id"]] = engine.prompt_view(st, fields, p["definition"].get("goal") or p["name"], p["kind"])
            except PipelineError as e:
                out[st["id"]] = {"error": str(e)}
    return out


def _graph_view(definition: dict) -> dict:
    g = graph.normalise(definition)
    try:
        order = graph.order(g)
    except graph.GraphError:
        order = [s["id"] for s in g["steps"]]
    return {"inputs": {s["id"]: s["inputs"] for s in g["steps"]}, "output_from": g["output_from"], "order": order,
            "outputs_from": {o["id"]: o.get("output_from") or [] for o in outputs.extras(definition)},
            "owner": outputs.owners(definition) if outputs.multi(definition) else None}


def _draft_view(p: dict) -> dict | None:
    """The open draft as the canvas draws it, with its check and each step's ports."""
    d = p["definition"].get("draft")
    if d is None:
        return None
    live = {s["id"]: s for s in graph.normalise(p["definition"])["steps"]}
    live_ids = [x["id"] for x in p["definition"]["sources"]]
    draft_ids = [x["id"] for x in _draft_sources(p)]

    def own(s: dict, all_ids: list[str]) -> dict:
        """What a step is, with "every source" spelled out, so pinning it is not a change."""
        cfg = dict(s.get("config") or {})
        if s["type"] == "extract":
            cfg["source_ids"] = cfg.get("source_ids") or all_ids
        return {"type": s["type"], "config": cfg, "inputs": s.get("inputs")}

    # Which applied steps Apply sends back for approval (their fingerprint changes).
    context = p["definition"].get("goal") or p["name"]
    dd = _draft_definition(p)
    try:
        old_sigs = outputs.signatures(p["definition"], context)
        new_sigs = outputs.signatures(dd, context)
    except graph.GraphError:
        old_sigs, new_sigs = {}, {}
    steps = []
    for s in d["steps"]:
        old = live.get(s["id"])
        state = ("new" if not old else "unchanged" if own(old, live_ids) == own(s, draft_ids) else "changed")
        resets = bool(old) and old.get("status") == "approved" and old_sigs.get(s["id"]) != new_sigs.get(s["id"])
        steps.append({**s, "draft_state": state, "resets": resets})
    live_src = {x["id"] for x in p["definition"]["sources"]}
    sources = [{**x, "draft_state": "unchanged" if x["id"] in live_src else "new"} for x in _draft_sources(p)]
    return {"steps": steps, "output_from": d["output_from"], "outputs_from": d.get("outputs_from") or {},
            "check": check_draft(p), "sources": sources,
            "removed_sources": [i for i in live_src if i not in {x["id"] for x in sources}],
            "removed": [sid for sid in live if sid not in {s["id"] for s in d["steps"]}],
            "ports": {s["id"]: engine.step_io(s, outputs.step_fields(dd, s["id"]), _draft_sources(p)) for s in d["steps"]}}


def _source_tables(p: dict) -> dict:
    """The CSV sources (applied or in the draft) a SQL step can join, with their columns."""
    out = {}
    for s in p["definition"]["sources"] + ((p["definition"].get("draft") or {}).get("sources") or []):
        meta = (p["state"].get("sources") or {}).get(s["id"]) or {}
        maybe = "csv" in (meta.get("content_type") or "") or (s.get("url") or "").lower().split("?")[0].endswith(".csv")
        if s["id"] not in out and s.get("kind") == "url" and maybe and not meta.get("error"):
            cols = engine.source_columns(s)
            if cols:
                out[s["id"]] = {"columns": cols, "label": s.get("label")}
    return out


def _next_version(db: Client, kind: str) -> int:
    """The number the next publish of this kind will get (versions are per kind, not per pipeline)."""
    latest = datasets.resolve(db, kind)
    return (latest["version"] if latest else 0) + 1


def _output_view(p: dict, oid: str) -> dict:
    """One output as the canvas and the Review & publish page show it."""
    po = _out(p, oid)
    t = kinds.target_spec(p["kind"], po["definition"])
    last = po["state"].get("last_run")
    return {
        "id": oid, "name": outputs.label(p["definition"], oid), "target": t["id"], "target_label": t["label"],
        "schema": po["definition"]["schema"], "output_from": outputs.output_from(p["definition"])[oid],
        "last_run": summarise_run(last),
        "last_records": (last or {}).get("records", [])[:engine.PREVIEW_RECORDS],
        "gate": gate(po), "output_locked": output_locked(po), "design_gaps": engine.design_gaps(po["definition"]),
        "overrides": po["definition"].get("overrides"), "review_marks": review_marks(po),
        "load_map": view_load_map(po), "has_defaults": bool(t["default_fields"]), "write": write_view(po),
    }


def view(db: Client, pipeline_id: int) -> dict[str, Any]:
    p = get(db, pipeline_id)
    s = kinds.spec(p["kind"])
    state = p["state"]
    step_out = state.get("outputs") or {}
    d = p["definition"]
    main = _out(p, outputs.MAIN)
    return {
        "id": p["id"], "kind": p["kind"], "name": p["name"], "kind_label": s["label"],
        "definition": p["definition"], "definition_version": p.get("definition_version") or 0,
        "busy": p.get("busy") if PipelineRepository.is_busy(p) else None,
        "progress": state.get("progress") if PipelineRepository.is_busy(p) else None,
        "sources": state.get("sources") or {},
        "outputs": step_out,
        # The first output's run, schema and review, as before; output_list has every output.
        "last_run": summarise_run(state.get("last_run")),
        "last_records": (state.get("last_run") or {}).get("records", [])[:engine.PREVIEW_RECORDS],
        "gate": gate(p),
        "prompts": _prompts(p),
        "output_locked": output_locked(main),
        "design_gaps": engine.design_gaps(main["definition"]),
        "overrides": d.get("overrides"),
        "ports": {st["id"]: engine.step_io(st, outputs.step_fields(d, st["id"]), d["sources"])
                  for st in graph.normalise(d)["steps"]},
        "output_list": [_output_view(p, oid) for oid in outputs.ids(d)],
        "targets": [{"id": k, "label": v["label"]} for k, v in s["targets"].items()],
        "graph": _graph_view(p["definition"]),
        "draft": _draft_view(p),
        "proposal": _proposal_view(p),
        "review_marks": review_marks(main),
        "ref_tables": {k: {"description": v["description"], "columns": v["columns"]}
                       for k, v in engine.ref_tables(p["kind"]).items()},
        "source_tables": _source_tables(p),
        "load_map": view_load_map(main),
        "write": write_view(main),
        "has_defaults": bool(kinds.target_spec(p["kind"], main["definition"])["default_fields"]),
        "many_outputs": bool(s.get("many_outputs")),
        "sources_optional": bool(s.get("sources_optional")),
        "sql_types": engine.SQL_TYPES,
        "catalog": {k: {"label": v["label"], "description": v["description"], "model": v["model"], "config": v["config"]}
                    for k, v in engine.catalog_for(p["kind"]).items()},
        "loads": PipelineRunRepository(db).for_pipeline(pipeline_id),
        "next_dataset_version": _next_version(db, p["kind"]),
        "created_by": p.get("created_by"), "created_at": p.get("created_at"),
    }
