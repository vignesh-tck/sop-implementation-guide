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
and Block Funding reads them too.

Zoning has three targets (block zones, use permissions, recommendation
readings), one output each, and publishes them together as one Zoning version
(zoning_load.py). Its steps look things up rather than ask a model
(zoning_steps.py, imported here so they are registered with the engine).

General has a free schema and writes each output to a table of its own
(general.py, also imported here to register its step and table provider).
"""

from __future__ import annotations

from datetime import datetime, timezone

from supabase import Client

from backend.pipelines import engine, general, zoning_load, zoning_steps  # noqa: F401 — they register their steps
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

ZONING_GUIDANCE = """This is a ZONING pipeline. A Zoning version is one bundle with three parts, and each part
this pipeline refreshes is one OUTPUT with its own schema (add_output adds one; each target takes one output):
  - block_zones: one record per study block: lookup_status (found / no_zone / failed), zones, prior_zones, cases,
    overlays. Built with from_table (table blocks) then spatial_lookup steps on an arcgis source: zones from
    layer 59 field CLASS with status_field lookup_status; prior zones from layer 65 CLASS; cases from layers 26,
    46, 29, 31 field CASE_NUM; overlays from layers 14, 22, 68, 23, 19, 20, 21 field *. The default source is
    https://gisdata.pgplanning.org/arcgis/rest/services/Applications/ZoningCertificationLetter/MapServer.
    Do NOT use Map_Services/C_I_Z layer 6 ("Plan 2035 Zoning"): it is pre-2022 zoning clipped to centers.
  - use_permissions: one record per ordinance use, with zone_<code> fields holding P (by right), SE (special
    exception), X (prohibited) or A. Built with a use_table step on the enCodePlus url source
    https://online.encodeplus.com/regs/princegeorgescounty-md/doc-view.aspx?secid=649 (the doc-viewer#secid
    link also works). Do not extract the use table with a model: use_table reads it exactly and checks the
    zone columns. An amendment not yet in enCodePlus (a CB bill PDF) can be extracted and joined in SQL.
  - readings: one record per SoP recommendation type: governed_by (use_table, not_a_land_use, unknown),
    ordinance_uses (use-table rows, ' | '-separated because use names contain semicolons, named exactly as the
    use table does), mode, confidence,
    note. Start from from_table zoning_readings (the readings in force) and recommendation_types (every
    label on the blocks); join them in SQL. Street and right-of-way items (benches, crosswalks, signals,
    sidewalks, lanes) are not_a_land_use: findings then say zoning does not decide them and ask no one.
    Any other new reading is a judgement: fill it with llm_fill (marked inferred, confidence low unless the
    planner says otherwise) and say which ones the SME should confirm.
Working with several outputs: the first output (id "output") is Block zones when the pipeline is created.
If the planner wants other parts, set the first output's target with update_output (before its schema is
approved) and add the rest with add_output. Every output must be fed by steps; remove one that is not wanted. Each output's schema is approved on its
own Output node, and all of them before the sources can be marked final. set_schema, set_load_mapping and
run_sql take output=...; add_step's output=... says which output a step goes to the end of (and whose
schema its config uses); connect_steps can wire a step into output, o2 or o3. Each branch feeds one
output: a step's output still goes to one place. The state lists every output under outputs.
Publishing merges every output into the zoning data in force and snapshots it as the next Zoning version;
parts without an output, and blocks, uses or readings an output does not list, are kept. Determine, don't
infer: zones come from the map, permissions from the ordinance; only readings are judgement. A failed map
lookup is left blank and marked failed, never treated as no zone."""

ZONING_INTRO = (
    "Let's build a zoning pipeline. A Zoning version has three parts, and this pipeline can refresh any of them, "
    "each as its own output:\n"
    "1. **Block zones**: which zones, prior zones, cases and overlays each block fronts, from the county map.\n"
    "2. **Use permissions**: what each zone allows for each use, from the ordinance's use tables.\n"
    "3. **Recommendation readings**: which ordinance use each SoP recommendation means, or that it is not a land use.\n\n"
    "Tell me which parts you want to refresh and from what sources (the county GIS service and the enCodePlus use "
    "table are the usual ones). I'll set up the outputs and propose the steps. Nothing is published until you "
    "approve it, and each publish becomes a new Zoning version."
)

KINDS = {
    "funding": {
        "label": "Funding",
        "available": True,
        "guidance": FUNDING_GUIDANCE,
        "intro": FUNDING_INTRO,
        # What an output can load into. A funding pipeline has one output, loading into the catalogue.
        "targets": {
            "programs": {"label": "Funding programmes", "default_fields": FUNDING_FIELDS, "catalogue": CATALOGUE},
        },
    },
    "zoning": {
        "label": "Zoning",
        "available": True,
        "guidance": ZONING_GUIDANCE,
        "intro": ZONING_INTRO,
        "targets": zoning_load.TARGETS,
    },
    "general": {
        "label": "General",
        "available": True,
        "guidance": general.GUIDANCE,
        "intro": general.INTRO,
        "targets": general.TARGETS,
        # Several outputs may load into the one target, each its own table; a pipeline may read tables only.
        "many_outputs": True,
        "sources_optional": True,
        "source_kinds": ["url"],
    },
}


def spec(kind: str) -> dict:
    s = KINDS.get(kind)
    if not s:
        raise ValueError(f"Unknown pipeline kind {kind!r}")
    return s


def target_id(kind: str, definition: dict) -> str | None:
    """What an output loads into: its saved target, else the kind's first. definition: an output's view (outputs.project)."""
    targets = spec(kind)["targets"]
    t = definition.get("target")
    return t if t in targets else next(iter(targets), None)


def target_spec(kind: str, definition: dict) -> dict:
    """The target an output loads into: {label, default_fields, catalogue}. Empty parts when the kind has none yet."""
    t = target_id(kind, definition)
    return {"id": t, **spec(kind)["targets"][t]} if t else {"id": None, "label": spec(kind)["label"],
                                                             "default_fields": [], "catalogue": {}}


def default_fields(kind: str, definition: dict, db: Client | None = None) -> list[dict]:
    """A target's default fields. Some depend on the data (the zones the study blocks front)."""
    f = target_spec(kind, definition)["default_fields"]
    return f(db) if callable(f) else f


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
    cat = target_spec(kind, definition)["catalogue"]
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


def _program_key(name: str, source_url: str) -> str:
    """Stable key so re-extracting the same programme updates rather than duplicates."""
    slug = "-".join(("".join(c if c.isalnum() else " " for c in name.lower())).split())[:60]
    host = (source_url.split("//")[-1].split("/")[0] or "extracted").replace("www.", "")
    return f"{host}:{slug}" if slug else f"{host}:unnamed"


def _designation_from_scope(scope: str | None) -> str | None:
    """'tract:NMTC' means eligibility depends on a tract designation."""
    if scope and scope.startswith("tract:"):
        return scope.split(":", 1)[1] or None
    return None


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


# ── Diff and load, per kind ──────────────────────────────────────────────────
#
# A publish hands over one part per output: {"output", "target", "definition"
# (the output's view), "records", "fields"}. Funding has one part.

def _with_columns(kind: str, parts: list[dict]) -> list[dict]:
    """Each part with its {column: field} mapping in force."""
    out = []
    for part in parts:
        cat = spec(kind)["targets"][part["target"]]["catalogue"]
        cols = {c["column"]: c["field"] for c in load_map(part["definition"], cat)["columns"] if c["field"]}
        out.append({**part, "columns": cols})
    return out


def diff(kind: str, db: Client, pipeline: dict, parts: list[dict]) -> dict:
    if kind == "funding":
        part = parts[0]
        return funding_diff(db, {**pipeline, "definition": part["definition"]}, part["records"], part["fields"])
    if kind == "zoning":
        return zoning_load.diff(db, pipeline, _with_columns(kind, parts))
    if kind == "general":
        return general.diff(db, pipeline, parts)
    raise ValueError(f"{kind} pipelines cannot be loaded yet")


def load(kind: str, db: Client, pipeline: dict, parts: list[dict], name: str | None,
         created_by: str | None, definition_version: int) -> dict:
    if kind == "funding":
        part = parts[0]
        return load_funding(db, {**pipeline, "definition": part["definition"]}, part["records"], part["fields"],
                            name, created_by, definition_version)
    if kind == "zoning":
        return zoning_load.load(db, pipeline, _with_columns(kind, parts), name, created_by, definition_version)
    if kind == "general":
        return general.load(db, pipeline, parts, name, created_by, definition_version)
    raise ValueError(f"{kind} pipelines cannot be loaded yet")
