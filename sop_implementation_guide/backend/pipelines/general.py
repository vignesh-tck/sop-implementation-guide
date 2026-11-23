"""
General pipelines: any schema and any instructions, from documents (web pages,
PDFs, CSVs, JSON, text) and from our own Supabase tables, written into tables
of their own.

Because nothing about the output is fixed, the safety rails sit where the data
comes in and where it goes out:

    in   Extract only extracts. Its prompt (GENERAL_EXTRACT_PROMPT) does not
         let the planner's instructions ask for inference or calculation, and
         a value is kept only when it is in its own quote and the quote is in
         the source (engine.EXTRACT_ONLY, engine.in_quote). Anything else is
         blanked, and the claim kept on the record under "rejected". Judgement
         and conversion are steps of their own (llm_fill, parse, sql), marked
         inferred or derived.
    out  A general pipeline writes only to tables named gp_<name>, which it
         creates itself. The app's tables (funding_programs, zoning_signals,
         blocks ...) are read-only to it. Writing goes through one database
         function, gp_publish (MIGRATION), so every output of a publish is
         written in one transaction: the table is created or given its new
         columns, and the rows are merged by key (or this pipeline's rows
         replaced). Each publish is also snapshotted as the next General
         dataset version.

Reading: every table the API exposes, except the pipelines' own bookkeeping,
is available as db.<table>, both to SQL steps (joined as a reference table) and
to the read_table step (one record per row). Tables are listed from the
PostgREST schema, so a gp_ table appears once it exists.

Importing this module registers the read_table step, the extract-only prompt
and the table provider with the engine (kinds.py imports it).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from postgrest.exceptions import APIError
from supabase import Client

from backend.config import settings
from backend.pipelines import engine
from backend.pipelines.engine import PipelineError
from backend.pipelines.zoning_steps import _columns_map
from backend.repositories.datasets import DatasetVersionRepository
from backend.tools import datasets

log = logging.getLogger(__name__)

KIND = "general"
PAGE = 1000                  # PostgREST returns at most this many rows per request
TABLES_TTL = 60              # seconds the table list is reused before it is read again
# The pipelines' own bookkeeping (big JSON state and chat) and the version snapshots are not data to build on.
HIDDEN = {"pipelines", "pipeline_messages", "pipeline_runs", "dataset_records"}
TABLE_RE = re.compile(r"^gp_[a-z][a-z0-9_]{0,50}$")
MODES = ("merge", "replace")

MIGRATION_FILE = "backend/db/schema.sql (the GENERAL PIPELINES section)"
MIGRATION = ("General pipelines need a one-time change in Supabase: run the GENERAL PIPELINES section of "
             f"{MIGRATION_FILE} in the Supabase SQL editor (it allows the 'general' kind and adds the gp_publish "
             "function that creates and writes gp_ tables), then try again.")


# ── Our tables, read live ────────────────────────────────────────────────────

_NUMERIC = {"bigint", "integer", "smallint", "numeric", "double precision", "real"}
_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "tables": {}}


def _duck_type(fmt: str | None) -> str:
    if fmt in _NUMERIC:
        return "DOUBLE"
    if fmt == "boolean":
        return "BOOLEAN"
    if fmt == "date":
        return "DATE"
    return "VARCHAR"


def tables(fresh: bool = False) -> dict[str, dict]:
    """
    Every readable table: {name: {columns: {column: DuckDB type}, formats:
    {column: Postgres type}, key: [primary key columns]}}, from the PostgREST
    schema. Reused for TABLES_TTL seconds; on a failed read the last list stands.
    """
    with _lock:
        if not fresh and _cache["at"] and time.monotonic() - _cache["at"] < TABLES_TTL:
            return _cache["tables"]
    key = settings.supabase_service_role_key
    try:
        resp = httpx.get(settings.supabase_url.rstrip("/") + "/rest/v1/", timeout=10.0,
                         headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/openapi+json"})
        resp.raise_for_status()
        spec = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("could not list Supabase tables: %s", e)
        with _lock:
            _cache["at"] = time.monotonic()   # try again after TABLES_TTL, not on every call
        return _cache["tables"]
    out = {}
    for name, d in sorted((spec.get("definitions") or {}).items()):
        if name in HIDDEN or not re.match(r"^[a-z_][a-z0-9_]*$", name):
            continue
        props = d.get("properties") or {}
        out[name] = {"columns": {c: _duck_type(p.get("format")) for c, p in props.items()},
                     "formats": {c: p.get("format") for c, p in props.items()},
                     "key": [c for c, p in props.items() if "<pk/>" in (p.get("description") or "")]}
    with _lock:
        _cache.update(at=time.monotonic(), tables=out)
    _register(out)
    return out


def _rows(db: Client, name: str) -> list[dict]:
    """A table's rows, every column, in key order, up to the SQL reference limit."""
    t = tables().get(name) or {}
    order = (t.get("key") or list(t.get("columns") or {})[:1] or [None])[0]
    out, start = [], 0
    while len(out) < engine.SQL_REF_ROWS:
        q = db.table(name).select("*")
        if order:
            q = q.order(order)
        page = q.range(start, start + PAGE - 1).execute().data or []
        out += page
        if len(page) < PAGE:
            break
        start += PAGE
    return out[:engine.SQL_REF_ROWS]


def _register(found: dict[str, dict]) -> None:
    """Make every table a reference table, db.<name>, for general pipelines; drop ones that are gone."""
    for t in [t for t in engine.REF_TABLES if t.startswith(engine.DB_PREFIX) and t[len(engine.DB_PREFIX):] not in found]:
        engine.REF_TABLES.pop(t, None)
    for name, t in found.items():
        own = name.startswith("gp_")
        engine.REF_TABLES[engine.DB_PREFIX + name] = {
            "description": f"Supabase table {name}{' (written by a general pipeline)' if own else ''}, read live and "
                           "read only.",
            "columns": t["columns"], "rows": lambda db, n=name: _rows(db, n), "kinds": [KIND]}


engine.REF_PROVIDERS[KIND] = lambda: tables()


# ── The read_table step ──────────────────────────────────────────────────────

def _table(name: Any) -> dict:
    name = str(name or "").strip()
    if name.startswith(engine.DB_PREFIX):
        name = name[len(engine.DB_PREFIX):]
    found = tables()
    if name not in found:
        found = tables(fresh=True)
    if name not in found:
        raise PipelineError(f"read_table.table must be one of our tables: {', '.join(found) or '(none could be listed)'}.")
    return {"name": name, **found[name]}


def _csv_source(sources: list[dict] | None, sid: Any) -> dict:
    src = next((s for s in sources or [] if s["id"] == sid), None)
    if not src:
        raise PipelineError(f"read_table.source_id must name a CSV source of this pipeline ({sid!r} is not one).")
    return src


def _read_validate(c: dict, fields: list[dict], sources: list[dict] | None) -> dict:
    if c.get("source_id"):   # a CSV source, read as a table: no model, one record per row
        src = _csv_source(sources, c["source_id"])
        cols = engine.source_columns(src)
        if cols is None:
            raise PipelineError(f"{src['id']} is not a CSV, or could not be read, so it cannot be read as a table: "
                                "read it with an Extract step.")
        c["table"] = None
        c["fields"] = _columns_map(c.get("fields"), list(cols), fields, "read_table")
        return c
    c.pop("source_id", None)
    t = _table(c.get("table"))
    c["table"] = t["name"]
    c["fields"] = _columns_map(c.get("fields"), list(t["columns"]), fields, "read_table")
    return c


def _read_io(step: dict, fields: list[dict]) -> dict:
    c = step.get("config") or {}
    return {"rows": "creates", "writes": list((c.get("fields") or {}).keys()),
            "reads": c.get("source_id") or f"table {c.get('table')}"}


_BOOL = {"yes": True, "y": True, "true": True, "t": True, "1": True, "no": False, "n": False, "false": False, "f": False, "0": False}


def _cell(f: dict, v: str | None) -> tuple[Any, bool]:
    """A CSV cell (always text) as the field's type. (value, read): read is False when it does not fit the type."""
    v = (v or "").strip()
    if not v:
        return None, True
    if f["type"] in engine.PARSERS:
        value = engine.PARSERS[f["type"]](v)
    elif f["type"] == "boolean":
        value = _BOOL.get(v.lower())
    elif f["type"] == "url":
        value = v if v.lower().startswith(("http://", "https://")) else None
    else:
        value, warn = engine._normalise(f, v)
        return value, warn is None
    return value, value is not None


def _read_csv(step: dict, records: list[dict], fields: list[dict], ctx: dict) -> tuple[list[dict], list[str]]:
    c = step["config"]
    src = _csv_source(ctx["sources"], c["source_id"])
    label = src.get("label") or src["id"]
    if ctx.get("say"):
        ctx["say"](f"Reading {label} as a table")
    doc = ctx["source_doc"](src["id"])
    columns, body, notes = engine.source_table(doc)
    names = list(columns)
    by_name = {f["name"]: f for f in fields}
    new, unread = [], {}
    for n, row in enumerate(body, 1):
        rec = {"id": f"{src['id']}-row{n}", "values": {}, "prov": {},
               "source_id": src["id"], "source_url": doc.get("final_url") or src.get("url")}
        for field, col in c["fields"].items():
            cell = row[names.index(col)]
            v, ok = _cell(by_name[field], cell)
            if not ok:
                unread.setdefault(field, []).append(n)
                v = None
            engine._set(rec, field, v, "derived", step["id"], note=f"{label}, row {n}, column {col}"
                        + ("" if ok else f"; {cell!r} is not a {by_name[field]['type']}, so left blank"))
        new.append(rec)
    warnings = [f"{len(new)} row(s) from {label}"] + [f"{label}: {x}" for x in notes]
    for field, rows in unread.items():
        warnings.append(f"{len(rows)} value(s) in column {c['fields'][field]} are not a {by_name[field]['type']} and were "
                        f"left blank in {field} (rows {', '.join(map(str, rows[:6]))}{' …' if len(rows) > 6 else ''}): "
                        "change the field's type, or convert the column in a SQL step.")
    return records + new, warnings


def _read_run(step: dict, records: list[dict], fields: list[dict], ctx: dict) -> tuple[list[dict], list[str]]:
    c = step["config"]
    if c.get("source_id"):
        return _read_csv(step, records, fields, ctx)
    t = _table(c["table"])
    if ctx.get("say"):
        ctx["say"](f"Reading table {t['name']}")
    rows = ctx["refs"](engine.DB_PREFIX + t["name"])
    by_name = {f["name"]: f for f in fields}
    new, warnings = [], []
    for n, row in enumerate(rows, 1):
        key = "-".join(str(row.get(k)) for k in t["key"]) if t["key"] else str(n)
        rec = {"id": f"{t['name']}-{re.sub(r'[^A-Za-z0-9]+', '-', key).strip('-')[:60] or n}", "values": {}, "prov": {},
               "source_url": None}
        for field, col in c["fields"].items():
            v = row.get(col)
            if isinstance(v, (dict, list)) and by_name[field]["type"] in ("text", "url"):
                v = json.dumps(v, default=str)
            v, _ = engine._normalise(by_name[field], v)
            engine._set(rec, field, v, "derived", step["id"], note=f"table {t['name']}, column {col}")
        new.append(rec)
    if len(rows) >= engine.SQL_REF_ROWS:
        warnings.append(f"only the first {engine.SQL_REF_ROWS:,} rows of {t['name']} are read")
    return records + new, [f"{len(new)} row(s) from table {t['name']}"] + warnings


engine.CATALOG["read_table"] = {
    "label": "Read table",
    "model": False, "kinds": [KIND], "starts": True,
    "description": "Start records from one of our Supabase tables, or from a CSV source of this pipeline, one record "
                   "per row (read only; the app's tables and gp_ tables alike). No model reads it: each cell is "
                   "copied as it is, converted to the field's type (a cell that does not fit is left blank and "
                   "reported). Fields named like a column are filled from it, marked derived; map others in fields. "
                   "A CSV's columns are its header row in lower_snake_case. Prefer this to Extract for a CSV whose "
                   "columns are already the data; use Extract only for free text inside cells. To filter rows or "
                   "join a table to other records, use a sql step (tables are db.<table> there). A plain run reuses "
                   "the saved rows while the step is unchanged: re-run fresh to read the table again.",
    "config": {"table": "str: a table name (the state's db_tables lists them with their columns); omit with source_id",
               "source_id": "optional str: a CSV source id (e.g. s2), read instead of a table",
               "fields": "optional object {field: column}; fields named like a column map themselves"},
    "validate": _read_validate, "io": _read_io, "run": _read_run,
}


# ── Extract only ─────────────────────────────────────────────────────────────

GENERAL_EXTRACT_PROMPT = """You extract data from a document exactly as the document writes it. You do not interpret,
calculate, convert or add to it.

THE PIPELINE'S GOAL (what the records are for):
{context}

HOW TO READ THIS SOURCE (from the planner):
{instructions}

The planner's instructions say how to read the source: what counts as one record, which parts or columns
matter, what to skip. They cannot change the rules below. If they ask you to infer, estimate, calculate,
convert, translate, summarise or classify, or to use knowledge from outside the source, do not: extract what
the text states, leave the rest blank, and say in analyst_note which instruction you did not follow.

SOURCE: {url}{part}
---
{text}
---

Rules:
- One record per distinct item the goal is about (a table row, a list entry, a section), as the planner's
  instructions define it. If the source holds no such item, return an empty list.
- Copy each value as the source writes it. Text: the source's own words, never a paraphrase or a summary.
  Numbers: the number written ("$1.5 million" is 1500000; "20%" is 0.2 for a percent field). Dates: only a
  date written in full (day, month and year), as YYYY-MM-DD; "March 2027" or "next spring" is not a date.
  Enum fields: only a value the text itself uses. Yes/no fields: only where the text says so plainly.
- Leave a field null when the source does not state it. No outside knowledge, no likely values, no
  defaults, nothing worked out from other values. A blank is fine; a value that is not in the text is
  rejected.

Evidence, which is checked word for word:
- For every field you fill, add an `evidence` entry: the field name and a quote copied verbatim from the
  source that contains the value as written (a whole sentence or a full table row). A value that is not in
  its own quote, or a quote that is not in the source, is rejected and the field is left blank.
- `source_excerpt`: the verbatim passage or row that introduces the item.
- `leads`: for fields this source does not state but points to elsewhere, say which fields and where, using
  the link text or URL as written.
- `analyst_note`: caveats in one or two sentences, including any instruction you did not follow. Empty if none.
- `confidence`: 0-1, how well the source supports the record as a whole."""

engine.EXTRACT_ONLY[KIND] = GENERAL_EXTRACT_PROMPT


# ── Where an output is written ───────────────────────────────────────────────
#
# Each output keeps definition["write"] = {table, mode, key}. Until the planner
# sets it, the table is named after the pipeline (and the output, for a second
# one), the mode is merge and the key is the first field.

PG_TYPES = {"text": "text", "url": "text", "enum": "text", "number": "double precision", "money": "double precision",
            "percent": "double precision", "date": "date", "boolean": "boolean"}
# Existing column types a field's values can be written into.
FITS = {"text": {"text", "character varying", "character"},
        "double precision": _NUMERIC,
        "date": {"date"},
        "boolean": {"boolean"}}
SYSTEM_COLUMNS = {"_key": "text", "_pipeline_id": "bigint", "_record_id": "text", "_source_url": "text",
                  "_evidence": "jsonb", "_published_at": "timestamp with time zone"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def default_table(pipeline_name: str, pipeline_id: int, oid: str, output_name: str | None) -> str:
    base = _slug(pipeline_name)[:36] or f"pipeline_{pipeline_id}"
    if base[0].isdigit():
        base = f"p_{base}"
    if oid != "output":
        base += "_" + (_slug(output_name or "")[:14] or oid)
    return f"gp_{base}"[:53]


def resolve_write(saved: dict | None, fields: list[dict], pipeline: dict, oid: str, output_name: str | None) -> dict:
    """The write settings in force: the saved ones, else the defaults. saved: true when the planner set them."""
    w = saved or {}
    names = [f["name"] for f in fields]
    key = [k for k in w.get("key") or [] if k in names] or names[:1]
    return {"table": w.get("table") or default_table(pipeline["name"], pipeline["id"], oid, output_name),
            "mode": w.get("mode") if w.get("mode") in MODES else "merge", "key": key, "saved": bool(saved)}


def clean_write(table: str | None, mode: str | None, key: list[str] | None, fields: list[dict], current: dict) -> dict:
    """A planner's write settings, checked. Raises PipelineError."""
    out = {k: current[k] for k in ("table", "mode", "key")}
    if table is not None:
        t = (table or "").strip().lower()
        t = t if t.startswith("gp_") else f"gp_{_slug(t)}"
        if not TABLE_RE.match(t):
            raise PipelineError("A general pipeline writes to a table named gp_ plus lower_snake_case (e.g. gp_bus_stops), "
                                "at most 53 characters.")
        out["table"] = t
    if mode is not None:
        if mode not in MODES:
            raise PipelineError("mode is merge (rows are matched by key; others are kept) or replace (this pipeline's rows "
                                "are replaced by the run).")
        out["mode"] = mode
    if key is not None:
        names = [f["name"] for f in fields]
        bad = [k for k in key if k not in names]
        if bad or not key:
            raise PipelineError(f"key must list schema fields ({', '.join(names)}); {', '.join(bad) or 'none given'}.")
        out["key"] = list(dict.fromkeys(key))
    return out


def record_key(rec: dict, key: list[str]) -> str | None:
    """A record's key in the table: its key fields' values, case and spacing aside. None when one is blank."""
    vals = [rec["values"].get(k) for k in key]
    if any(engine._blank(v) for v in vals):
        return None
    vals = [int(v) if isinstance(v, float) and v.is_integer() else v for v in vals]   # 12.0 and 12 are one key
    return " | ".join(" ".join(str(v).split()).lower() for v in vals)


def _pg_value(f: dict, v: Any) -> Any:
    if engine._blank(v):
        return None
    t = PG_TYPES[f["type"]]
    if t == "double precision":
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        return engine.PARSERS.get(f["type"], engine.parse_number)(v)
    if t == "date":
        return engine.parse_date(v)
    if t == "boolean":
        return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "y", "1")
    return json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)


def _evidence(rec: dict) -> dict:
    out = {"fields": {k: {x: (str(p[x])[:500] if x == "quote" else p[x]) for x in ("kind", "quote", "verified", "source_url", "note")
                          if p.get(x) is not None} for k, p in (rec.get("prov") or {}).items()}}
    if rec.get("excerpt"):
        out["excerpt"] = str(rec["excerpt"])[:1000]
    if rec.get("rejected"):
        out["rejected"] = rec["rejected"]
    return out


def rows(pipeline: dict, part: dict, now: str) -> list[dict]:
    """The output's records as the table's rows."""
    w, fields = part["write"], part["fields"]
    out = []
    for rec in part["records"]:
        row = {f["name"]: _pg_value(f, rec["values"].get(f["name"])) for f in fields}
        row.update(_key=record_key(rec, w["key"]), _pipeline_id=pipeline["id"], _record_id=rec["id"],
                   _source_url=rec.get("source_url"), _evidence=_evidence(rec), _published_at=now)
        out.append(row)
    return out


def table_plan(write: dict, fields: list[dict]) -> dict:
    """What publishing does to the table: create it or add columns, and which existing columns do not fit."""
    t = tables().get(write["table"])
    cols, conflicts = [], []
    for f in fields:
        want = PG_TYPES[f["type"]]
        have = (t or {}).get("formats", {}).get(f["name"])
        state = "new" if have is None else "exists" if have in FITS[want] else "conflict"
        cols.append({"name": f["name"], "type": want, "state": state, **({"existing": have} if have else {})})
        if state == "conflict":
            conflicts.append(f"Column `{f['name']}` in {write['table']} is {have}, but the field is {f['type']} "
                             f"(written as {want}): change the field's type, rename the field, or write to another table.")
    return {"exists": t is not None, "columns": cols, "conflicts": conflicts,
            "other_columns": [c for c in (t or {}).get("columns", {}) if c not in SYSTEM_COLUMNS
                              and c not in {f["name"] for f in fields}]}


def write_problems(write: dict, fields: list[dict], records: list[dict] | None = None,
                   taken: list[str] | None = None) -> list[str]:
    """Why this output cannot be written as set up (and with the last run's records). Empty when it can."""
    out = []
    if not TABLE_RE.match(write["table"]):
        out.append(f"{write['table']} is not a table a general pipeline can write: name it gp_ plus lower_snake_case.")
    if write["table"] in (taken or []):
        out.append(f"Another output of this pipeline writes {write['table']} too: give each output its own table.")
    if not fields:
        return out
    out += table_plan(write, fields)["conflicts"]
    if records:
        keys = [record_key(r, write["key"]) for r in records]
        blank = sum(1 for k in keys if k is None)
        if blank:
            out.append(f"{blank} record(s) have no {' + '.join(f'`{k}`' for k in write['key'])}, which is the table's key: "
                       "fill it, remove those records, or choose another key under Writes to.")
        dup = sorted({k for k in keys if k is not None and keys.count(k) > 1})
        if dup:
            out.append(f"{len(dup)} key value(s) appear on more than one record ({'; '.join(dup[:4])}"
                       f"{' …' if len(dup) > 4 else ''}): add a dedupe step on {', '.join(write['key'])}, or choose a key "
                       "that tells them apart.")
    return out


def write_view(write: dict, fields: list[dict], records: list[dict] | None, taken: list[str]) -> dict:
    """The Output node's "Writes to" panel."""
    plan = table_plan(write, fields)
    return {**write, **plan, "problems": write_problems(write, fields, records, taken),
            "modes": {"merge": "Rows are matched by key and updated; rows not in the run are kept.",
                      "replace": "This pipeline's rows in the table are replaced by the run; other rows are kept."}}


# ── Diff and publish ─────────────────────────────────────────────────────────

def _existing(db: Client, table: str, columns: list[str]) -> list[dict]:
    have = tables().get(table)
    if not have:
        return []
    cols = ["_key", "_pipeline_id"] + [c for c in columns if c in have["columns"]]
    out, start = [], 0
    while True:
        page = db.table(table).select(",".join(cols)).order("_key").range(start, start + PAGE - 1).execute().data or []
        out += page
        if len(page) < PAGE:
            return out
        start += PAGE


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) < 1e-9
    return (a if a is not None else "") == (b if b is not None else "") or str(a) == str(b)


def diff(db: Client, pipeline: dict, parts: list[dict]) -> dict:
    """What publishing would do to each table."""
    tables(fresh=True)
    now = datetime.now(timezone.utc).isoformat()
    problems, outs = [], []
    taken: list[str] = []
    for part in parts:
        w, fields = part["write"], part["fields"]
        problems += [f"{part['name']}: {p}" for p in write_problems(w, fields, part["records"], taken)]
        taken.append(w["table"])
        new = {r["_key"]: r for r in rows(pipeline, part, now) if r["_key"] is not None}
        current = {r["_key"]: r for r in _existing(db, w["table"], [f["name"] for f in fields])}
        added, changed, unchanged = [], [], 0
        for k, r in new.items():
            old = current.get(k)
            if not old:
                added.append(k)
                continue
            diffs = [f["name"] for f in fields if f["name"] in old and not _same(old[f["name"]], r[f["name"]])]
            if diffs:
                changed.append({"key": k, "fields": diffs})
            else:
                unchanged += 1
        removed = ([k for k, r in current.items() if r.get("_pipeline_id") == pipeline["id"] and k not in new]
                   if w["mode"] == "replace" else [])
        plan = table_plan(w, fields)
        outs.append({"output": part["output"], "name": part["name"], "table": w["table"], "mode": w["mode"],
                     "key": w["key"], "exists": plan["exists"],
                     "new_columns": [c["name"] for c in plan["columns"] if c["state"] == "new"],
                     "rows_now": len(current), "added": added[:200], "added_count": len(added),
                     "changed": changed[:200], "changed_count": len(changed), "unchanged": unchanged,
                     "removed": removed[:200], "removed_count": len(removed),
                     "kept": len(current) - len(removed) - len(changed) - unchanged})
    latest = datasets.resolve(db, KIND)
    return {"tables": outs, "problems": problems, "base_version": datasets.label(latest),
            "note": "Merge updates rows with the same key and keeps the rest; replace swaps this pipeline's rows for "
                    "the run's. Rows other pipelines wrote are never removed."}


def _missing_function(e: APIError) -> bool:
    text = f"{getattr(e, 'code', '')} {getattr(e, 'message', '')} {e}"
    return "PGRST202" in text or ("gp_publish" in text and "find" in text.lower())


def load(db: Client, pipeline: dict, parts: list[dict], name: str | None, created_by: str | None,
         definition_version: int) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    problems, taken, writes, records = [], [], [], []
    tables(fresh=True)
    for part in parts:
        w, fields = part["write"], part["fields"]
        problems += [f"{part['name']}: {p}" for p in write_problems(w, fields, part["records"], taken)]
        taken.append(w["table"])
        rs = rows(pipeline, part, now)
        writes.append({"table": w["table"], "replace": w["mode"] == "replace",
                       "columns": [{"name": f["name"], "type": PG_TYPES[f["type"]]} for f in fields], "rows": rs})
        records += [{"record_type": w["table"], "record_key": r["_key"], "data": r} for r in rs]
    if problems:
        raise PipelineError("Not published: " + " ".join(problems[:6]))
    note = f"Pipeline “{pipeline['name']}” def v{definition_version}"
    # The version first: it fails at once if the 'general' kind is not allowed yet, before anything is written.
    try:
        version = datasets._create_version(
            db, KIND, records, {"tables": {w["table"]: len(w["rows"]) for w in writes}}, name=name, note=note,
            source={"type": "pipeline", "pipeline_id": pipeline["id"], "definition_version": definition_version,
                    "tables": [w["table"] for w in writes]},
            created_by=created_by)
    except APIError as e:
        if getattr(e, "code", None) == "23514":
            raise PipelineError(MIGRATION) from e
        raise
    try:
        result = db.rpc("gp_publish", {"p_writes": writes, "p_pipeline_id": pipeline["id"]}).execute().data or []
    except APIError as e:
        DatasetVersionRepository(db).delete(version["id"])
        if _missing_function(e):
            raise PipelineError(MIGRATION) from e
        raise PipelineError(f"Supabase refused the write, so nothing was published: {getattr(e, 'message', None) or e}") from e
    except Exception:
        DatasetVersionRepository(db).delete(version["id"])
        raise
    tables(fresh=True)
    done = "; ".join(f"{r.get('table')}: {r.get('written')} row(s)"
                     + (" (new table)" if r.get("created") else "")
                     + (f", {r.get('deleted')} replaced" if r.get("deleted") else "")
                     + (f", new columns {', '.join(r.get('added_columns') or [])}" if r.get("added_columns") and not r.get("created") else "")
                     for r in result if isinstance(r, dict))
    return {"written": sum(len(w["rows"]) for w in writes), "dataset_version": version, "tables": result,
            "note": f"Written to {done}." if done else ""}


# ── What the agent is told ───────────────────────────────────────────────────

GUIDANCE = """This is a GENERAL pipeline. Its schema, its fields and the planner's instructions are free: there is
no catalogue to load into and no default fields. Agree the fields with the planner from what they need.

Sources: web pages, PDFs, CSVs, JSON and text files (paste URLs; uploads arrive as sources), and our own
Supabase tables, listed in the state under db_tables with their columns. A pipeline may use tables only, with
no sources at all: then the planner can mark the (empty) sources final straight away.
- read_table starts records from a table, one per row (fields named like a column fill themselves).
  With config.source_id it reads a CSV source instead (columns under source_tables in the state): use
  this, not Extract, when the CSV's columns already are the data, since every cell is copied with no
  model. Use Extract on a CSV only to read free text inside its cells, and give that Extract step
  source_ids that leave out a CSV read_table reads, or its rows come in twice.
- A sql step can join any table as a reference table named db.<table> (list it in config.tables, e.g.
  "db.blocks", and write FROM records r JOIN db.blocks b ON ...). run_sql takes the same names.
- Tables are read only. A gp_ table one of the general pipelines wrote can be read like any other.

Extract only extracts, and the app enforces it: a value is kept only if it appears in its own quote and the
quote appears word for word in the source; anything else is blanked and listed as rejected on the record.
Text is copied, not paraphrased; a number or date must be written in the source. So:
- Write Extract instructions about WHAT to read (what counts as one record, which sections, columns or
  pages, what to skip), never about computing, converting, classifying or summarising. Those instructions
  are ignored, and the values they produce are rejected.
- Put every transformation in a step of its own: parse for text to number or date, sql for computed fields,
  joins and rules, map_values for recodes, llm_fill for judgement (marked inferred). A field the source
  cannot state but the planner wants judged is extract=false and filled by llm_fill.
- When preview warnings list rejected values, say how many and why, and propose the step that would
  produce the value properly, or ask for a source that states it.
- A CSV whose rows are already the records can be read by Extract (one record per row, each row its
  own quote) or joined as a table in sql with no model at all; prefer the table when no reading is needed.

Publishing writes each output to a table of its own in Supabase, named gp_<name>, never to the app's tables.
The state's write (per output) shows the table, the mode (merge: matched by key, others kept; replace: this
pipeline's rows replaced), the key fields, whether the table exists, which columns would be added, and any
problem. The table is created, and new fields added as columns, when the planner publishes. Use set_write
when the planner names a table, a mode or a key. The key must be filled and unique on every record, so a
dedupe step on the key fields is usually needed. A general pipeline can have several outputs, each its own
table: add_output adds one (target table)."""

INTRO = (
    "Let's build a general pipeline. Tell me:\n"
    "1. **The goal**: what each record should be and what it is for.\n"
    "2. **The fields** you need for each record, with their types.\n"
    "3. **Sources**: paste URLs, or upload PDFs, CSVs, JSON or text files. I can also read our Supabase tables "
    "(blocks, recommendations, funding programmes and the rest) and join them in.\n\n"
    "Extract only copies what a source states, with a quote checked word for word; anything computed or judged "
    "gets its own step. Publishing writes the output to a table of its own in Supabase (gp_…), created on the "
    "first publish, and nothing is written until you approve it."
)

TARGETS = {"table": {"label": "Supabase table", "default_fields": [], "catalogue": {}}}
