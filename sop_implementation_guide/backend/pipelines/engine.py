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
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from backend.tools import grants_gov, web_source
from backend.tools.llm_extractor import structured_call

log = logging.getLogger(__name__)

FIELD_TYPES = ("text", "number", "money", "percent", "date", "url", "enum", "boolean")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
SOURCE_CHARS = 12_000        # per extraction call: a long source is read in chunks of this size
FETCH_CHARS = 60_000         # how much of one source is fetched (5 chunks at most)
PREVIEW_RECORDS = 50         # records kept per step output in state


class PipelineError(ValueError):
    """A definition, step config or source the engine cannot accept. The message is user-facing."""


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
        "config": {"field": "optional str, default geo_scope"},
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


def _set(rec: dict, field: str, value: Any, kind: str, step_id: str, note: str | None = None) -> None:
    rec["values"][field] = value
    if _blank(value):
        rec["prov"].pop(field, None)
    else:
        rec["prov"][field] = {"kind": kind, "step": step_id, **({"note": note} if note else {})}


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


EXTRACT_PROMPT = """Extract records from the source below for a city planner's data pipeline.

PIPELINE GOAL / CONTEXT:
{context}

HOW TO READ THIS SOURCE:
{instructions}

SOURCE: {url}
---
{text}
---

Rules that matter more than completeness:
- One record per distinct item the goal is about. If the source contains none, return an empty list.
- Fill a field only when the text supports it. Leave it null rather than guess; a blank the planner
  fills in is fine, an invented value is not.
- `source_excerpt` must be a verbatim span (at least one full sentence) copied from the source text
  that supports the record. It is checked against the text.
- `confidence` is 0-1: how well the text supports the values."""


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


def _extract(step: dict, sources: list[dict], fetched: dict[str, dict], fields: list[dict],
             context: str, cache: dict) -> tuple[list[dict], list[str]]:
    ids = step["config"].get("source_ids") or [s["id"] for s in sources]
    readable = [f for f in fields if f.get("extract", True)]
    record_schema = _obj({**{f["name"]: _json_type(f) for f in readable},
                          "source_excerpt": {"type": "string", "description": "Verbatim span from the source."},
                          "confidence": {"type": "number", "description": "0-1."}})
    schema = _obj({"records": {"type": "array", "items": record_schema}})
    records, warnings = [], []
    for src in sources:
        if src["id"] not in ids or src["id"] not in fetched:
            continue
        doc = fetched[src["id"]]
        raws = []
        parts = _chunks(doc.get("text") or "")
        for text in parts:
            key = _hash(["extract", step["config"].get("instructions"), readable, text, context])
            if key not in cache:
                result = structured_call(EXTRACT_PROMPT.format(
                    context=context or "(none given)",
                    instructions=step["config"].get("instructions") or "(no special instructions)",
                    url=doc.get("final_url") or src.get("url"), text=text), schema, max_tokens=8192)
                cache[key] = result["records"]
            raws += cache[key]
        label = src.get("label") or src["id"]
        if len(parts) > 1:
            warnings.append(f"{label}: read in {len(parts)} parts; an item split across parts may appear twice "
                            "(add a dedupe step).")
        if doc.get("truncated"):
            warnings.append(f"{label}: longer than {FETCH_CHARS:,} characters; the rest was not read.")
        for n, raw in enumerate(raws, 1):
            rec = {"id": f"{src['id']}-{n}", "values": {}, "prov": {},
                   "source_id": src["id"], "source_url": doc.get("final_url") or src.get("url"),
                   "excerpt": raw.get("source_excerpt"), "confidence": raw.get("confidence"),
                   "excerpt_verified": web_source.excerpt_is_genuine(raw.get("source_excerpt"), [doc])}
            for f in fields:
                value, warn = _normalise(f, raw.get(f["name"])) if f.get("extract", True) else (None, None)
                _set(rec, f["name"], value, "extracted", step["id"], warn)
            records.append(rec)
    unverified = [r["id"] for r in records if not r["excerpt_verified"]]
    if unverified:
        warnings.append(f"{len(unverified)} record(s) quote an excerpt not found in the source: treat as unconfirmed.")
    return records, warnings


FILL_PROMPT = """For each record below, set the field `{field}` ({type_desc}).

INSTRUCTION FROM THE PLANNER: {instruction}

Use only what each record's own excerpt and values support. If they do not support a value,
return null for that record. Give a one-line reason for each.

RECORDS:
{records}"""

GEO_INSTRUCTION = """Recommend each programme's geographic reach. The planner works on city blocks in Bowie,
Prince George's County, Maryland (county FIPS 24033). Use one of: federal (available nationally),
state:MD (Maryland only), county:24033 (Prince George's County only), tract:NMTC (only in tracts
carrying a designation; name it after 'tract:'), unknown (the source does not say). A programme run
by a state agency is state-scoped even when the money is federal. Base it on what the source says."""


def _model_fill(step: dict, records: list[dict], fields: list[dict], cache: dict,
                instruction: str, kind_label: str) -> list[str]:
    f = _field(fields, step["config"]["field"])
    only_blank = step["config"].get("only_if_blank", True)
    targets = [r for r in records if not (only_blank and not _blank(r["values"].get(f["name"])))]
    if not targets:
        return []
    digest = [{"id": r["id"], "values": r["values"], "excerpt": (r.get("excerpt") or "")[:800],
               "source_url": r.get("source_url")} for r in targets]
    schema = _obj({"results": {"type": "array", "items": _obj({
        "id": {"type": "string"}, "value": _json_type(f), "reason": {"type": "string"}})}})
    key = _hash([kind_label, instruction, f, digest])
    if key not in cache:
        result = structured_call(FILL_PROMPT.format(
            field=f["name"], type_desc=_json_type(f)["description"], instruction=instruction,
            records=json.dumps(digest, indent=1, default=str)), schema, max_tokens=8192)
        cache[key] = result["results"]
    by_id = {r["id"]: r for r in cache[key]}
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


def definition_hash(definition: dict) -> str:
    """What a run's results depend on. Approval status and notes are excluded."""
    schema = definition.get("schema") or {}
    return _hash({
        "fields": [{k: f.get(k) for k in ("name", "type", "required", "enum_values", "extract")}
                   for f in schema.get("fields", [])],
        "sources": [{k: s.get(k) for k in ("id", "kind", "url", "query")} for s in definition.get("sources", [])],
        "steps": [{k: s.get(k) for k in ("id", "type", "config")} for s in definition.get("steps", [])],
    })


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
    say = progress or (lambda _m: None)

    fetched, src_state = {}, {}
    for s in sources:
        say(f"Reading {s.get('label') or s['id']}")
        try:
            fetched[s["id"]] = fetch_source(s, fresh=fresh)
            src_state[s["id"]] = source_meta(fetched[s["id"]])
        except PipelineError as e:
            src_state[s["id"]] = {"error": str(e)}
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
                new, warnings = _extract(step, sources, fetched, fields, context, cache)
                records = records + new
            elif step["type"] == "llm_fill":
                warnings = _model_fill(step, records, fields, cache, step["config"]["instruction"], "llm_fill")
            elif step["type"] == "geo_tie":
                warnings = _model_fill(step, records, fields, cache, GEO_INSTRUCTION, "geo_tie")
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

    # The cache only keeps entries this run used or still could, bounded.
    state["cache"] = dict(list(cache.items())[-40:])
    state["outputs"] = outputs
    state["last_run"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "upto": upto,
        "complete": upto is None and not errors and len(outputs) == len(steps),
        "definition_hash": definition_hash(definition) if upto is None else None,
        "records": records,
        "coverage": coverage(records, fields),
        "errors": errors + [v["error"] for k, v in src_state.items() if v.get("error")],
    }
    return state
