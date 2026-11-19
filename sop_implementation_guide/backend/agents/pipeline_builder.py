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
from backend.pipelines import engine, graph, kinds, service
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
The order is fixed, and the app enforces it: schema, then sources, then steps.
1. Agree the target schema first: the fields every record must have, their types, and which are required.
   Propose it with set_schema, or adapt the one already in the state. The planner approves it in the
   panel; you cannot.
2. Sources: the planner pastes URLs or uploads files (uploads appear in the state as sources). Add pasted
   URLs with add_source; you may suggest sources too. Do not propose steps yet. When the sources are
   complete, the planner marks them final in the panel (sources_status "final" in the state); add_step
   is refused before that. If they seem done but have not, remind them of the "Sources are final" button.
3. Once the sources are final: take a QUICK look at each source with one read_source call (the first few
   thousand characters), enough to see what kind of page or document it is, how items are laid out and
   whether it links to detail pages. Do not read sources end to end; the extract step does that.
   Then propose THE WHOLE PIPELINE at once with add_step, every step it needs to produce every field
   in the schema, each with a one-line note saying why. A funding pipeline is typically: extract, then
   research (programme pages hold the amounts and deadlines), then parse steps for money/date fields
   if the values need it, then a step for each judgement field (geo_tie for geo_scope; llm_fill for
   others). Add dedupe when sources overlap or are read in parts. Do not hold steps back for later:
   the draft's check warns about every field no step on the way to Output produces, and there must be
   no such warning when you finish proposing. Keep the pipeline otherwise short: only the transforms
   the data needs or the planner asks for.
   You PROPOSE changes; the planner makes them. add_step, update_step, remove_step, connect_steps and
   disconnect_steps (and add_source / remove_source once the sources are final) edit YOUR PROPOSAL: a copy
   of the planner's draft, or of the applied steps when no draft is open. The planner sees it on the
   canvas as dashed nodes with a list of changes, and adds it to their draft or discards it; then they
   Check and Apply the draft. Nothing runs or resets until then. While your proposal is pending, further
   edits add to it (the state shows it under proposal). If the state says it is stale, the planner changed
   the steps since: tell them to discard it, and propose again from the current state. The steps
   form a graph: each step takes its inputs' records. add_step appends at the end by default (it takes
   what fed Output and feeds Output itself); `after` puts it right after a step; `inputs` wires it
   explicitly. A step with several inputs merges them, which is how sources that need different reading
   are handled: one Extract step per group of sources, each with its own instructions, then one step
   (e.g. dedupe) taking all of them as inputs. Each source can be read by one Extract step only, a step's
   output goes to one place (no splitting), and every branch into a merge must hand on the same schema:
   the same fields produced and parsed the same way. A merge STACKS records. To ADD fields from one
   branch to another's records (a source that only supplies one field per programme, matched on a key),
   JOIN instead: a sql step with both branches as inputs, whose query reads each input as its own table
   named by its step id (SELECT a.id, b.x AS field FROM t2 a LEFT JOIN t5 b ON ...). Its rows keep the
   ids of the records it returns, so the joined branch's own records drop out; branches into a join may
   have different schemas. When the proposal is done, call check_pipeline (it checks your proposal), fix
   every error it reports, then tell the planner in one or two lines what you proposed and that they can
   add it to the draft from the Agent tab or the canvas (you cannot add or apply it). run_preview runs
   the applied steps, not the draft or your proposal.
   Sources added or removed after they are final also go into your proposal (add_source and remove_source
   do this themselves). A new source is read by no step until you wire it: add an Extract step for it
   (inputs [], config.source_ids [its id]) and merge that branch where the schemas match, or add its id
   to an existing Extract step's source_ids. Extract steps that read "every source" are pinned to the
   sources they read before, so a new source never changes them silently.
   Once applied, the planner reviews the steps one at a time, inputs first: they
   read its prompt in the panel, tune its instructions (the instructions/instruction config; the rest of
   the prompt is fixed), preview its output and approve it. Instruction changes you suggest are proposals
   too (update_step). Write instructions they can read and edit:
   plain sentences about what counts as an item and what to look for. Changing a step's config, or the
   schema, sends that step and every later one back for approval.
   Once every step is approved and the pipeline has run, the planner can override rows of the final
   output (correct values, remove rows, add rows). Those show in the state as planner_overrides, are
   re-applied on every run (values marked "manual"), and are theirs: do not try to undo them with steps.
   The planner also reviews the output record by record: they accept values and flag records for
   follow-up (flagged_records in the state, with their note). A flag is a question for you when they ask:
   look at that record's evidence and propose what would fix it.
4. Preview with run_preview and report what came out honestly: record count, which required fields are
   blank and on how many records, excerpts that could not be verified, warnings.
   Work like an analyst, not an extractor: when fields are blank, first look at where the answers would
   be. Records carry leads (links or documents the source points to), and pages carry links. Propose a
   research step to follow them before asking the planner; turn on its web_search only when the sources
   link nothing useful. Ask the planner only for what research could not find.
SQL, for looking at the data and for transforms:
- run_sql runs a read-only SELECT (DuckDB SQL) over one step's previewed records (table `records`, plus
  `records_text` with the same columns as text) and any reference tables you list. Aggregates and any
  columns are fine; it writes nothing. Use it to answer the planner's questions about the data (counts,
  distributions, what is blank, what a reference table holds) and to check a transform on real rows.
- When a transform is plain logic (a computed field, a filter with several conditions, a CASE rule, a
  lookup against a reference table), a sql step is usually simpler than a chain of parse/map/filter
  steps. Write it on the fly for the data at hand: first test it with run_sql on the output of the step
  it will follow (on_step_id), check as_step in the result says it works as a step, then add it with
  add_step type sql. In a sql step the result must return id (one row per record) and only schema fields;
  only values that change are written, marked derived, and records left out are dropped.
- A CSV source that is a lookup table (tract -> designation, agency -> level, a planner's own list) needs
  no Extract step: list its id in a sql step's config.tables (and run_sql's tables) and join it as a table.
  Its columns, all text, are in the state under source_tables. Its values are exactly the file's, so
  they carry no quote; use this for lookups, and Extract for documents.
- SQL computes from values already in the records, in reference tables or in CSV sources. It is not a way
  to supply facts a source should state, and it never replaces an extract or research step for reading
  documents.
- Show the planner the query you chose in a short code block, and say in one line what it does.

5. Publishing (Load) is the planner's action in the panel, once everything is approved and every required
   field is filled or explicitly accepted as blank. A full run never publishes: it ends "ready for review"
   with what changed since the last publish, and the planner approves it. Each publish becomes a new
   numbered dataset version. The state's load_map shows how the output's fields map onto the catalogue
   (proposed from their names; the planner confirms it on the Output node). Use set_load_mapping when the
   planner says how a field should map.

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
         "description": "Add a source and fetch it now. kind url needs url; kind grants_gov needs a 2-5 word keyword query. "
                        "Once the sources are final it goes into the draft, read by no step until you wire it.",
         "input_schema": {"type": "object", "properties": {
             "kind": {"type": "string", "enum": ["url", "grants_gov"]},
             "url": {"type": "string"}, "query": {"type": "string"}, "label": {"type": "string"}},
             "required": ["kind"]}},
        {"name": "remove_source", "description": "Remove a source.",
         "input_schema": {"type": "object", "properties": {"source_id": {"type": "string"}}, "required": ["source_id"]}},
        {"name": "read_source", "description": "Take a quick look at part of a source's fetched text (up to 4,000 "
                                                "characters; default the first 3,000). One look per source is usually enough.",
         "input_schema": {"type": "object", "properties": {
             "source_id": {"type": "string"}, "offset": {"type": "integer"}, "length": {"type": "integer"}},
             "required": ["source_id"]}},
        {"name": "add_step",
         "description": "Add a step from the catalog to your proposal. By default it goes at the end: it takes what "
                        "fed Output and feeds Output. Returns the proposal's check.",
         "input_schema": {"type": "object", "properties": {
             "type": {"type": "string", "enum": step_types},
             "config": {"type": "object", "description": "As the catalog describes for this type."},
             "note": {"type": "string", "description": "One line: why this step."},
             "after": {"type": "string", "description": "Put it right after this step (it takes that step's place in the wiring)."},
             "inputs": {"type": "array", "items": {"type": "string"},
                        "description": "Wire these steps into it instead (several = a merge). [] for an Extract step "
                                       "that starts a new branch; then connect its output with connect_steps."}},
             "required": ["type", "config", "note"]}},
        {"name": "update_step",
         "description": "Change a step's config or note, in your proposal.",
         "input_schema": {"type": "object", "properties": {
             "step_id": {"type": "string"}, "config": {"type": "object"}, "note": {"type": "string"}},
             "required": ["step_id"]}},
        {"name": "remove_step", "description": "Remove a step in your proposal; a step between two others is bridged.",
         "input_schema": {"type": "object", "properties": {"step_id": {"type": "string"}}, "required": ["step_id"]}},
        {"name": "connect_steps", "description": "In your proposal, wire from_step's output into to_step (a step id, "
                                                  "or 'output'). Several inputs into one step merge.",
         "input_schema": {"type": "object", "properties": {
             "from_step": {"type": "string"}, "to_step": {"type": "string"}}, "required": ["from_step", "to_step"]}},
        {"name": "disconnect_steps", "description": "In your proposal, remove the wire from from_step into to_step (or 'output').",
         "input_schema": {"type": "object", "properties": {
             "from_step": {"type": "string"}, "to_step": {"type": "string"}}, "required": ["from_step", "to_step"]}},
        {"name": "check_pipeline", "description": "Check your proposal: wiring, settings, sources and schemas at merges. "
                                                   "Fix every error before handing it to the planner.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "run_preview",
         "description": "Run the pipeline (or up to a step) on the sources and report records, coverage and warnings.",
         "input_schema": {"type": "object", "properties": {"upto_step_id": {"type": "string"}}}},
        {"name": "run_sql",
         "description": "Run a read-only SELECT (DuckDB SQL) over a step's previewed records, optional reference "
                        "tables and CSV sources, to look at the data or test a transform before adding it as a sql "
                        "step. Writes nothing. Returns columns, up to 50 rows, the row count, and as_step: whether "
                        "the query would work as a sql step and which fields it would write.",
         "input_schema": {"type": "object", "properties": {
             "query": {"type": "string", "description": "One SELECT; the table `records` is the step's output."},
             "tables": {"type": "array", "items": {"type": "string"},
                        "description": "Reference tables (" + ", ".join(engine.REF_TABLES) + ") or CSV source ids the query joins."},
             "on_step_id": {"type": "string", "description": "Whose output is `records`: the step a new sql step "
                            "would follow. Default the last previewed step; \"output\" for the last run's final records."},
             "input_step_ids": {"type": "array", "items": {"type": "string"},
                                "description": "Instead of on_step_id, for a join: the steps a sql step with several "
                                               "inputs would take. `records` holds all of them, and each is also its own "
                                               "table named by its step id."},
             "limit": {"type": "integer", "description": "Rows to return, at most 50 (default 20)."}},
             "required": ["query"]}},
        {"name": "set_load_mapping",
         "description": "Set which output field Load writes to each catalogue column: {column: field, or null for "
                        "none}. Columns left out keep their mapping. Resets no step.",
         "input_schema": {"type": "object", "properties": {
             "mapping": {"type": "object", "additionalProperties": {"type": ["string", "null"]}}},
             "required": ["mapping"]}},
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
        "sources_status": d.get("sources_status") or "open",
        "sources": [{**{k: s.get(k) for k in ("id", "kind", "url", "query", "label")},
                     "fetch": {k: (st.get("sources") or {}).get(s["id"], {}).get(k)
                               for k in ("chars", "truncated", "error")}} for s in d["sources"]],
        "steps": [{**{k: s.get(k) for k in ("id", "type", "config", "note", "status")}, "inputs": s["inputs"]}
                  for s in graph.normalise(d)["steps"]],
        "output_from": graph.normalise(d)["output_from"],
        "draft": ({"steps": [{k: s.get(k) for k in ("id", "type", "config", "note", "inputs")} for s in d["draft"]["steps"]],
                   "output_from": d["draft"]["output_from"],
                   **({"sources": [{k: x.get(k) for k in ("id", "kind", "url", "query", "label")} for x in d["draft"]["sources"]]}
                      if d["draft"].get("sources") is not None else {}),
                   "check": {k: [x["text"] for x in v] for k, v in service.check_draft(p).items() if k in ("errors", "warnings")}}
                  if d.get("draft") is not None else None),
        "source_tables": {k: list(v["columns"]) for k, v in service._source_tables(p).items()},
        "load_map": ({"columns": {c["column"]: c["field"] for c in lm["columns"]}, "unmapped_fields": lm["extras"],
                      "notes": lm["notes"]} if (lm := service.view_load_map(p)) else None),
        "proposal": ({"changes": [c["text"] for c in pv["changes"]], "stale": pv["stale"],
                      "check": {k: [x["text"] for x in v] for k, v in pv["check"].items() if k in ("errors", "warnings")}}
                     if (pv := service._proposal_view(p)) else None),
        "flagged_records": {rid: m["flag"].get("note") or "" for rid, m in service.review_marks(p).items() if m.get("flag")},
        "planner_overrides": d.get("overrides"),
        "design_gaps": engine.design_gaps(d),
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

def _check_result(db, pipeline_id: int) -> dict:
    with service.proposing():
        p = service.get(db, pipeline_id)
        if p["definition"].get("draft") is None:
            return {"proposal": "none yet"}
        r = service.check_draft(p)
    return {"ok": r["ok"], "errors": [e["text"] for e in r["errors"]], "warnings": [w["text"] for w in r["warnings"]],
            "next": "fix the errors" if r["errors"] else "the planner can add your proposal to the draft, then Check and Apply"}


# Tools that change the steps (or sources once final) edit the agent's proposal, never the draft.
PROPOSING = {"add_step", "update_step", "remove_step", "connect_steps", "disconnect_steps", "add_source", "remove_source"}


def _execute(db, pipeline_id: int, name: str, args: dict) -> tuple[dict, bool]:
    """Run one tool. Returns (result, is_error)."""
    if name in PROPOSING:
        with service.proposing():
            return _run_tool(db, pipeline_id, name, args)
    return _run_tool(db, pipeline_id, name, args)


def _run_tool(db, pipeline_id: int, name: str, args: dict) -> tuple[dict, bool]:
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
            return service.read_source(db, pipeline_id, args["source_id"], args.get("offset") or 0, args.get("length") or 3000), False
        if name == "add_step":
            step = service.draft_add_step(db, pipeline_id, args["type"], args.get("config") or {}, args.get("note"),
                                          after=args.get("after"), inputs=args.get("inputs"))
            return {"step": step, "status": "added to your proposal", "check": _check_result(db, pipeline_id)}, False
        if name == "update_step":
            step = service.draft_update_step(db, pipeline_id, args["step_id"], args.get("config"), args.get("note"))
            return {"step": step, "status": "changed in your proposal", "check": _check_result(db, pipeline_id)}, False
        if name == "remove_step":
            service.draft_remove_step(db, pipeline_id, args["step_id"])
            return {"removed": args["step_id"], "check": _check_result(db, pipeline_id)}, False
        if name == "connect_steps":
            service.draft_link(db, pipeline_id, args["from_step"], args["to_step"])
            return {"connected": f"{args['from_step']} -> {args['to_step']}", "check": _check_result(db, pipeline_id)}, False
        if name == "disconnect_steps":
            service.draft_unlink(db, pipeline_id, args["from_step"], args["to_step"])
            return {"disconnected": f"{args['from_step']} -> {args['to_step']}", "check": _check_result(db, pipeline_id)}, False
        if name == "check_pipeline":
            return _check_result(db, pipeline_id), False
        if name == "run_preview":
            summary = service.run(db, pipeline_id, upto=args.get("upto_step_id"), announce=False)
            return _preview_result(service.get(db, pipeline_id), summary), False
        if name == "run_sql":
            r = service.explore_sql(db, pipeline_id, args.get("query") or "", args.get("tables"),
                                    args.get("on_step_id"), args.get("limit") or 20, args.get("input_step_ids"))
            r["rows"] = [[_short(v) for v in row] for row in r["rows"]]
            return r, False
        if name == "set_load_mapping":
            m = service.set_load_map(db, pipeline_id, args.get("mapping") or {})
            return {"columns": {c["column"]: c["field"] for c in m["columns"]}, "unmapped_fields": m["extras"]}, False
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
               "read_source": "read a source", "add_step": "drafted a step", "update_step": "changed a step",
               "remove_step": "removed a step", "run_preview": "ran a preview", "rename_pipeline": "renamed the pipeline",
               "set_goal": "recorded the goal", "run_sql": "ran SQL", "connect_steps": "wired steps",
               "disconnect_steps": "unwired steps", "check_pipeline": "checked the draft",
               "set_load_mapping": "mapped fields for Load"}


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
        "run_sql": lambda: " ".join((a.get("query") or "").split()),
        "connect_steps": lambda: f"{a.get('from_step')} → {a.get('to_step')}",
        "disconnect_steps": lambda: f"{a.get('from_step')} ✕ {a.get('to_step')}",
        "set_load_mapping": lambda: ", ".join(f"{v or 'nothing'} → {k}" for k, v in (a.get("mapping") or {}).items()),
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
