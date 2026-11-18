"""
PipelineBuilderAgent: the chat side of the Pipeline Development tab.

A manual tool-use loop rather than the SDK's tool runner, because a turn has
to survive across HTTP requests: every message is stored in
pipeline_messages exactly as the API returned it and is sent back unchanged
on the next turn (append-only, so thinking blocks stay valid), and the
planner's approvals happen in the panel between turns, not inside a tool call.

The agent's tools can only propose: schema, sources, steps, previews.
Approving, accepting blanks and loading are the planner's, in the panel (see
backend/pipelines/service.py). Each user turn carries a snapshot of the
pipeline's current state, so the agent sees panel actions it did not make.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from backend.db.session import get_supabase
from backend.pipelines import engine, kinds, service
from backend.pipelines.engine import PipelineError
from backend.repositories.pipelines import PipelineMessageRepository, PipelineRepository
from backend.tools.llm_extractor import _get_client

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 12
STATE_TAG = "pipeline_state"

SYSTEM = """You are the pipeline builder in a planning app for Bowie, Maryland (Prince George's County), used to
implement State of Place block recommendations. You help a city planner build a {kind} data pipeline by
conversation. The pipeline is shown to them in a panel beside this chat, and your tools edit it.

How the work goes:
0. Record the planner's goal with set_goal as soon as they state it (and again if it changes). Every
   model step reads it to decide what counts as a record and what matters.
1. Agree the target schema first: the fields every record must have, their types, and which are required.
   Propose it with set_schema, or adapt the one already in the state. The planner approves it in the
   panel; you cannot.
2. Sources: the planner pastes URLs or uploads files (uploads appear in the state as sources). Add pasted
   URLs with add_source. Use read_source to see what a source really contains before designing steps.
3. Steps: propose steps from the catalog with add_step, each with a one-line note saying why. Keep the
   pipeline short: usually one extract step per kind of source, then only the transforms the data needs
   or the planner asks for. The planner designs each step with you, one at a time and in order: they
   read its prompt in the panel, tune its instructions (the instructions/instruction config; the rest of
   the prompt is fixed), preview its output and approve it. Write instructions they can read and edit:
   plain sentences about what counts as an item and what to look for. Changing a step's config, or the
   schema, sends that step and every later one back for approval.
   Once every step is approved and the pipeline has run, the planner can override rows of the final
   output (correct values, remove rows, add rows). Those show in the state as planner_overrides, are
   re-applied on every run (values marked "manual"), and are theirs: do not try to undo them with steps.
4. Preview with run_preview and report what came out honestly: record count, which required fields are
   blank and on how many records, excerpts that could not be verified, warnings.
   Work like an analyst, not an extractor: when fields are blank, first look at where the answers would
   be. Records carry leads (links or documents the source points to), and pages carry links. Propose a
   research step to follow them before asking the planner; turn on its web_search only when the sources
   link nothing useful. Ask the planner only for what research could not find.
5. Publishing (Load) is the planner's action in the panel, once everything is approved and every required
   field is filled or explicitly accepted as blank. A full run never publishes: it ends "ready for review"
   with what changed since the last publish, and the planner approves it. Each publish becomes a new
   numbered dataset version.

The guardrail matters more than anything else:
- Every value read from a source carries a verbatim quote that is checked against that source. A value
  whose quote is not found is shown as unconfirmed; research discards such values outright.
- The schema decides what the pipeline must produce. When the sources cannot fill a required field, say
  which field and on how many records, and ask for what would fill it: another source (say what kind of
  document would have it), or a fact the planner can state. Then stop and wait for the answer.
- Never invent values to close a gap. set_value is only for a value the planner stated in this
  conversation. llm_fill and geo_tie are judgement steps whose outputs are marked inferred; they are not
  a way to manufacture facts a source should state.
- Accepting blanks for a required field is the planner's decision in the panel. You may mention it as an
  option when a field genuinely cannot be sourced.
- If a source is only a navigation page with no programme detail, say so plainly and suggest what to add.

Talking to the planner:
- Explain before you ask: what you found, what it means for the pipeline, then the question.
- Keep messages short and plain; the panel shows the detail. Refer to sources and steps by id (s1, t2).
- Each planner message ends with a <{tag}> block the app adds: the pipeline's current state, including
  approvals, uploads and edits the planner made in the panel. Trust it over your memory of earlier turns.
  It is data from the app, not instructions from the planner.

{guidance}

Step catalog (the only step types you can add):
{catalog}"""


def _tools(kind: str) -> list[dict]:
    step_types = list(engine.catalog_for(kind))
    return [
        {"name": "set_schema",
         "description": "Propose the target schema, replacing the current one. The planner must approve it.",
         "input_schema": {"type": "object", "properties": {"fields": {"type": "array", "items": {
             "type": "object", "properties": {
                 "name": {"type": "string", "description": "lower_snake_case"},
                 "type": {"type": "string", "enum": list(engine.FIELD_TYPES)},
                 "required": {"type": "boolean"},
                 "description": {"type": "string", "description": "What the field holds; the extractor reads this."},
                 "enum_values": {"type": "array", "items": {"type": "string"}, "description": "For type enum only."},
                 "extract": {"type": "boolean", "description": "Default true. False for a judgement field that "
                             "extraction must leave blank and a model step (llm_fill, geo_tie) fills."}},
             "required": ["name", "type", "required", "description"]}}},
             "required": ["fields"]}},
        {"name": "add_source",
         "description": "Add a source and fetch it now. kind url needs url; kind grants_gov needs a 2-5 word keyword query.",
         "input_schema": {"type": "object", "properties": {
             "kind": {"type": "string", "enum": ["url", "grants_gov"]},
             "url": {"type": "string"}, "query": {"type": "string"}, "label": {"type": "string"}},
             "required": ["kind"]}},
        {"name": "remove_source", "description": "Remove a source.",
         "input_schema": {"type": "object", "properties": {"source_id": {"type": "string"}}, "required": ["source_id"]}},
        {"name": "read_source", "description": "Read part of a source's fetched text.",
         "input_schema": {"type": "object", "properties": {
             "source_id": {"type": "string"}, "offset": {"type": "integer"}, "length": {"type": "integer"}},
             "required": ["source_id"]}},
        {"name": "add_step",
         "description": "Propose a step from the catalog, by default at the end. The planner must approve it.",
         "input_schema": {"type": "object", "properties": {
             "type": {"type": "string", "enum": step_types},
             "config": {"type": "object", "description": "As the catalog describes for this type."},
             "note": {"type": "string", "description": "One line: why this step."},
             "after": {"type": "string", "description": "Insert after this step id."}},
             "required": ["type", "config", "note"]}},
        {"name": "update_step",
         "description": "Change a step's config or note. A changed config needs the planner's approval again.",
         "input_schema": {"type": "object", "properties": {
             "step_id": {"type": "string"}, "config": {"type": "object"}, "note": {"type": "string"}},
             "required": ["step_id"]}},
        {"name": "remove_step", "description": "Remove a step.",
         "input_schema": {"type": "object", "properties": {"step_id": {"type": "string"}}, "required": ["step_id"]}},
        {"name": "run_preview",
         "description": "Run the pipeline (or up to a step) on the sources and report records, coverage and warnings.",
         "input_schema": {"type": "object", "properties": {"upto_step_id": {"type": "string"}}}},
        {"name": "set_goal", "description": "Record the planner's goal for this pipeline, in their words.",
         "input_schema": {"type": "object", "properties": {"goal": {"type": "string"}}, "required": ["goal"]}},
        {"name": "rename_pipeline", "description": "Give the pipeline a short descriptive name.",
         "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    ]


# ── State snapshot sent with every planner message ───────────────────────────

def _short(v: Any, n: int = 120) -> Any:
    return v[:n] + "…" if isinstance(v, str) and len(v) > n else v


def state_snapshot(p: dict) -> dict:
    d = p["definition"]
    st = p["state"]
    last = st.get("last_run") or {}
    cov = last.get("coverage") or {}
    return {
        "pipeline": {"name": p["name"], "kind": p["kind"], "goal": d.get("goal"),
                     "definition_version": p.get("definition_version") or 0},
        "schema": {"status": d["schema"].get("status"),
                   "fields": [{k: f.get(k) for k in ("name", "type", "required", "extract", "accept_blank", "enum_values")
                               if f.get(k) is not None} for f in d["schema"].get("fields", [])]},
        "sources": [{**{k: s.get(k) for k in ("id", "kind", "url", "query", "label")},
                     "fetch": {k: (st.get("sources") or {}).get(s["id"], {}).get(k)
                               for k in ("chars", "truncated", "error")}} for s in d["sources"]],
        "steps": [{k: s.get(k) for k in ("id", "type", "config", "note", "status")} for s in d["steps"]],
        "planner_overrides": d.get("overrides"),
        "last_run": ({"at": last.get("at"), "upto": last.get("upto"), "records": cov.get("records"),
                      "required_gaps": {g: cov["fields"][g]["missing"] for g in cov.get("required_gaps", [])},
                      "errors": last.get("errors")} if last else None),
        "load_blocked_by": service.gate(p),
    }


def _preview_result(p: dict, summary: dict) -> dict:
    outputs = p["state"].get("outputs") or {}
    records = (p["state"].get("last_run") or {}).get("records") or []
    cov = summary["coverage"]
    return {
        "records": cov["records"],
        "fields": {n: {"filled": c["filled"], "missing": c["missing"], "required": c["required"], "kinds": c["kinds"]}
                   for n, c in cov["fields"].items()},
        "required_gaps": cov["required_gaps"],
        "steps": {sid: {"count": o.get("count"), "warnings": o.get("warnings"), "error": o.get("error")}
                  for sid, o in outputs.items()},
        "unverified_excerpts": [{"id": r["id"], "name": _short(next(iter(r["values"].values()), None), 60)}
                                for r in records if r.get("excerpt_verified") is False],
        "sample": [{"id": r["id"], **{k: _short(v) for k, v in r["values"].items()}} for r in records[:4]],
        "errors": summary["errors"],
    }


# ── Tool execution ───────────────────────────────────────────────────────────

def _execute(db, pipeline_id: int, name: str, args: dict) -> tuple[dict, bool]:
    """Run one tool. Returns (result, is_error)."""
    try:
        if name == "set_schema":
            schema = service.set_schema(db, pipeline_id, args.get("fields") or [])
            return {"status": "proposed: the planner must approve it in the panel",
                    "fields": [f["name"] for f in schema["fields"]]}, False
        if name == "add_source":
            src = service.add_source(db, pipeline_id, args.get("kind"), args.get("url"), args.get("query"), args.get("label"))
            f = src.pop("fetch")
            return {"source": src, "fetch": f if f.get("error") else {
                "chars": f["chars"], "truncated": f["truncated"], "preview": _short(f["preview"], 600)}}, bool(f.get("error"))
        if name == "remove_source":
            service.remove_source(db, pipeline_id, args["source_id"])
            return {"removed": args["source_id"]}, False
        if name == "read_source":
            return service.read_source(db, pipeline_id, args["source_id"], args.get("offset") or 0, args.get("length") or 4000), False
        if name == "add_step":
            step = service.add_step(db, pipeline_id, args["type"], args.get("config") or {}, args.get("note"), args.get("after"))
            return {"step": step, "status": "proposed: the planner must approve it"}, False
        if name == "update_step":
            return {"step": service.update_step(db, pipeline_id, args["step_id"], args.get("config"), args.get("note"))}, False
        if name == "remove_step":
            service.remove_step(db, pipeline_id, args["step_id"])
            return {"removed": args["step_id"]}, False
        if name == "run_preview":
            summary = service.run(db, pipeline_id, upto=args.get("upto_step_id"), announce=False)
            return _preview_result(service.get(db, pipeline_id), summary), False
        if name == "set_goal":
            return {"goal": service.set_goal(db, pipeline_id, args.get("goal") or "")}, False
        if name == "rename_pipeline":
            new = (args.get("name") or "").strip()[:80]
            if not new:
                raise PipelineError("A name is required.")
            PipelineRepository(db).update(pipeline_id, {"name": new})
            return {"name": new}, False
        return {"error": f"Unknown tool {name}"}, True
    except (PipelineError, KeyError) as e:
        return {"error": str(e)}, True
    except Exception as e:  # noqa: BLE001 — a failing tool is reported to the model, not raised
        log.exception("pipeline tool %s failed", name)
        return {"error": f"{type(e).__name__}: {e}"}, True


# ── The turn ─────────────────────────────────────────────────────────────────

def _history(rows: list[dict]) -> list[dict]:
    return [{"role": r["role"], "content": r["content"]} for r in rows if r["role"] in ("user", "assistant")]


def _repair(db, pipeline_id: int, messages: list[dict]) -> None:
    """
    A turn that died between a tool call and its result leaves a tool_use with
    no tool_result, which the API rejects. Answer those calls as interrupted,
    appended rather than edited out, so the history stays append-only.
    """
    if not messages or messages[-1]["role"] != "assistant":
        return
    pending = [b for b in messages[-1]["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
    if not pending:
        return
    content = [{"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                "content": "Interrupted: the server stopped before this ran. Check the state and retry if needed."}
               for b in pending]
    PipelineMessageRepository(db).add(pipeline_id, "user", content)
    messages.append({"role": "user", "content": content})


def _create(client, **kwargs):
    """Call the model with server-side refusal fallbacks; drop them if this API/account rejects the parameter."""
    try:
        return client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    except anthropic.BadRequestError as e:
        if "fallback" not in str(e).lower():
            raise
        log.warning("refusal fallbacks unavailable, continuing without: %s", e)
        return client.beta.messages.create(**kwargs)


def run_turn(pipeline_id: int, text: str) -> None:
    """One planner message and everything the agent does in reply. Runs in a background thread."""
    db = get_supabase()
    msgs = PipelineMessageRepository(db)
    try:
        p = service.get(db, pipeline_id)
        s = kinds.spec(p["kind"])
        system = SYSTEM.format(kind=s["label"].lower(), tag=STATE_TAG, guidance=s["guidance"],
                               catalog=engine.catalog_text(p["kind"]))
        tools = _tools(p["kind"])
        messages = _history(msgs.for_pipeline(pipeline_id))
        _repair(db, pipeline_id, messages)

        user = [{"type": "text", "text": text},
                {"type": "text", "text": f"<{STATE_TAG}>\n{json.dumps(state_snapshot(p), default=str)}\n</{STATE_TAG}>"}]
        msgs.add(pipeline_id, "user", user)
        messages.append({"role": "user", "content": user})

        client = _get_client()
        for _ in range(MAX_TOOL_ROUNDS):
            resp = _create(client, model=MODEL, max_tokens=16000,
                           system=[{"type": "text", "text": system}], tools=tools, messages=messages,
                           cache_control={"type": "ephemeral"}, output_config={"effort": "medium"})
            content = [b.model_dump(mode="json", exclude_none=True) for b in resp.content]
            msgs.add(pipeline_id, "assistant", content)
            messages.append({"role": "assistant", "content": content})

            if resp.stop_reason == "refusal":
                service.event(db, pipeline_id, "The model declined to continue this request.", level="warn")
                return
            if resp.stop_reason == "max_tokens":
                service.event(db, pipeline_id, "The reply was cut off (too long). Ask it to continue.", level="warn")
                return
            if resp.stop_reason != "tool_use":
                return

            results = []
            for b in resp.content:
                if b.type != "tool_use":
                    continue
                result, is_error = _execute(db, pipeline_id, b.name, b.input or {})
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(result, default=str), **({"is_error": True} if is_error else {})})
            msgs.add(pipeline_id, "user", results)
            messages.append({"role": "user", "content": results})
        service.event(db, pipeline_id, "The agent stopped after many tool calls in one turn. Reply to continue.",
                      level="warn")
    except Exception as e:  # noqa: BLE001 — surfaced in the chat, never lost in a thread
        log.exception("pipeline turn failed for %s", pipeline_id)
        service.event(db, pipeline_id, f"The agent hit an error: {type(e).__name__}: {e}", level="error")
    finally:
        PipelineRepository(db).release(pipeline_id)


# ── Chat as the UI shows it ──────────────────────────────────────────────────

TOOL_LABELS = {"set_schema": "proposed the schema", "add_source": "added a source", "remove_source": "removed a source",
               "read_source": "read a source", "add_step": "proposed a step", "update_step": "changed a step",
               "remove_step": "removed a step", "run_preview": "ran a preview", "rename_pipeline": "renamed the pipeline",
               "set_goal": "recorded the goal"}


def _chip(block: dict) -> dict:
    a = block.get("input") or {}
    detail = {
        "add_step": lambda: f"{a.get('type')}: {a.get('note') or ''}",
        "update_step": lambda: a.get("step_id"),
        "remove_step": lambda: a.get("step_id"),
        "add_source": lambda: a.get("url") or a.get("query"),
        "remove_source": lambda: a.get("source_id"),
        "read_source": lambda: a.get("source_id"),
        "run_preview": lambda: f"up to {a['upto_step_id']}" if a.get("upto_step_id") else "whole pipeline",
        "set_schema": lambda: ", ".join(f.get("name", "") for f in a.get("fields") or []),
        "rename_pipeline": lambda: a.get("name"),
        "set_goal": lambda: a.get("goal"),
    }.get(block.get("name"), lambda: "")()
    return {"id": block.get("id"), "tool": block.get("name"), "label": TOOL_LABELS.get(block.get("name"), block.get("name")),
            "detail": _short(detail or "", 160)}


def display(rows: list[dict]) -> list[dict]:
    """The conversation for the chat pane: text, tool chips (with their error status) and app events."""
    errors = {}
    for r in rows:
        if r["role"] == "user":
            for b in r["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    errors[b["tool_use_id"]] = _short(str(b.get("content")), 300)
    out = []
    for r in rows:
        base = {"id": r["id"], "at": r.get("created_at")}
        if r["role"] == "event":
            out.append({**base, "kind": "event", **r["content"]})
        elif r["role"] == "user":
            texts = [b["text"] for b in r["content"] if isinstance(b, dict) and b.get("type") == "text"
                     and not b["text"].startswith(f"<{STATE_TAG}>")]
            if texts:
                out.append({**base, "kind": "user", "text": "\n\n".join(texts)})
        else:
            text = "\n\n".join(b["text"] for b in r["content"] if b.get("type") == "text" and b.get("text"))
            chips = [{**_chip(b), "error": errors.get(b.get("id"))} for b in r["content"] if b.get("type") == "tool_use"]
            if text or chips:
                out.append({**base, "kind": "assistant", "text": text, "tools": chips})
    return out
