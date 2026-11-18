"""
The pipeline engine: a fixed catalog of steps, a runner, and the coverage gate.

A pipeline definition is data:

    {"schema":  {"status": "proposed" | "approved", "fields": [field, ...]},
     "sources": [{"id", "kind": "url" | "grants_gov", "url" | "query", "label"}],
     "steps":   [{"id", "type", "config", "note", "status": "proposed" | "approved"}]}

The target schema is the guardrail. It says which fields the pipeline must
produce, and every record carries, per field, how its value got there:

    extracted  read from a source, with a verbatim excerpt that is checked
    derived    computed by a deterministic step (parse, map, dedupe)
    inferred   proposed by a model step (llm_fill, geo_tie); the user approved the step
    assumed    a value the user stated, applied by set_value
    manual     a value the user entered while reviewing a step's output
    missing    nothing filled it

Load is refused while any required field is missing on any record, unless the
user has explicitly accepted blanks for that field. Nothing in here guesses a
value to make a gap go away; the agent is told to ask for more data instead.

Model calls (extract, llm_fill, geo_tie) are cached on their exact inputs, so
previewing a pipeline again does not pay for the same extraction twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from backend.tools import grants_gov, web_source
from backend.tools.llm_extractor import structured_call

log = logging.getLogger(__name__)

FIELD_TYPES = ("text", "number", "money", "percent", "date", "url", "enum", "boolean")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
SOURCE_CHARS = 12_000        # per extraction call: a long source is read in chunks of this size
# The pipeline's model steps (extract, research, llm_fill, geo_tie) work like an analyst, so they use the
# current Opus model. Discovery and the block funding agent keep their own model (llm_extractor.MODEL).
MODEL = "claude-opus-5-5"
EFFORT = "medium"
FETCH_CHARS = 60_000         # how much of one source is fetched (5 chunks at most)
PREVIEW_RECORDS = 300        # records kept per step output in state; a step can be reviewed only if all fit


PARALLEL = 6                 # model calls or page reads in flight at once
FILL_BATCH = 25              # records per llm_fill / geo_tie call; batches run in parallel


class PipelineError(ValueError):
    """A definition, step config or source the engine cannot accept. The message is user-facing."""


def _parallel(fn: Callable[[Any], Any], items: list, workers: int = PARALLEL) -> list:
    """
    fn over items, several at a time. Results come back in input order, with the
    exception in place of a result that failed, so callers decide what a failure
    means. Results are merged by the caller on one thread: fn must not write
    shared state (the model cache in particular).
    """
    def safe(item):
        try:
            return fn(item)
        except Exception as e:  # noqa: BLE001 — handed back to the caller
            return e
    if len(items) <= 1:
        return [safe(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        return list(ex.map(safe, items))


# ── Catalog ──────────────────────────────────────────────────────────────────
#
# What the agent may build with. The descriptions are served to the agent in
# its system prompt and to the UI, so the two cannot drift.

FILTER_OPS = ("equals", "not_equals", "contains", "not_contains", "is_blank", "not_blank",
              "before", "after", "gte", "lte")

CATALOG: dict[str, dict] = {
    "extract": {
        "label": "Extract",
        "model": True,
        "description": "Read the sources and pull out one record per item (e.g. per funding programme), "
                       "filling the target fields. Every record must quote a verbatim excerpt, which is "
                       "checked against the fetched text. Fields the text does not support stay blank.",
        "config": {"instructions": "optional str: how to read these sources (what counts as one item, "
                                   "which sections matter, what to ignore)",
                   "source_ids": "optional list of source ids; default all sources"},
    },
    "parse": {
        "label": "Parse",
        "model": False,
        "description": "Turn text into a typed value: money ('$250K', 'up to $1.5 million' -> number), "
                       "number, percent ('20% local match' -> 0.2) or date (-> YYYY-MM-DD).",
        "config": {"field": "str", "as": "money | number | percent | date"},
    },
    "map_values": {
        "label": "Map values",
        "model": False,
        "description": "Replace values of a field using a lookup table, e.g. agency names to "
                       "federal/state/local. Unmatched values are kept.",
        "config": {"field": "str", "mapping": "object {from: to}", "case_insensitive": "optional bool, default true"},
    },
    "set_value": {
        "label": "Set value",
        "model": False,
        "description": "Set a field to a value THE USER STATED, optionally only where another field "
                       "matches. Never use it to fill a gap with your own guess.",
        "config": {"field": "str", "value": "any", "only_if_blank": "optional bool, default true",
                   "where": f"optional {{field, op, value}}; op in {', '.join(FILTER_OPS)}"},
    },
    "filter": {
        "label": "Filter",
        "model": False,
        "description": "Keep only records matching a condition (e.g. deadline after 2026-01-01).",
        "config": {"field": "str", "op": " | ".join(FILTER_OPS), "value": "comparison value (not for is_blank/not_blank)"},
    },
    "dedupe": {
        "label": "Dedupe",
        "model": False,
        "description": "Merge records that share the same key fields, keeping the first and filling its "
                       "blanks from the duplicates.",
        "config": {"keys": "list of field names"},
    },
    "research": {
        "label": "Research",
        "model": True,
        "description": "Work like an analyst on records with blank fields: follow the links on each record's "
                       "source page (and URLs printed in a PDF) to the item's own pages, guidelines or "
                       "notices, read them, and fill the blanks. Every value found must quote the page it came "
                       "from, checked word for word; unquoted values are discarded. Optionally search the web "
                       "for an official page when the source links nothing useful.",
        "config": {"fields": "optional list of field names; default every extractable field that is blank somewhere",
                   "max_pages_per_record": "optional int 1-4, default 2",
                   "web_search": "optional bool, default false",
                   "instructions": "optional str: what to look for"},
    },
    "llm_fill": {
        "label": "Model fill",
        "model": True,
        "description": "Ask the model to set one field per record from that record's own excerpt and "
                       "values, with a one-line reason each. Marked 'inferred'. Use for judgement calls "
                       "the user asked for (e.g. classify each programme's project type), not for facts "
                       "the source should state.",
        "config": {"field": "str", "instruction": "str", "only_if_blank": "optional bool, default true"},
    },
    "geo_tie": {
        "label": "Geographic tie",
        "model": True,
        "kinds": ["funding"],
        "description": "Recommend each programme's geographic reach: federal, state:MD, county:24033 "
                       "(Prince George's), tract:NMTC (only in designated tracts) or unknown, with a "
                       "reason. Marked 'inferred'.",
        "config": {"field": "optional str, default geo_scope",
                   "instruction": "optional str: how to judge reach; replaces the default guidance (the allowed "
                                  "values stay fixed)"},
    },
}


def catalog_for(kind: str) -> dict[str, dict]:
    return {k: v for k, v in CATALOG.items() if kind in v.get("kinds", [kind])}


def catalog_text(kind: str) -> str:
    """The catalog as the agent reads it."""
    lines = []
    for name, spec in catalog_for(kind).items():
        cfg = "; ".join(f"{k}: {v}" for k, v in spec["config"].items())
        lines.append(f"- {name}{' (model call)' if spec['model'] else ''}: {spec['description']}\n  config: {cfg}")
    return "\n".join(lines)


# ── Validation ───────────────────────────────────────────────────────────────

def validate_fields(fields: list[dict]) -> list[dict]:
    """Normalise a proposed target schema. Raises PipelineError."""
    if not fields:
        raise PipelineError("The schema needs at least one field.")
    out, seen = [], set()
    for f in fields:
        name = (f.get("name") or "").strip()
        if not NAME_RE.match(name):
            raise PipelineError(f"Field name {name!r} must be lower_snake_case.")
        if name in seen:
            raise PipelineError(f"Field {name!r} appears twice.")
        seen.add(name)
        ftype = f.get("type") or "text"
        if ftype not in FIELD_TYPES:
            raise PipelineError(f"Field {name!r} has type {ftype!r}; use one of {', '.join(FIELD_TYPES)}.")
        enum = [str(v) for v in (f.get("enum_values") or [])]
        if ftype == "enum" and not enum:
            raise PipelineError(f"Enum field {name!r} needs enum_values.")
        out.append({
            "name": name, "type": ftype, "required": bool(f.get("required")),
            "description": (f.get("description") or "").strip(),
            **({"enum_values": enum} if enum else {}),
            # False for judgement fields (e.g. geographic reach): extraction leaves them
            # blank and a model step fills them, marked inferred, not "read from the source".
            "extract": f.get("extract", True) is not False,
            "accept_blank": bool(f.get("accept_blank")),
        })
    return out


def _field(fields: list[dict], name: Any) -> dict:
    f = next((x for x in fields if x["name"] == name), None)
    if not f:
        raise PipelineError(f"No field named {name!r} in the schema.")
    return f


def validate_step(kind: str, step_type: str, config: dict, fields: list[dict]) -> dict:
    """Check a step's type and config against the catalog and schema. Returns the cleaned config."""
    cat = catalog_for(kind)
    if step_type not in cat:
        raise PipelineError(f"Unknown step type {step_type!r}; available: {', '.join(cat)}.")
    c = dict(config or {})
    if step_type == "extract":
        if c.get("source_ids") is not None and not isinstance(c["source_ids"], list):
            raise PipelineError("extract.source_ids must be a list.")
    elif step_type == "parse":
        _field(fields, c.get("field"))
        if c.get("as") not in ("money", "number", "percent", "date"):
            raise PipelineError("parse.as must be money, number, percent or date.")
    elif step_type == "map_values":
        _field(fields, c.get("field"))
        if not isinstance(c.get("mapping"), dict) or not c["mapping"]:
            raise PipelineError("map_values.mapping must be a non-empty object.")
    elif step_type == "set_value":
        _field(fields, c.get("field"))
        if "value" not in c:
            raise PipelineError("set_value needs a value.")
        if c.get("where"):
            _check_condition(c["where"], fields)
    elif step_type == "filter":
        _check_condition(c, fields)
    elif step_type == "dedupe":
        keys = c.get("keys")
        if not isinstance(keys, list) or not keys:
            raise PipelineError("dedupe.keys must be a non-empty list.")
        for k in keys:
            _field(fields, k)
    elif step_type == "research":
        if c.get("fields") is not None:
            if not isinstance(c["fields"], list):
                raise PipelineError("research.fields must be a list.")
            for name in c["fields"]:
                _field(fields, name)
        if c.get("max_pages_per_record") is not None and not str(c["max_pages_per_record"]).isdigit():
            raise PipelineError("research.max_pages_per_record must be a whole number.")
    elif step_type == "llm_fill":
        _field(fields, c.get("field"))
        if not (c.get("instruction") or "").strip():
            raise PipelineError("llm_fill needs an instruction.")
    elif step_type == "geo_tie":
        c["field"] = c.get("field") or "geo_scope"
        _field(fields, c["field"])
    return c


def _check_condition(c: dict, fields: list[dict]) -> None:
    _field(fields, c.get("field"))
    if c.get("op") not in FILTER_OPS:
        raise PipelineError(f"op must be one of {', '.join(FILTER_OPS)}.")
    if c["op"] not in ("is_blank", "not_blank") and "value" not in c:
        raise PipelineError(f"op {c['op']} needs a value.")


# ── Sources ──────────────────────────────────────────────────────────────────
#
# Fetched text is cached for the process; a restart simply refetches. Only the
# metadata (size, preview, errors) is stored in the pipeline's state.

_SOURCE_CACHE: dict[str, dict] = {}


def _source_key(src: dict) -> str:
    return f"grants_gov:{src.get('query')}" if src["kind"] == "grants_gov" else f"url:{src.get('url')}"


def fetch_source(src: dict, fresh: bool = False) -> dict:
    """Fetch one source (cached). Raises PipelineError with a readable reason."""
    key = _source_key(src)
    if not fresh and key in _SOURCE_CACHE:
        return _SOURCE_CACHE[key]
    try:
        if src["kind"] == "grants_gov":
            text, hits = grants_gov.as_source_text(src["query"])
            fetched = {"url": "https://www.grants.gov/search-grants", "final_url": "https://www.grants.gov/search-grants",
                       "text": text, "truncated": False, "content_type": "grants.gov search",
                       "fetched_at": datetime.now(timezone.utc).isoformat(), "hits": len(hits)}
        else:
            fetched = web_source.fetch(src["url"], max_text=FETCH_CHARS)
    except Exception as e:  # noqa: BLE001 — every failure is reported to the user
        raise PipelineError(f"Could not read {src.get('label') or src.get('url') or src.get('query')}: {e}") from e
    _SOURCE_CACHE[key] = fetched
    return fetched


def source_meta(fetched: dict) -> dict:
    text = fetched.get("text") or ""
    return {"chars": len(text), "truncated": bool(fetched.get("truncated")),
            "content_type": fetched.get("content_type"), "final_url": fetched.get("final_url"),
            "fetched_at": fetched.get("fetched_at"), "preview": text[:600], "error": None}


# ── Typed parsing ────────────────────────────────────────────────────────────

_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "mm": 1e6, "b": 1e9, "billion": 1e9}


def parse_money(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)\s*(k|thousand|mm|m|million|b|billion)?\b", str(v).lower())
    if not m:
        return None
    return float(m.group(1).replace(",", "")) * _MULT.get(m.group(2) or "", 1)


def parse_number(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(v or ""))
    return float(m.group(0).replace(",", "")) if m else None


def parse_percent(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) / 100 if v > 1 else float(v)
    m = re.search(r"([\d.]+)\s*%", str(v or ""))
    if m:
        return float(m.group(1)) / 100
    n = parse_number(v)
    return (n / 100 if n is not None and n > 1 else n)


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%d %B %Y",
                 "%b. %d, %Y", "%Y/%m/%d")


def parse_date(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.strftime("%Y-%m-%d")
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", str(v).strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    return m.group(0) if m else None


PARSERS: dict[str, Callable[[Any], Any]] = {
    "money": parse_money, "number": parse_number, "percent": parse_percent, "date": parse_date,
}


# ── Records ──────────────────────────────────────────────────────────────────

def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _set(rec: dict, field: str, value: Any, kind: str, step_id: str, note: str | None = None, **extra) -> None:
    """Set a value with its provenance. extra carries evidence: quote, verified, unmatched, source_url."""
    rec["values"][field] = value
    if _blank(value):
        rec["prov"].pop(field, None)
    else:
        rec["prov"][field] = {"kind": kind, "step": step_id, **({"note": note} if note else {}),
                              **{k: v for k, v in extra.items() if v is not None}}


def _match(rec: dict, c: dict) -> bool:
    v, op, target = rec["values"].get(c["field"]), c["op"], c.get("value")
    if op == "is_blank":
        return _blank(v)
    if op == "not_blank":
        return not _blank(v)
    if _blank(v):
        return False
    sv, st = str(v).lower(), str(target).lower()
    if op == "equals":
        return sv == st
    if op == "not_equals":
        return sv != st
    if op == "contains":
        return st in sv
    if op == "not_contains":
        return st not in sv
    if op in ("before", "after"):
        a, b = parse_date(v), parse_date(target)
        if not a or not b:
            return False
        return a < b if op == "before" else a > b
    a, b = parse_number(v), parse_number(target)
    if a is None or b is None:
        return False
    return a >= b if op == "gte" else a <= b


def _hash(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _json_type(f: dict) -> dict:
    desc = f["description"] or f["name"]
    if f["type"] in ("number", "money", "percent"):
        extra = {"money": " In dollars, as a number.", "percent": " As a fraction, 0.2 for 20%."}.get(f["type"], "")
        return {"type": ["number", "null"], "description": desc + extra}
    if f["type"] == "boolean":
        return {"type": ["boolean", "null"], "description": desc}
    if f["type"] == "date":
        return {"type": ["string", "null"], "description": desc + " As YYYY-MM-DD."}
    if f["type"] == "enum":
        return {"type": ["string", "null"], "description": desc + " One of: " + ", ".join(f["enum_values"]) + "."}
    return {"type": ["string", "null"], "description": desc}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _normalise(f: dict, v: Any) -> tuple[Any, str | None]:
    """Coerce a model-supplied value to the field's type. Returns (value, warning)."""
    if _blank(v):
        return None, None
    if f["type"] == "date":
        d = parse_date(v)
        return (d, None) if d else (None, f"unreadable date {v!r}")
    if f["type"] == "enum":
        hit = next((e for e in f["enum_values"] if e.lower() == str(v).strip().lower()), None)
        return (hit, None) if hit else (str(v), f"not one of {', '.join(f['enum_values'])}")
    return v, None


EXTRACT_PROMPT = """You are a careful research analyst building a dataset for a city planner in Bowie, Maryland.
Read the source below the way an analyst would, and turn it into records.

THE PIPELINE'S GOAL:
{context}

HOW TO READ THIS SOURCE (from the planner):
{instructions}

SOURCE: {url}{part}
---
{text}
---

How to read it:
- Read all of it: tables, sidebars, footnotes, lists of links, "how to apply" and eligibility sections.
- One record per distinct item the goal is about (e.g. one funding programme). A page that lists several
  programmes gives several records; a programme mentioned twice is still one record. If the source holds
  no such item, return an empty list.
- Resolve what the text states, even indirectly: "up to $50,000 per project" is a maximum award of 50000;
  "a 25% local match" is 0.25; "applications close the third Friday of March 2027" is a date if the text
  makes it one. A statement made for a group ("these grants are administered by...", "the application
  period for these grants is...") applies to each item in the group.
- Do not go beyond the text. No outside knowledge, no likely values, no defaults. Leave a field null when
  this source does not state it; a blank the planner can fill is fine, an invented value is not.

Evidence, which is checked word for word against the source:
- For every field you fill, add an `evidence` entry: the field name and a quote copied verbatim from the
  source (one or more whole sentences, or a full table row) that states the value. You may quote
  sentences from different places for one field; quote each exactly.
- `source_excerpt`: the verbatim passage that introduces the item.
- `leads`: for fields this source does not state but points to elsewhere (a linked programme page,
  "see the guidelines", a NOFO), say which fields and where, using the link text or URL as written.
- `analyst_note`: caveats a planner should know, in one or two sentences: a closed round, an amount from a
  past cycle, a rolling deadline, an item that is really a loan or a category rather than a programme.
  Empty if none.
- `confidence`: 0-1, how well the source supports the record as a whole."""


def _chunks(text: str, size: int = SOURCE_CHARS) -> list[str]:
    """Split at a paragraph or line break near each boundary, so an item is rarely cut in half."""
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = max(text.rfind("\n\n", start + size // 2, end), text.rfind("\n", start + size // 2, end))
            end = cut if cut > start else end
        out.append(text[start:end])
        start = end
    return out


def _evidence_schema(field_names: list[str]) -> dict:
    return {"type": "array", "items": _obj({
        "field": {"type": "string", "enum": field_names},
        "quote": {"type": "string", "description": "Verbatim from the source."}})}


def _apply_evidence(rec: dict, f: dict, value: Any, quotes: list[str], doc: dict, step_id: str,
                    note: str | None, source_url: str | None = None) -> str | None:
    """Set a field with its quotes checked against `doc`. Returns a warning when it is unconfirmed."""
    quote = " ".join(q for q in quotes if q) or None
    check = web_source.check_excerpt(quote, [doc]) if quote else {"verified": False, "missing": []}
    _set(rec, f["name"], value, "extracted", step_id, note,
         quote=quote, verified=check["verified"], unmatched=check.get("missing") or None,
         **({"source_url": source_url} if source_url else {}))
    if _blank(value):
        return None
    if not quote:
        return f"{rec['id']}.{f['name']}: no quote given"
    return None if check["verified"] else f"{rec['id']}.{f['name']}: quote not found in the source"


def _extract(step: dict, sources: list[dict], fetched: dict[str, dict], fields: list[dict],
             context: str, cache: dict, say: Callable[[str], None] = lambda _m: None) -> tuple[list[dict], list[str]]:
    ids = step["config"].get("source_ids") or [s["id"] for s in sources]
    readable = [f for f in fields if f.get("extract", True)]
    names = [f["name"] for f in readable]
    record_schema = _obj({
        **{f["name"]: _json_type(f) for f in readable},
        "evidence": _evidence_schema(names),
        "source_excerpt": {"type": "string", "description": "Verbatim passage introducing the item."},
        "leads": {"type": "array", "items": _obj({
            "fields": {"type": "array", "items": {"type": "string"}},
            "where": {"type": "string", "description": "Link text or URL as written in the source."}})},
        "analyst_note": {"type": "string"},
        "confidence": {"type": "number", "description": "0-1."}})
    schema = _obj({"records": {"type": "array", "items": record_schema}})
    records, warnings, field_warnings = [], [], []
    # Every part of every source is read in parallel; records are then assembled in source order.
    plan, jobs = [], {}
    for src in sources:
        if src["id"] not in ids or src["id"] not in fetched:
            continue
        doc = fetched[src["id"]]
        parts = _chunks(doc.get("text") or "")
        keys = []
        for i, text in enumerate(parts):
            key = _hash(["extract2", MODEL, step["config"].get("instructions"), readable, text, context])
            keys.append(key)
            if key not in cache and key not in jobs:
                jobs[key] = EXTRACT_PROMPT.format(
                    context=context or "(none given)",
                    instructions=step["config"].get("instructions") or "(no special instructions)",
                    url=doc.get("final_url") or src.get("url"),
                    part=f" (part {i + 1} of {len(parts)})" if len(parts) > 1 else "",
                    text=text)
        plan.append((src, doc, parts, keys))
    if jobs:
        say(f"Extracting {len(jobs)} part(s), {min(len(jobs), PARALLEL)} at a time")
        results = _parallel(lambda prompt: structured_call(prompt, schema, max_tokens=32000, model=MODEL,
                                                           effort=EFFORT)["records"], list(jobs.values()))
        for key, res in zip(jobs, results):
            if not isinstance(res, Exception):
                cache[key] = res
        failed = [r for r in results if isinstance(r, Exception)]
        if failed:
            raise failed[0]   # the parts that succeeded stay cached for the next preview
    for src, doc, parts, keys in plan:
        raws = [x for key in keys for x in cache[key]]
        label = src.get("label") or src["id"]
        if len(parts) > 1:
            warnings.append(f"{label}: read in {len(parts)} parts; an item split across parts may appear twice "
                            "(add a dedupe step).")
        if doc.get("truncated"):
            warnings.append(f"{label}: longer than {FETCH_CHARS:,} characters; the rest was not read.")
        for n, raw in enumerate(raws, 1):
            check = web_source.check_excerpt(raw.get("source_excerpt"), [doc])
            rec = {"id": f"{src['id']}-{n}", "values": {}, "prov": {},
                   "source_id": src["id"], "source_url": doc.get("final_url") or src.get("url"),
                   "excerpt": raw.get("source_excerpt"), "confidence": raw.get("confidence"),
                   "excerpt_verified": check["verified"], "excerpt_unmatched": check["missing"] or None,
                   "analyst_note": (raw.get("analyst_note") or "").strip() or None,
                   "leads": raw.get("leads") or []}
            quotes: dict[str, list[str]] = {}
            for e in raw.get("evidence") or []:
                quotes.setdefault(e.get("field"), []).append(e.get("quote") or "")
            for f in fields:
                if not f.get("extract", True):
                    _set(rec, f["name"], None, "extracted", step["id"])
                    continue
                value, warn = _normalise(f, raw.get(f["name"]))
                w = _apply_evidence(rec, f, value, quotes.get(f["name"], []), doc, step["id"], warn)
                if w:
                    field_warnings.append(w)
            records.append(rec)
    unverified = [r["id"] for r in records if not r["excerpt_verified"]]
    if unverified:
        warnings.append(f"{len(unverified)} record(s) quote an excerpt not found in the source: "
                        f"{', '.join(unverified[:8])}. Treat as unconfirmed.")
    if field_warnings:
        warnings.append(f"{len(field_warnings)} value(s) without a confirmed quote: "
                        + "; ".join(field_warnings[:6]) + ("…" if len(field_warnings) > 6 else ""))
    return records, warnings


# ── Research: follow the trail like an analyst ───────────────────────────────

CHOOSE_PROMPT = """You are a research analyst filling gaps in a dataset for a city planner in Bowie, Maryland.

THE PIPELINE'S GOAL:
{context}

HOW TO RESEARCH (from the planner):
{instructions}

The records below were read from this page: {url}
Each lists what is known, the fields still blank, and any leads the first reading noted. Known values
read from an overview page are often thin (a heading, a one-line summary); the item's own page usually
states them in full.

RECORDS:
{records}

LINKS ON THAT PAGE (number: link text -> url):
{links}

For each record, choose up to {n} links most likely to state its blank fields for THAT item: its own
programme page, guidelines, a notice of funding, an application manual, an FAQ. Prefer official pages
and links named after the item. Choose nothing for a record when no link is plausibly about it. Use only
the numbers above."""

FILL_FROM_PAGE_PROMPT = """You are a research analyst filling gaps in a dataset for a city planner in Bowie, Maryland.

THE PIPELINE'S GOAL:
{context}

HOW TO RESEARCH (from the planner):
{instructions}

PAGE: {url}
---
{text}
---

For each record below, use THIS page to improve what is known about THAT item:
- action "fill": a field listed as blank, where this page states it for this item.
- action "replace": a field already known, where this page (typically the item's own page) states it
  more fully or more precisely than the known value, e.g. eligibility described in full rather than a
  heading. Do not replace a value with an equivalent one.
- conflicts: where this page contradicts a known value, report the field, what the page says and the
  quote. Do not change the value.
- note: one or two sentences of caveats this page adds (a closed round, when the next cycle opens, a
  cap that applies per year, a required pre-application). Empty if none.
Rules:
- Resolve what the page states, even indirectly ("up to $50,000" is 50000; "25% match" is 0.25). A
  statement made for a group of items applies to each item in it.
- No outside knowledge, no likely values. If the page does not state something for this item, omit it.
- Every value and conflict needs a quote copied verbatim from the page (whole sentences or a full table
  row). It is checked word for word; anything whose quote is not found is discarded.

FIELDS (name: what it holds):
{fields}

RECORDS:
{records}"""


def _short_v(v: Any, n: int = 80) -> Any:
    return v[:n] + "…" if isinstance(v, str) and len(v) > n else v


def _page(url: str, fresh: bool = False) -> dict:
    return fetch_source({"kind": "url", "url": url}, fresh=fresh)


def _research(step: dict, records: list[dict], fields: list[dict], cache: dict, context: str,
              say: Callable[[str], None]) -> list[str]:
    cfg = step["config"]
    wanted = cfg.get("fields") or []
    targets = [f for f in fields if f.get("extract", True) and (not wanted or f["name"] in wanted)]
    by_name = {f["name"]: f for f in targets}
    per_rec = int(min(max(cfg.get("max_pages_per_record") or 2, 1), 4))
    # By default every record is researched: blank fields get filled, thin ones improved, and
    # contradictions surfaced. refine=false limits it to records with blanks.
    refine = cfg.get("refine", True) is not False
    todo = [r for r in records if refine or any(_blank(r["values"].get(f["name"])) for f in targets)]
    if not todo:
        return ["Nothing to research: no blank fields."]
    warnings: list[str] = []

    def blanks(r: dict) -> list[str]:
        return [f["name"] for f in targets if _blank(r["values"].get(f["name"]))]

    # 1. Plan: which pages to read for which record, one model call per source page, all in parallel.
    plan: dict[str, list[str]] = {}
    groups: dict[str, list[dict]] = {}
    for r in todo:
        groups.setdefault(r.get("source_url") or "", []).append(r)
    group_items = list(groups.items())

    def links_for(item: tuple[str, list[dict]]) -> list[dict]:
        src_url = item[0]
        links = (_page(src_url).get("links") or []) if src_url else []
        return [l for l in links if l["url"].rstrip("/") != (src_url or "").rstrip("/")][:250]

    choose_schema = _obj({"choices": {"type": "array", "items": _obj({
        "id": {"type": "string"}, "links": {"type": "array", "items": {"type": "integer"}},
        "why": {"type": "string"}})}})
    choose_jobs: dict[str, str] = {}
    chosen: list[tuple[list[dict], list[dict], str | None]] = []
    for (src_url, recs), links in zip(group_items, _parallel(links_for, group_items)):
        if isinstance(links, Exception):
            warnings.append(f"could not reopen {src_url}: {links}")
            links = []
        key = None
        if links:
            digest = [{"id": r["id"], "known": {k: _short_v(v) for k, v in r["values"].items() if not _blank(v)},
                       "blank": blanks(r), "leads": r.get("leads") or []} for r in recs]
            key = _hash(["choose", MODEL, cfg.get("instructions"), context, src_url, digest, links, per_rec])
            if key not in cache and key not in choose_jobs:
                choose_jobs[key] = CHOOSE_PROMPT.format(
                    context=context or "(none given)",
                    instructions=cfg.get("instructions") or "(none given)",
                    url=src_url, n=per_rec, records=json.dumps(digest, indent=1, default=str),
                    links="\n".join(f"{i}: {l['text'] or '(no text)'} -> {l['url']}" for i, l in enumerate(links)))
        chosen.append((recs, links, key))
    if choose_jobs:
        say(f"Choosing pages to read for {len(todo)} record(s)")
        results = _parallel(lambda prompt: structured_call(prompt, choose_schema, max_tokens=16000, model=MODEL,
                                                           effort=EFFORT)["choices"], list(choose_jobs.values()))
        for key, res in zip(choose_jobs, results):
            if isinstance(res, Exception):
                warnings.append(f"could not choose pages for some records: {res}")
            else:
                cache[key] = res
    for recs, links, key in chosen:
        for c in cache.get(key, []) if key else []:
            urls = [links[i]["url"] for i in c.get("links") or [] if isinstance(i, int) and 0 <= i < len(links)]
            if urls:
                plan[c["id"]] = urls[:per_rec]
        if cfg.get("web_search"):
            for r in recs:
                if r["id"] in plan:
                    continue
                name = next(iter(r["values"].values()), "") or ""
                say(f"Searching the web for {name[:40]}")
                key = _hash(["search", name, r["values"].get("source_agency")])
                if key not in cache:
                    res = web_source.search(f"{name} {r['values'].get('source_agency') or ''} official programme page")
                    cache[key] = [x["url"] for x in res.get("results", []) if x.get("url")][:per_rec]
                if cache[key]:
                    plan[r["id"]] = cache[key]

    # 2. Read each chosen page once, for every record that chose it. Pages are read and
    # questioned in parallel; their answers are applied below in a fixed order.
    pages: dict[str, list[dict]] = {}
    recs_by_id = {r["id"]: r for r in todo}
    for rid, urls in plan.items():
        for u in urls:
            if rid in recs_by_id:
                pages.setdefault(u, []).append(recs_by_id[rid])
    filled_total, replaced_total, conflicts_total, discarded = 0, 0, 0, 0
    names = [f["name"] for f in targets]
    fill_schema = _obj({"results": {"type": "array", "items": _obj({
        "id": {"type": "string"},
        "values": {"type": "array", "items": _obj({
            "field": {"type": "string", "enum": names},
            "action": {"type": "string", "enum": ["fill", "replace"]},
            "value": {"type": ["string", "number", "boolean", "null"]},
            "quote": {"type": "string"}})},
        "conflicts": {"type": "array", "items": _obj({
            "field": {"type": "string", "enum": names},
            "page_says": {"type": "string"}, "quote": {"type": "string"}})},
        "note": {"type": "string"}})}})

    def read(item: tuple[str, list[dict]]) -> tuple[dict, str, list[dict] | None]:
        """Fetch a page and ask about it. Reads the cache, never writes it."""
        url, recs = item
        doc = _page(url)
        digest = [{"id": r["id"], "known": {k: v for k, v in r["values"].items() if not _blank(v)},
                   "blank": blanks(r)} for r in recs]
        key = _hash(["research_page", MODEL, context, url, (doc.get("text") or "")[:SOURCE_CHARS], digest]
                    + ([cfg["instructions"]] if cfg.get("instructions") else []))
        if key in cache:
            return doc, key, None
        return doc, key, structured_call(FILL_FROM_PAGE_PROMPT.format(
            context=context or "(none given)", url=doc.get("final_url") or url,
            instructions=cfg.get("instructions") or "(none given)",
            text=(doc.get("text") or "")[:SOURCE_CHARS],
            fields="\n".join(f"- {n}: {by_name[n]['description'] or n} ({by_name[n]['type']})" for n in names),
            records=json.dumps(digest, indent=1, default=str)),
            fill_schema, max_tokens=16000, model=MODEL, effort=EFFORT)["results"]

    page_items = list(pages.items())
    if page_items:
        say(f"Reading {len(page_items)} page(s), {min(len(page_items), PARALLEL)} at a time")
    for (url, recs), got in zip(page_items, _parallel(read, page_items)):
        if isinstance(got, Exception):
            warnings.append(f"could not read {url}: {got}")
            for r in recs:
                r.setdefault("research", []).append({"url": url, "error": str(got)})
            continue
        doc, key, answer = got
        if answer is not None:
            cache[key] = answer
        page_url = doc.get("final_url") or url
        got = {x["id"]: x for x in cache[key]}
        for r in recs:
            res = got.get(r["id"]) or {}
            filled, replaced = [], []
            for v in res.get("values") or []:
                f = by_name.get(v.get("field"))
                if not f:
                    continue
                old = r["values"].get(f["name"])
                if not _blank(old) and v.get("action") != "replace":
                    continue
                raw = v.get("value")
                if isinstance(raw, str) and f["type"] in PARSERS:
                    raw = PARSERS[f["type"]](raw)
                value, warn = _normalise(f, raw)
                if _blank(value) or value == old:
                    continue
                if not web_source.check_excerpt(v.get("quote"), [doc])["verified"]:
                    discarded += 1
                    continue
                how = (f"replaced {_short_v(old)!r} with the fuller statement on {page_url}" if not _blank(old)
                       else f"found by research on {page_url}")
                _set(r, f["name"], value, "extracted", step["id"], "; ".join(x for x in (how, warn) if x),
                     quote=v.get("quote"), verified=True, source_url=page_url)
                (replaced if not _blank(old) else filled).append(f["name"])
            for c in res.get("conflicts") or []:
                if c.get("field") in by_name and web_source.check_excerpt(c.get("quote"), [doc])["verified"]:
                    r.setdefault("conflicts", []).append({**c, "url": page_url,
                                                          "current": r["values"].get(c["field"])})
                    conflicts_total += 1
            note = (res.get("note") or "").strip()
            if note:
                r["analyst_note"] = " ".join(x for x in (r.get("analyst_note"), note) if x)
            filled_total += len(filled)
            replaced_total += len(replaced)
            r.setdefault("research", []).append({"url": page_url, "filled": filled, "replaced": replaced})
    nothing = [r["id"] for r in todo if r["id"] not in plan]
    warnings.insert(0, f"Read {len(pages)} page(s) for {len(todo)} record(s): filled {filled_total} blank value(s), "
                       f"improved {replaced_total}, found {conflicts_total} conflict(s).")
    if conflicts_total:
        warnings.append("Conflicts are listed on each record; the values were not changed. Check them.")
    if discarded:
        warnings.append(f"{discarded} value(s) discarded because their quote was not found on the page.")
    if nothing:
        warnings.append(f"No promising page for {len(nothing)} record(s)"
                        + ("" if cfg.get("web_search") else "; web_search could look further") + ".")
    return warnings


FILL_PROMPT = """For each record below, set the field `{field}` ({type_desc}).

INSTRUCTION FROM THE PLANNER: {instruction}

Use only what each record's own values, evidence and notes support. If they do not support a value,
return null for that record. Give a one-line reason for each.

RECORDS:
{records}"""

GEO_VALUES = """Recommend each programme's geographic reach. The planner works on city blocks in Bowie,
Prince George's County, Maryland (county FIPS 24033). Use one of: federal (available nationally),
state:MD (Maryland only), county:24033 (Prince George's County only), tract:NMTC (only in tracts
carrying a designation; name it after 'tract:'), unknown (the source does not say)."""
GEO_GUIDANCE = """Judge by who may
apply and where the money may be spent, as the evidence states it: a programme open to every Maryland
jurisdiction is state:MD even if a county or city administers its local share; a federal programme
that a state agency runs is state:MD. Base it on the evidence, not on what would be convenient."""


def geo_instruction(cfg: dict) -> str:
    return GEO_VALUES + " " + (cfg.get("instruction") or GEO_GUIDANCE)


# ── What a model step sends, for the planner to read and tune ────────────────
#
# Each model step's prompt is fixed text (the reading and evidence rules the
# quote check depends on) around one slot the planner owns: the step's
# instructions. prompt_view renders the real template with that slot marked,
# so what the planner reads is what the model gets.

_SLOT = "\x00SLOT\x00"
EDITABLE = {"extract": "instructions", "research": "instructions", "llm_fill": "instruction", "geo_tie": "instruction"}


def _split(text: str) -> dict:
    before, after = text.split(_SLOT)
    return {"before": before, "after": after}


def prompt_view(step: dict, fields: list[dict], context: str) -> dict | None:
    t, c = step["type"], step.get("config") or {}
    if t not in EDITABLE:
        return None
    ph = lambda s: f"‹{s}›"
    goal = context or "(none given)"
    targets = [f for f in fields if f.get("extract", True)]
    view = {"key": EDITABLE[t], "value": c.get(EDITABLE[t]) or "", "default": "(none given)", "prompts": []}
    if t == "extract":
        view["default"] = "(no special instructions)"
        view["prompts"].append({"title": "Read each source (once per part of up to "
                                         f"{SOURCE_CHARS:,} characters)", **_split(EXTRACT_PROMPT.format(
            context=goal, instructions=_SLOT, url=ph("source URL"), part="",
            text=ph("the source text")))})
    elif t == "research":
        rec = ph("each record: what is known, which fields are blank, and its leads")
        view["prompts"].append({"title": "1. Choose which linked pages to read for each record", **_split(CHOOSE_PROMPT.format(
            context=goal, instructions=_SLOT, url=ph("the record's source page"), n=c.get("max_pages_per_record") or 2,
            records=rec, links=ph("the links found on that page")))})
        names = c.get("fields") or [f["name"] for f in targets]
        by_name = {f["name"]: f for f in fields}
        view["prompts"].append({"title": "2. Read each chosen page and fill or improve values", **_split(FILL_FROM_PAGE_PROMPT.format(
            context=goal, instructions=_SLOT, url=ph("page URL"), text=ph("the page text"),
            fields="\n".join(f"- {n}: {by_name[n]['description'] or n} ({by_name[n]['type']})" for n in names if n in by_name),
            records=rec))})
    else:
        f = _field(fields, c.get("field") or "geo_scope")
        instruction = (GEO_VALUES + " " + _SLOT) if t == "geo_tie" else _SLOT
        if t == "geo_tie":
            view["value"] = c.get("instruction") or GEO_GUIDANCE
            view["default"] = GEO_GUIDANCE
        view["prompts"].append({"title": f"Judge `{f['name']}` for each record", **_split(FILL_PROMPT.format(
            field=f["name"], type_desc=_json_type(f)["description"], instruction=instruction,
            records=ph("each record's values, quotes, excerpt and analyst note")))})
    if t in ("extract", "research"):
        view["fields"] = [{"name": f["name"], "description": _json_type(f)["description"]} for f in targets]
    return view


def _model_fill(step: dict, records: list[dict], fields: list[dict], cache: dict,
                instruction: str, kind_label: str) -> list[str]:
    f = _field(fields, step["config"]["field"])
    only_blank = step["config"].get("only_if_blank", True)
    targets = [r for r in records if not (only_blank and not _blank(r["values"].get(f["name"])))]
    if not targets:
        return []
    digest = [{"id": r["id"], "values": r["values"], "excerpt": (r.get("excerpt") or "")[:800],
               "evidence": {k: (p.get("quote") or "")[:400] for k, p in r["prov"].items() if p.get("quote")},
               "analyst_note": r.get("analyst_note"), "source_url": r.get("source_url")} for r in targets]
    schema = _obj({"results": {"type": "array", "items": _obj({
        "id": {"type": "string"}, "value": _json_type(f), "reason": {"type": "string"}})}})
    # Batches of records, judged in parallel. Each record is judged on its own evidence,
    # so splitting them does not change what the model sees about any one record.
    batches = [digest[i:i + FILL_BATCH] for i in range(0, len(digest), FILL_BATCH)]
    keys = [_hash([kind_label, MODEL, instruction, f, b]) for b in batches]
    jobs = {k: b for k, b in zip(keys, batches) if k not in cache}
    results = _parallel(lambda b: structured_call(FILL_PROMPT.format(
        field=f["name"], type_desc=_json_type(f)["description"], instruction=instruction,
        records=json.dumps(b, indent=1, default=str)), schema, max_tokens=16000, model=MODEL,
        effort=EFFORT)["results"], list(jobs.values()))
    for k, res in zip(jobs, results):
        if not isinstance(res, Exception):
            cache[k] = res
    failed = [r for r in results if isinstance(r, Exception)]
    if failed:
        raise failed[0]   # the batches that succeeded stay cached for the next preview
    by_id = {r["id"]: r for k in keys for r in cache[k]}
    warnings = []
    for r in targets:
        hit = by_id.get(r["id"])
        if not hit:
            continue
        value, warn = _normalise(f, hit.get("value"))
        _set(r, f["name"], value, "inferred", step["id"], "; ".join(x for x in (hit.get("reason"), warn) if x))
        if warn:
            warnings.append(f"{r['id']}: {warn}")
    return warnings


def _apply(step: dict, records: list[dict], fields: list[dict]) -> tuple[list[dict], list[str]]:
    """Deterministic steps."""
    t, c, sid = step["type"], step["config"], step["id"]
    warnings: list[str] = []
    if t == "parse":
        fn = PARSERS[c["as"]]
        for r in records:
            v = r["values"].get(c["field"])
            if _blank(v):
                continue
            parsed = fn(v)
            if parsed is None:
                warnings.append(f"{r['id']}: could not parse {v!r} as {c['as']}")
            elif parsed != v:
                _set(r, c["field"], parsed, "derived", sid, f"parsed from {v!r}")
        return records, warnings
    if t == "map_values":
        ci = c.get("case_insensitive", True)
        table = {(k.lower() if ci else k): v for k, v in c["mapping"].items()}
        for r in records:
            v = r["values"].get(c["field"])
            if _blank(v):
                continue
            k = str(v).lower() if ci else str(v)
            if k in table:
                _set(r, c["field"], table[k], "derived", sid, f"mapped from {v!r}")
        return records, warnings
    if t == "set_value":
        for r in records:
            if c.get("where") and not _match(r, c["where"]):
                continue
            if c.get("only_if_blank", True) and not _blank(r["values"].get(c["field"])):
                continue
            _set(r, c["field"], c["value"], "assumed", sid, "value stated by the user")
        return records, warnings
    if t == "filter":
        kept = [r for r in records if _match(r, c)]
        if len(kept) < len(records):
            warnings.append(f"removed {len(records) - len(kept)} record(s)")
        return kept, warnings
    if t == "dedupe":
        out, index = [], {}
        for r in records:
            key = tuple(str(r["values"].get(k) or "").strip().lower() for k in c["keys"])
            if key in index:
                keep = index[key]
                for f, v in r["values"].items():
                    if _blank(keep["values"].get(f)) and not _blank(v):
                        keep["values"][f] = v
                        keep["prov"][f] = {**r["prov"].get(f, {}), "note": f"merged from {r['id']}"}
                continue
            index[key] = r
            out.append(r)
        if len(out) < len(records):
            warnings.append(f"merged {len(records) - len(out)} duplicate(s)")
        return out, warnings
    raise PipelineError(f"Step type {t!r} is not deterministic.")


# ── The planner's overrides on the final output ──────────────────────────────
#
# Once every step is approved and the pipeline has run, the planner can correct
# values, remove records and add ones the sources missed. The overrides are
# saved on the definition (definition["overrides"]) and applied to the final
# output of every full run, so a re-run keeps them:
#
#     {"removed": [{"id", "key"}], "changed": [{"id", "key", "values": {field: value}}],
#      "added": [{"id", "values": {field: value}}]}
#
# key is the record's first field (e.g. programme name) when the edit was made.
# Record ids follow the order the model listed items in, so if a re-run lists
# them differently an edit follows its key; one that matches nothing is reported
# rather than applied to the wrong record. Applied values are marked "manual",
# keeping what they replaced, so an edit can be undone without re-running.

EMPTY_EDITS = {"removed": [], "changed": [], "added": []}


def record_key(rec: dict, fields: list[dict]) -> str:
    v = rec["values"].get(fields[0]["name"]) if fields else None
    return str(v or "").strip().lower()


def _find(records: list[dict], edit: dict, fields: list[dict]) -> dict | None:
    by_id = next((r for r in records if r["id"] == edit["id"]), None)
    if by_id and (not edit.get("key") or record_key(by_id, fields) == edit["key"]):
        return by_id
    if edit.get("key"):
        return next((r for r in records if record_key(r, fields) == edit["key"]), None)
    return None


OVERRIDE_NOTE = "your override on the final output"


def _manual(rec: dict, field: str, value: Any) -> None:
    old = rec["prov"].get(field)
    was = old["was"] if old and old.get("kind") == "manual" else {"value": rec["values"].get(field), "prov": old}
    rec["values"][field] = value
    rec["prov"][field] = {"kind": "manual", "step": "final", "note": OVERRIDE_NOTE, "was": was}


def clean_edit_values(values: dict, fields: list[dict]) -> dict:
    """Type a planner's values like extracted ones ('$50,000' -> 50000). Raises PipelineError."""
    out = {}
    by_name = {f["name"]: f for f in fields}
    for name, v in (values or {}).items():
        f = by_name.get(name)
        if not f:
            raise PipelineError(f"No field named {name!r}.")
        if isinstance(v, str):
            v = v.strip()
        if _blank(v):
            out[name] = None
            continue
        t = f["type"]
        if t in PARSERS:
            value = PARSERS[t](v)
        elif t == "enum":
            value = next((e for e in f["enum_values"] if e.lower() == str(v).lower()), None)
        elif t == "boolean":
            value = v if isinstance(v, bool) else {"yes": True, "true": True, "no": False, "false": False}.get(str(v).lower())
        elif t == "url":
            value = v if str(v).lower().startswith(("http://", "https://")) else None
        else:
            value = str(v)
        if value is None:
            hint = f" (one of {', '.join(f['enum_values'])})" if t == "enum" else ""
            raise PipelineError(f"{name}: {v!r} is not a valid {t}{hint}.")
        out[name] = value
    return out


def apply_edits(overrides: dict | None, records: list[dict], fields: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """Apply the planner's overrides. Returns (records, removed as [{index, record}], warnings)."""
    edits = {**EMPTY_EDITS, **(overrides or {})}
    warnings, removed = [], []
    for e in edits["changed"]:
        r = _find(records, e, fields)
        if not r:
            warnings.append(f"your change to “{e.get('key') or e['id']}” matches no record any more")
            continue
        for name, v in e["values"].items():
            _manual(r, name, v)
    for e in edits["removed"]:
        r = _find(records, e, fields)
        if not r:
            warnings.append(f"“{e.get('key') or e['id']}”, which you removed, is no longer in the output")
            continue
        removed.append({"index": records.index(r), "record": r})
    gone = {id(x["record"]) for x in removed}
    records = [r for r in records if id(r) not in gone]
    for e in edits["added"]:
        rec = {"id": e["id"], "values": {f["name"]: None for f in fields}, "prov": {},
               "source_id": None, "source_url": e.get("source_url"), "excerpt": None,
               "excerpt_verified": None, "added_by_you": True}
        for name, v in e["values"].items():
            if not _blank(v):
                rec["values"][name] = v
                rec["prov"][name] = {"kind": "manual", "step": "final", "note": OVERRIDE_NOTE}
        records.append(rec)
    n = len(edits["changed"]) + len(edits["removed"]) + len(edits["added"])
    if n:
        warnings.insert(0, f"your overrides: {len(edits['changed'])} changed, {len(removed)} removed, "
                           f"{len(edits['added'])} added")
    return records, removed, warnings


def unapply_edits(out: dict) -> list[dict]:
    """The pipeline's output before the planner's overrides, rebuilt from a stored run."""
    def revert(r: dict) -> dict:
        r = json.loads(json.dumps(r))
        for name, p in list(r["prov"].items()):
            if p.get("kind") == "manual" and "was" in p:
                r["values"][name] = p["was"]["value"]
                if p["was"]["prov"]:
                    r["prov"][name] = p["was"]["prov"]
                else:
                    r["prov"].pop(name)
        return r

    records = [revert(r) for r in out.get("records") or [] if not r.get("added_by_you")]
    for x in sorted(out.get("removed") or [], key=lambda x: x["index"]):
        records.insert(min(x["index"], len(records)), revert(x["record"]))
    return records


# ── Coverage and the load gate ───────────────────────────────────────────────

def coverage(records: list[dict], fields: list[dict]) -> dict:
    """Per field: how many records have a value, and by what kind; which required fields have gaps."""
    per_field = {}
    for f in fields:
        kinds: dict[str, int] = {}
        missing = []
        for r in records:
            p = r["prov"].get(f["name"])
            if p and not _blank(r["values"].get(f["name"])):
                kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
            else:
                missing.append(r["id"])
        per_field[f["name"]] = {"filled": len(records) - len(missing), "missing": len(missing),
                                "missing_ids": missing[:20], "kinds": kinds,
                                "required": f["required"], "accept_blank": f.get("accept_blank", False)}
    gaps = [n for n, c in per_field.items() if c["required"] and c["missing"]]
    blocking = [n for n in gaps if not per_field[n]["accept_blank"]]
    return {"records": len(records), "fields": per_field, "required_gaps": gaps, "blocking_gaps": blocking}


def changes_since(prev: list[dict] | None, records: list[dict], fields: list[dict]) -> dict:
    """
    What a run changes against the last published output, matched by the first
    field (e.g. programme name): records new, gone and changed (field, old, new).
    marks maps a record id to "new" or "changed" for the review table.
    """
    names = [f["name"] for f in fields]
    old = {record_key(r, fields): r for r in prev or []}
    seen, added, changed, marks, unchanged = set(), [], [], {}, 0
    label = lambda r: r["values"].get(names[0]) if names else r["id"]
    for r in records:
        k = record_key(r, fields)
        seen.add(k)
        o = old.get(k)
        if o is None:
            added.append({"id": r["id"], "name": label(r)})
            marks[r["id"]] = "new"
            continue
        diffs = [{"field": n, "old": o["values"].get(n), "new": r["values"].get(n)} for n in names
                 if str(o["values"].get(n) or "").strip() != str(r["values"].get(n) or "").strip()]
        if diffs:
            changed.append({"id": r["id"], "name": label(r), "fields": diffs})
            marks[r["id"]] = "changed"
        else:
            unchanged += 1
    removed = [{"name": label(o)} for k, o in old.items() if k not in seen]
    return {"added": added, "removed": removed, "changed": changed, "unchanged": unchanged, "marks": marks}


def definition_hash(definition: dict) -> str:
    """What a run's results depend on. Approval status and notes are excluded."""
    schema = definition.get("schema") or {}
    return _hash({
        "goal": definition.get("goal"),
        "fields": [{k: f.get(k) for k in ("name", "type", "required", "enum_values", "extract")}
                   for f in schema.get("fields", [])],
        "sources": [{k: s.get(k) for k in ("id", "kind", "url", "query")} for s in definition.get("sources", [])],
        "steps": [{k: s.get(k) for k in ("id", "type", "config")} for s in definition.get("steps", [])],
        **({"overrides": definition["overrides"]} if definition.get("overrides") else {}),
    })


def design_gaps(definition: dict) -> list[str]:
    """
    Fields the steps cannot produce at all, whatever the sources say: a required
    field with no extract step to read it, or a judgement field (extract=false)
    with no step that sets it. A required field the planner accepts blank is not
    a gap. The design is not finished while this is non-empty.
    """
    fields = (definition.get("schema") or {}).get("fields") or []
    steps = definition.get("steps") or []
    has_extract = any(s["type"] == "extract" for s in steps)
    set_by_step = {(s.get("config") or {}).get("field") or ("geo_scope" if s["type"] == "geo_tie" else None)
                   for s in steps if s["type"] in ("llm_fill", "geo_tie", "set_value")}
    gaps = []
    for f in fields:
        if f.get("accept_blank"):
            continue
        if f.get("extract", True):
            if f["required"] and not has_extract:
                gaps.append(f["name"])
        elif f["name"] not in set_by_step:
            gaps.append(f["name"])
    return gaps


def load_gate(definition: dict, state: dict, required_fields: list[str]) -> list[str]:
    """Every reason Load is refused right now. Empty list = loadable."""
    reasons = []
    schema = definition.get("schema") or {}
    fields = schema.get("fields") or []
    if not fields:
        reasons.append("No target schema yet.")
    elif schema.get("status") != "approved":
        reasons.append("The target schema is not approved.")
    names = {f["name"] for f in fields}
    for req in required_fields:
        if fields and req not in names:
            reasons.append(f"The schema must include `{req}`.")
    if not definition.get("sources"):
        reasons.append("No sources.")
    steps = definition.get("steps") or []
    if not any(s["type"] == "extract" for s in steps):
        reasons.append("No extract step.")
    gaps = design_gaps(definition)
    if gaps:
        reasons.append(f"No step produces {', '.join(f'`{g}`' for g in gaps)}: the pipeline needs more steps.")
    pending = [s["id"] for s in steps if s.get("status") != "approved"]
    if pending:
        reasons.append(f"Steps not approved: {', '.join(pending)}.")
    last = state.get("last_run") or {}
    if not last:
        reasons.append("The pipeline has not been run.")
    elif last.get("upto"):
        reasons.append(f"The last run was a preview up to {last['upto']}: run the whole pipeline.")
    elif last.get("definition_hash") != definition_hash(definition):
        reasons.append("The pipeline changed since the last run: run it again.")
    elif (last.get("review") or {}).get("published"):
        pub = last["review"]["published"]
        reasons.append(f"This output is already published as version {pub['dataset_version']}. "
                       "Run the pipeline again to pick up changes.")
    else:
        if last.get("errors"):
            reasons.append("The last run had errors.")
        cov = last.get("coverage") or {}
        if not cov.get("records"):
            reasons.append("The last run produced no records.")
        for name in cov.get("blocking_gaps") or []:
            reasons.append(f"Required field `{name}` is blank on {cov['fields'][name]['missing']} record(s): "
                           "add a source or answer that fills it, or accept blanks for it.")
    return reasons


# ── Runner ───────────────────────────────────────────────────────────────────

def run(kind: str, definition: dict, state: dict, upto: str | None = None, fresh: bool = False,
        context: str = "", progress: Callable[[str], None] | None = None) -> dict:
    """
    Run the pipeline (up to and including step `upto`, or all of it).

    Mutates and returns `state`: source metadata, each step's output (records
    capped at PREVIEW_RECORDS), warnings and errors, coverage, and the model
    call cache. A step that fails stops the run there; earlier outputs stay.
    fresh refetches sources and ignores the model cache, for a real re-run.
    """
    fields = (definition.get("schema") or {}).get("fields") or []
    sources = definition.get("sources") or []
    steps = definition.get("steps") or []
    cache = {} if fresh else dict(state.get("cache") or {})
    lock = threading.Lock()

    def say(msg: str) -> None:   # steps report progress from worker threads too
        if progress:
            with lock:
                progress(msg)

    fetched, src_state = {}, {}
    say(f"Reading {len(sources)} source(s)")
    for s, got in zip(sources, _parallel(lambda s: fetch_source(s, fresh=fresh), sources)):
        if isinstance(got, Exception):
            src_state[s["id"]] = {"error": str(got)}
        else:
            fetched[s["id"]] = got
            src_state[s["id"]] = source_meta(got)
    state["sources"] = src_state

    records: list[dict] = []
    outputs, errors = {}, []
    if not fields:
        errors.append("No target schema yet.")
    for step in steps if fields else []:
        say(f"Running {step['id']} ({step['type']})")
        try:
            cfg = validate_step(kind, step["type"], step["config"], fields)
            step = {**step, "config": cfg}
            if step["type"] == "extract":
                new, warnings = _extract(step, sources, fetched, fields, context, cache, say)
                records = records + new
            elif step["type"] == "research":
                warnings = _research(step, records, fields, cache, context, say)
            elif step["type"] == "llm_fill":
                warnings = _model_fill(step, records, fields, cache, step["config"]["instruction"], "llm_fill")
            elif step["type"] == "geo_tie":
                warnings = _model_fill(step, records, fields, cache, geo_instruction(step["config"]), "geo_tie")
            else:
                records, warnings = _apply(step, records, fields)
        except Exception as e:  # noqa: BLE001 — reported on the step, run stops here
            log.warning("pipeline step %s failed: %s", step["id"], e)
            errors.append(f"{step['id']} ({step['type']}): {e}")
            outputs[step["id"]] = {"error": str(e), "count": len(records)}
            break
        records = json.loads(json.dumps(records, default=str))  # snapshot, so later steps don't rewrite this one
        outputs[step["id"]] = {"count": len(records), "warnings": warnings,
                               "records": records[:PREVIEW_RECORDS], "coverage": coverage(records, fields)}
        if upto and step["id"] == upto:
            break

    # The planner's overrides apply to the final output of a full run.
    removed, override_notes = [], []
    if upto is None and not errors:
        records, removed, override_notes = apply_edits(definition.get("overrides"), records, fields)
        records = json.loads(json.dumps(records, default=str))
        removed = json.loads(json.dumps(removed, default=str))

    # The cache only keeps entries this run used or still could, bounded.
    state["cache"] = dict(list(cache.items())[-120:])
    state["outputs"] = outputs
    state["last_run"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "upto": upto,
        "complete": upto is None and not errors and len(outputs) == len(steps),
        "definition_hash": definition_hash(definition) if upto is None else None,
        "records": records,
        "removed": removed,
        "override_notes": override_notes,
        "coverage": coverage(records, fields),
        "errors": errors + [v["error"] for k, v in src_state.items() if v.get("error")],
    }
    return state
