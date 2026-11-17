"""
Pathway templates — the editable steps every implementation plan starts from.

The Pathways tab reads and saves these. A save never overwrites: it adds a new
version to pathway_templates, so a bad edit can be undone by restoring an older
version (version 0 is backend/data/pathways.json). See backend/tools/pathways.py
for how the templates in force are chosen.

Plans nobody has saved yet follow the templates live. A saved plan keeps its own
steps, so a template edit never changes a plan a reviewer has already worked on.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import Client

from backend.db.session import get_supabase
from backend.repositories.pathway_templates import PathwayTemplateRepository, TemplateTableMissing
from backend.repositories.recommendations import BlockRepository
from backend.tools.pathways import load_file_templates, load_templates, normalize_templates

router = APIRouter(prefix="/pathways", tags=["pathways"])

MISSING_TABLE = (
    "Template edits cannot be saved yet: the pathway_templates table does not exist. "
    "Paste the PATHWAY TEMPLATES section of backend/db/schema.sql into the "
    "Supabase SQL editor and run it."
)


class TemplatesUpdate(BaseModel):
    pathways: list[dict]
    rec_types: dict[str, str]
    status: Optional[str] = None
    updated_by: str
    note: Optional[str] = None
    # The version the editor started from. A save against an older version is
    # refused, so two reviewers editing at once can't silently undo each other.
    base_version: Optional[str] = None


class StepsUpdate(BaseModel):
    steps: list[dict]
    updated_by: str
    note: Optional[str] = None


class Restore(BaseModel):
    updated_by: str


def _reviewer(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Saving a template needs your name.")
    return name


def _response(db: Client) -> dict:
    templates = load_templates(db)
    history = []
    if templates["storage_ready"]:
        try:
            history = PathwayTemplateRepository(db).history()
        except TemplateTableMissing:
            pass
    known = BlockRepository(db).distinct_rec_labels()
    return {
        **templates,
        "history": history,
        "unmapped_rec_labels": [l for l in known if l not in templates["rec_types"]],
    }


def _save(db: Client, current: dict, doc: dict, updated_by: str, note: str | None) -> dict:
    if not current["storage_ready"]:
        raise HTTPException(status_code=503, detail=MISSING_TABLE)
    clean, errors = normalize_templates(doc, current["filled_by"])
    if errors:
        raise HTTPException(status_code=422, detail=" ".join(errors))
    try:
        PathwayTemplateRepository(db).insert({
            "version": current["version_number"] + 1,
            "doc": clean,
            "updated_by": updated_by,
            "note": (note or "").strip() or None,
        })
    except TemplateTableMissing:
        raise HTTPException(status_code=503, detail=MISSING_TABLE)
    return _response(db)


@router.get("")
def get_pathways(db: Client = Depends(get_supabase)):
    """
    The templates in force, their version history, and any recommendation types
    in the data that no pathway covers yet.
    """
    return _response(db)


@router.put("")
def save_pathways(body: TemplatesUpdate, db: Client = Depends(get_supabase)):
    """Save the whole template set as a new version."""
    updated_by = _reviewer(body.updated_by)
    current = load_templates(db)
    if body.base_version and body.base_version != current["version"]:
        raise HTTPException(
            status_code=409,
            detail=f"Someone saved {current['version']} while you were editing "
                   f"{body.base_version}. Reload to see their changes, then edit again.",
        )
    doc = {"status": body.status, "pathways": body.pathways, "rec_types": body.rec_types}
    return _save(db, current, doc, updated_by, body.note)


@router.put("/{key}/steps")
def save_pathway_steps(key: str, body: StepsUpdate, db: Client = Depends(get_supabase)):
    """
    Replace one pathway's steps, for example with a reviewer's finalised plan,
    so the next block starts from what the SME settled on.
    """
    updated_by = _reviewer(body.updated_by)
    current = load_templates(db)
    if not any(p["key"] == key for p in current["pathways"]):
        raise HTTPException(status_code=404, detail=f"No pathway '{key}'")
    pathways = [{**p, "steps": body.steps} if p["key"] == key else p for p in current["pathways"]]
    doc = {"status": current.get("status"), "pathways": pathways, "rec_types": current["rec_types"]}
    return _save(db, current, doc, updated_by, body.note)


@router.post("/versions/{version}/restore")
def restore_version(version: int, body: Restore, db: Client = Depends(get_supabase)):
    """Make an earlier version current again, by saving a copy of it as the newest."""
    updated_by = _reviewer(body.updated_by)
    current = load_templates(db)
    if version == 0:
        doc = load_file_templates()
        label = "the original file (v0)"
    else:
        try:
            row = PathwayTemplateRepository(db).get_version(version)
        except TemplateTableMissing:
            raise HTTPException(status_code=503, detail=MISSING_TABLE)
        if not row:
            raise HTTPException(status_code=404, detail=f"No template version v{version}")
        doc = row["doc"]
        label = f"v{version}"
    return _save(db, current, doc, updated_by, f"Restored {label}")
