"""
What differs between pipeline kinds: the default target schema, the guidance
the agent gets, how the output maps onto the catalogue, and how a run is
diffed and loaded.

Funding loads into funding_programs (merged by program_key) and snapshots the
catalogue as the next Funding dataset version. The output may be any schema:
Load maps its fields onto the catalogue's columns (proposed from the field
names, confirmed by the planner, saved as definition["load_map"]). Only a
programme name is needed, because Load recognises a programme it loaded before
by its name and source. Fields mapped to no column travel with the programme
and Block Funding reads them too. Zoning is not available yet: its catalog
(use-table extraction, zone-layer lookups) is the next step.
"""

from __future__ import annotations

from datetime import datetime, timezone

from supabase import Client

from backend.agents.funding_discovery import _designation_from_scope, _program_key
from backend.pipelines import engine
from backend.repositories.datasets import DatasetRecordRepository
from backend.repositories.funding_programs import _jsonable
from backend.tools import datasets

FUNDING_COLUMNS = ("program_name", "program_type", "source_agency", "award_amount_min", "award_amount_max",
                   "deadline", "eligibility_notes", "application_url", "geo_scope")

# The catalogue columns an output field can map onto: the field types that fit
# (None: any, written as text), what Block Funding does with the column, and the
# field names proposed for it besides its own.
CATALOGUE: dict[str, dict] = {
    "program_name": {"types": None, "required": True,
                     "use": "Names the programme. Load recognises a programme it loaded before by name and source.",
                     "names": ["name", "title", "program", "programme", "program_title", "programme_name",
                               "opportunity", "opportunity_title", "grant_name", "fund_name"]},
    "source_agency": {"types": None, "use": "The body that runs it. The fit model reads it.",
                      "names": ["agency", "funding_org", "funding_organization", "funding_organisation", "organization",
                                "organisation", "funder", "administering_agency", "administrator", "sponsor", "org"]},
    "program_type": {"types": None, "use": "federal, state or local. The fit model reads it.",
                     "names": ["type", "level", "funding_level", "government_level", "program_level"]},
    "award_amount_min": {"types": ("money", "number"), "use": "Shown with each match.",
                         "names": ["min_award", "award_min", "minimum_award", "award_floor", "amount_min", "min_amount"]},
    "award_amount_max": {"types": ("money", "number"), "use": "Shown with each match.",
                         "names": ["amount", "award", "award_amount", "max_award", "award_max", "maximum_award",
                                   "award_ceiling", "amount_max", "max_amount", "funding_amount", "grant_amount"]},
    "deadline": {"types": ("date",), "use": "Orders programmes (soonest first). The fit model reads it.",
                 "names": ["due_date", "close_date", "closing_date", "application_deadline", "deadline_date", "closes"]},
    "eligibility_notes": {"types": None, "use": "Who may apply and on what: the fit model's main evidence.",
                          "names": ["eligibility", "eligible_uses", "who_can_apply", "eligible_applicants",
                                    "eligibility_criteria", "requirements"]},
    "application_url": {"types": None, "use": "Shown with each match.",
                        "names": ["url", "link", "apply_url", "application_link", "website", "program_url"]},
    "geo_scope": {"types": None, "use": "Geographic reach. The fit model reads it; tract:<designation> limits a "
                                        "programme to blocks in designated tracts. Unmapped means unknown.",
                  "names": ["geographic_scope", "geography", "reach", "geo", "geographic_reach", "scope"]},
}

FUNDING_FIELDS = [
    {"name": "program_name", "type": "text", "required": True,
     "description": "The programme's name as the source writes it."},
    {"name": "source_agency", "type": "text", "required": True,
     "description": "The body that administers the programme."},
    {"name": "program_type", "type": "enum", "required": False, "enum_values": ["federal", "state", "local"],
     "description": "federal, state or local, whichever the source indicates."},
    {"name": "award_amount_min", "type": "money", "required": False,
     "description": "Smallest award the source states."},
    {"name": "award_amount_max", "type": "money", "required": False,
     "description": "Largest award the source states."},
    {"name": "deadline", "type": "date", "required": False,
     "description": "The application closing date the source states."},
    {"name": "eligibility_notes", "type": "text", "required": False,
     "description": "Who may apply and what the money may be spent on, in the source's own terms."},
    {"name": "application_url", "type": "url", "required": False,
     "description": "Where to apply, if the source links it."},
    {"name": "geo_scope", "type": "text", "required": True, "extract": False,
     "description": "Geographic reach: federal, state:MD, county:24033, tract:<designation> or unknown."},
]

FUNDING_GUIDANCE = """This is a FUNDING pipeline. Its records are funding programmes, and Load merges them into
the funding catalogue (funding_programs, matched by programme name + source host) and saves the catalogue
as the next Funding dataset version. Block Funding analyses then assess every programme in that version
against each block's recommendations, so a programme needs at least a name, an administering agency and a
geographic reach to be useful.

The fields are the user's to name: the output can be any schema. Load maps them onto the catalogue's
columns: program_name, program_type, source_agency, award_amount_min, award_amount_max (money or number),
deadline (date), eligibility_notes, application_url, geo_scope. The mapping is proposed from the field
names (funding_org -> source_agency, amount -> award_amount_max, ...) and the user confirms or changes it
on the Output node; use set_load_mapping when the user tells you how a field should map. Only
program_name must be mapped, since Load recognises a programme by its name. Fields mapped to no column
travel with the programme and Block Funding reads them too, so a schema that maps fewer columns still
loads; it just gives the block analysis less to go on (say so when eligibility, agency or reach are
unmapped). The default funding fields map one to one.

Fields with extract=false are judgement fields: extraction leaves them blank and a model step fills them,
marked inferred. geo_scope is one (a judgement about reach, filled by a geo_tie step the user approves).
Mark any other field whose value is a judgement rather than a stated fact the same way.

Funding detail (award amounts, match share, deadlines, eligibility) usually sits on each programme's own
page or in its guidelines or notice of funding, not on the overview page that lists programmes. After the
first preview, a research step that follows each programme's links is normally worth proposing. Grants.gov can be added as a source with a short keyword query (2-5 words in a funding
agency's vocabulary, e.g. "pedestrian safety", "community garden"); prose queries return junk."""

FUNDING_INTRO = (
    "Let's build a funding pipeline. Tell me:\n"
    "1. **The goal**: what kind of funding this pipeline should collect, and for what work.\n"
    "2. **The fields** you need for each programme. Or press *Use default funding fields* on the Output node "
    "(name, agency, type, award range, deadline, eligibility, application link, geographic reach).\n"
    "3. **Sources**: paste URLs here, or upload PDFs, CSVs or text files.\n\n"
    "I'll propose the steps on the right. Approve or adjust each one, preview the output, and load it "
    "when every required field is filled. Each load becomes a new Funding version."
)

KINDS = {
    "funding": {
        "label": "Funding",
        "available": True,
        "default_fields": FUNDING_FIELDS,
        "catalogue": CATALOGUE,
        "guidance": FUNDING_GUIDANCE,
        "intro": FUNDING_INTRO,
    },
    "zoning": {
        "label": "Zoning",
        "available": False,
        "default_fields": [],
        "catalogue": {},
        "guidance": "",
        "intro": "",
    },
}


def spec(kind: str) -> dict:
    s = KINDS.get(kind)
    if not s:
        raise ValueError(f"Unknown pipeline kind {kind!r}")
    return s


# ── Funding: diff and load ───────────────────────────────────────────────────

# ── The output's fields onto the catalogue's columns ─────────────────────────

def _fits(col: str, f: dict, cat: dict) -> bool:
    types = cat[col]["types"]
    return types is None or f["type"] in types


# When no field has a column's name or a usual alternative, a field whose name
# has a word starting with one of these is proposed instead (funding_program ->
# program_name, amount_provided -> award_amount_max). Columns are tried in this
# order, so the specific ones win: fund_details is eligibility, not a name.
KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("application_url", ("url", "link", "website", "apply")),
    ("deadline", ("deadline", "due", "close", "closing", "date")),
    ("award_amount_min", ("min", "floor", "smallest")),
    ("award_amount_max", ("amount", "award", "max", "ceiling", "value", "size")),
    ("eligibility_notes", ("eligib", "detail", "description", "criteria", "requirement", "who", "purpose", "summary", "use")),
    ("source_agency", ("agency", "org", "funder", "sponsor", "administ", "department", "provider")),
    ("geo_scope", ("geo", "scope", "region", "area", "location", "jurisdiction", "reach")),
    ("program_type", ("type", "level", "category", "tier")),
    ("program_name", ("program", "programme", "title", "name", "opportunity", "grant", "fund", "scheme")),
]


def propose_load_map(fields: list[dict], cat: dict = CATALOGUE) -> dict[str, str | None]:
    """
    Each column's field by name: its own name first, then the usual
    alternatives, then a field with a matching word (KEYWORDS). A field is used once.
    """
    by_name = {f["name"]: f for f in fields}
    used: set[str] = set()
    out: dict[str, str | None] = {}
    for col, spec in cat.items():
        pick = next((n for n in [col, *spec["names"]] if n in by_name and n not in used and _fits(col, by_name[n], cat)), None)
        out[col] = pick
        if pick:
            used.add(pick)
    for f in fields:
        if f["name"] in used:
            continue
        words = f["name"].lower().split("_")
        col = next((c for c, keys in KEYWORDS if c in cat and not out.get(c) and _fits(c, f, cat)
                    and any(w.startswith(k) for w in words for k in keys)), None)
        if col:
            out[col] = f["name"]
            used.add(f["name"])
    return out


def load_map(definition: dict, cat: dict = CATALOGUE) -> dict:
    """
    The mapping in force: the planner's saved choice per column (None = no
    field), else the proposal. A saved field that left the schema, or changed to
    a type the column cannot take, is dropped and reported.
    """
    fields = (definition.get("schema") or {}).get("fields") or []
    by_name = {f["name"]: f for f in fields}
    saved = definition.get("load_map") or {}
    proposed = propose_load_map(fields, cat)
    columns, notes = [], []
    taken = {v for k, v in saved.items() if k in cat and v}
    for col, spec in cat.items():
        if col in saved:
            field, auto = saved[col], False
            if field and (field not in by_name or not _fits(col, by_name[field], cat)):
                notes.append(f"`{field}` no longer fits {col} ({'not in the schema' if field not in by_name else 'its type changed'}), so {col} is unmapped.")
                field = None
        else:
            field, auto = proposed[col], True
            if field in taken:
                field = None
        columns.append({"column": col, "field": field, "auto": auto, "required": bool(spec.get("required")),
                        "types": list(spec["types"]) if spec["types"] else None, "use": spec["use"]})
    mapped = {c["field"] for c in columns if c["field"]}
    return {"columns": columns, "notes": notes, "extras": [f["name"] for f in fields if f["name"] not in mapped]}


def clean_load_map(mapping: dict, fields: list[dict], cat: dict = CATALOGUE) -> dict[str, str | None]:
    """A planner's mapping {column: field or None}, checked. Raises engine.PipelineError."""
    by_name = {f["name"]: f for f in fields}
    out: dict[str, str | None] = {}
    for col, field in (mapping or {}).items():
        if col not in cat:
            raise engine.PipelineError(f"{col} is not a catalogue column; they are {', '.join(cat)}.")
        field = (field or "").strip() or None
        if field:
            if field not in by_name:
                raise engine.PipelineError(f"No field `{field}` in the schema.")
            if not _fits(col, by_name[field], cat):
                raise engine.PipelineError(f"{col} takes a {' or '.join(cat[col]['types'])} field; `{field}` is {by_name[field]['type']}. "
                                           "Parse it, or change its type, first.")
        out[col] = field
    dup = sorted({f for f in out.values() if f and list(out.values()).count(f) > 1})
    if dup:
        raise engine.PipelineError(f"`{dup[0]}` is mapped to more than one column; map each field once.")
    return out


def load_problems(kind: str, definition: dict, records: list[dict] | None = None) -> list[str]:
    """Why Load cannot map this output (and the last run's records) onto the catalogue. Empty when it can."""
    cat = spec(kind)["catalogue"]
    if not cat or not (definition.get("schema") or {}).get("fields"):
        return []
    m = load_map(definition, cat)
    out = [f"Map a field to `{c['column']}` on the Output node: {c['use']}"
           for c in m["columns"] if c["required"] and not c["field"]]
    for c in m["columns"]:
        if c["required"] and c["field"] and records:
            blank = sum(1 for r in records if engine._blank(r["values"].get(c["field"])))
            if blank:
                out.append(f"{blank} record(s) have no `{c['field']}`, which Load uses as `{c['column']}`: "
                           "fill it, or remove those records on the output.")
    return out


def _column_value(col: str, f: dict, v):
    """A field's value as the catalogue column holds it: numbers and dates typed, the rest as text."""
    if engine._blank(v):
        return None
    types = CATALOGUE[col]["types"]
    if types == ("date",):
        return engine.parse_date(v)
    if types:
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else engine.parse_money(v)
    if isinstance(v, (dict, list)):
        return engine._sql_text(v)
    return str(v) if not isinstance(v, str) else v


def _funding_row(pipeline: dict, rec: dict, fields: list[dict]) -> dict:
    v = rec["values"]
    url = rec.get("source_url") or ""
    by_name = {f["name"]: f for f in fields}
    cols = {c["column"]: c["field"] for c in load_map(pipeline["definition"])["columns"] if c["field"]}
    vals = {col: _column_value(col, by_name[field], v.get(field)) for col, field in cols.items()}
    geo = vals.get("geo_scope") or "unknown"
    row = {k: vals.get(k) for k in FUNDING_COLUMNS if k in cols}
    row.update({
        "program_key": _program_key(vals.get("program_name") or "", url),
        "geo_scope": geo,
        "requires_tract_designation": _designation_from_scope(geo),
        "source_name": f"Pipeline “{pipeline['name']}” from {url}",
        "source_url": url,
        "source_vintage": datetime.now(timezone.utc).date().isoformat(),
        "is_extracted": True,
        "extraction_confidence": rec.get("confidence"),
        "source_excerpt": rec.get("excerpt"),
        "reviewed_by_user": True,
        "discovery_thread_id": f"pipeline-{pipeline['id']}",
        "raw_data": {
            "pipeline_id": pipeline["id"], "record_id": rec["id"],
            "excerpt_verified": rec.get("excerpt_verified"), "provenance": rec.get("prov"),
            "analyst_note": rec.get("analyst_note"), "conflicts": rec.get("conflicts") or None,
            "research": rec.get("research") or None,
            # Fields mapped to no column; Block Funding reads them with the programme.
            "fields": {k: val for k, val in v.items() if k not in cols.values() and not engine._blank(val)},
        },
    })
    return row


def funding_diff(db: Client, pipeline: dict, records: list[dict], fields: list[dict]) -> dict:
    """What loading these records would change against the latest Funding version."""
    latest = datasets.resolve(db, "funding")
    current = {}
    if latest:
        current = {r["record_key"]: r["data"] for r in DatasetRecordRepository(db).for_version(latest["id"], "program")}
    added, changed, unchanged = [], [], []
    for rec in records:
        row = _funding_row(pipeline, rec, fields)
        old = current.get(row["program_key"])
        if not old:
            added.append({"program_key": row["program_key"], "program_name": row.get("program_name")})
            continue
        diffs = [k for k in FUNDING_COLUMNS if k in row and str(row.get(k) or "") != str(old.get(k) or "")]
        (changed if diffs else unchanged).append(
            {"program_key": row["program_key"], "program_name": row.get("program_name"), "fields": diffs})
    return {"base_version": datasets.label(latest), "added": added, "changed": changed,
            "unchanged": len(unchanged), "catalogue_size": len(current),
            "note": "Loading merges into the catalogue; programmes not in this run are kept."}


def load_funding(db: Client, pipeline: dict, records: list[dict], fields: list[dict], name: str | None,
                 created_by: str | None, definition_version: int) -> dict:
    rows = [_jsonable(_funding_row(pipeline, r, fields)) for r in records]
    # The same programme twice in one run would make the batch upsert fail; keep the first.
    seen, unique = set(), []
    for r in rows:
        if r["program_key"] not in seen:
            seen.add(r["program_key"])
            unique.append(r)
    if unique:
        db.table("funding_programs").upsert(unique, on_conflict="program_key").execute()
    version = datasets.snapshot_funding(
        db, name=name, note=f"Pipeline “{pipeline['name']}” def v{definition_version}",
        source={"type": "pipeline", "pipeline_id": pipeline["id"], "definition_version": definition_version,
                "programs_written": len(unique)},
        created_by=created_by)
    return {"written": len(unique), "dataset_version": version}


def diff(kind: str, db: Client, pipeline: dict, records: list[dict], fields: list[dict]) -> dict:
    if kind == "funding":
        return funding_diff(db, pipeline, records, fields)
    raise ValueError(f"{kind} pipelines cannot be loaded yet")


def load(kind: str, db: Client, pipeline: dict, records: list[dict], fields: list[dict], name: str | None,
         created_by: str | None, definition_version: int) -> dict:
    if kind == "funding":
        return load_funding(db, pipeline, records, fields, name, created_by, definition_version)
    raise ValueError(f"{kind} pipelines cannot be loaded yet")
