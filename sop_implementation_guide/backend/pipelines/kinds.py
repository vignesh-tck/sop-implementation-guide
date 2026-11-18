"""
What differs between pipeline kinds: the default target schema, the guidance
the agent gets, the fields Load needs, and how a run is diffed and loaded.

Funding loads into funding_programs (merged by program_key) and snapshots the
catalogue as the next Funding dataset version. Zoning is not available yet:
its catalog (use-table extraction, zone-layer lookups) is the next step.
"""

from __future__ import annotations

from datetime import datetime, timezone

from supabase import Client

from backend.agents.funding_discovery import _designation_from_scope, _program_key
from backend.repositories.datasets import DatasetRecordRepository
from backend.repositories.funding_programs import _jsonable
from backend.tools import datasets

FUNDING_COLUMNS = ("program_name", "program_type", "source_agency", "award_amount_min", "award_amount_max",
                   "deadline", "eligibility_notes", "application_url", "geo_scope")

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

Fields the app maps onto catalogue columns: program_name (required by Load), program_type, source_agency,
award_amount_min, award_amount_max, deadline, eligibility_notes, application_url, geo_scope. Any other
field the user wants is kept with the programme as an extra attribute.

Fields with extract=false are judgement fields: extraction leaves them blank and a model step fills them,
marked inferred. geo_scope is one (a judgement about reach, filled by a geo_tie step the user approves).
Mark any other field whose value is a judgement rather than a stated fact the same way. Grants.gov can be added as a source with a short keyword query (2-5 words in a funding
agency's vocabulary, e.g. "pedestrian safety", "community garden"); prose queries return junk."""

FUNDING_INTRO = (
    "Let's build a funding pipeline. Tell me:\n"
    "1. **The goal**: what kind of funding this pipeline should collect, and for what work.\n"
    "2. **The fields** you need for each programme. Or press *Use default funding fields* in the panel "
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
        "required_fields": ["program_name"],
        "guidance": FUNDING_GUIDANCE,
        "intro": FUNDING_INTRO,
    },
    "zoning": {
        "label": "Zoning",
        "available": False,
        "default_fields": [],
        "required_fields": [],
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

def _funding_row(pipeline: dict, rec: dict, fields: list[dict]) -> dict:
    v = rec["values"]
    url = rec.get("source_url") or ""
    geo = v.get("geo_scope") or "unknown"
    names = {f["name"] for f in fields}
    row = {k: v.get(k) for k in FUNDING_COLUMNS if k in names}
    row.update({
        "program_key": _program_key(v.get("program_name") or "", url),
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
            "fields": {k: val for k, val in v.items() if k not in FUNDING_COLUMNS},
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
