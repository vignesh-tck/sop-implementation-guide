"""
Implementation pathways — turn one recommendation into an ordered plan of steps.

The 65 recommendation types collapse into six pathways (building and façade, land
use, parks, traffic control, streetscape, road and curb), each a template of the
steps a city follows to get that kind of change done. The templates are data, not
Python branching. They start as backend/data/pathways.json (version v0) and are
edited in the app's Pathways tab, which saves each edit as a new version in the
pathway_templates table. The latest saved version wins, so the file serves as
the seed and the fallback. A host's disk gets wiped on redeploy, so edits can't
be written back to the file. Templates are read on every call, so an edit shows
up without a restart.

A plan is the template plus whatever this system can attach to it. Each step's
`filled_by` names the source meant to supply it: the template itself, an agent
(funding, zoning, policy) or a lookup (parcel, road owner, program terms). The
`live` flag on each source says whether it actually supplies anything yet. Today
only the funding agent does: its step gets the approved funding signals for that
exact recommendation. A step whose source is not live is labelled as needing the
SME, so the reviewer can tell findings from blanks they have to fill.

Funding is re-attached on every read, including for a plan someone already saved.
The saved plan holds the reviewer's wording of the steps; the money is live, so a
signal approved after the plan was finalised still appears on it.
"""

import json
import re
from pathlib import Path

from supabase import Client

from backend.repositories.pathway_templates import PathwayTemplateRepository, TemplateTableMissing

TEMPLATES_PATH = Path(__file__).resolve().parent.parent / "data" / "pathways.json"

# The step fields a reviewer can set. Anything else in a saved step (attached
# funding, computed status) is derived on read and never stored.
STEP_FIELDS = ("title", "owner", "waits_on", "time", "filled_by", "note", "edited")

# A template step has no `edited` flag: it is what every plan starts from.
TEMPLATE_STEP_FIELDS = ("title", "owner", "waits_on", "time", "filled_by", "note")

PATHWAY_FIELDS = ("key", "name", "who", "branch")

KEY_PATTERN = re.compile(r"^[a-z0-9_]+$")


def load_file_templates() -> dict:
    with open(TEMPLATES_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_templates(db: Client | None = None) -> dict:
    """
    The templates in force, plus where they came from.

    The result has the shape of pathways.json, with these keys added: `source`
    ('app' or 'file'), `storage_ready` (False while the table doesn't exist),
    `updated_by` and `version_number` (0 for the file).
    """
    file_doc = load_file_templates()
    file_doc.update(source="file", storage_ready=db is not None, updated_by=None, version_number=0)
    if db is None:
        return file_doc
    try:
        row = PathwayTemplateRepository(db).latest()
    except TemplateTableMissing:
        file_doc["storage_ready"] = False
        return file_doc
    if not row:
        return file_doc
    doc = row["doc"]
    return {
        "version": f"v{row['version']}",
        "version_number": row["version"],
        "updated": (row.get("created_at") or "")[:10],
        "updated_by": row.get("updated_by"),
        "status": doc.get("status") or "",
        # Always from the file: these describe what the code can automate.
        "filled_by": file_doc["filled_by"],
        "pathways": doc.get("pathways") or [],
        "rec_types": doc.get("rec_types") or {},
        "source": "app",
        "storage_ready": True,
    }


def pathway_for(rec_label: str | None, templates: dict | None = None) -> dict | None:
    """The pathway a recommendation type belongs to, or None if it is unmapped."""
    templates = templates or load_templates()
    key = templates["rec_types"].get((rec_label or "").strip())
    return next((p for p in templates["pathways"] if p["key"] == key), None)


def clean_template_step(step: dict) -> dict:
    out = {}
    for k in TEMPLATE_STEP_FIELDS:
        v = step.get(k)
        if isinstance(v, str):
            v = v.strip()
        if v:
            out[k] = v
    out.setdefault("title", "")
    out.setdefault("filled_by", "template")
    for k in ("owner", "waits_on", "time"):
        out.setdefault(k, "")
    return out


def normalize_templates(doc: dict, sources: dict) -> tuple[dict, list[str]]:
    """
    Tidy an edited template set, and list what is wrong with it.

    Returns the doc to store ({status, pathways, rec_types}) and the errors.
    The doc is only saved when the errors list is empty.
    """
    errors: list[str] = []
    pathways = []
    seen: set[str] = set()
    for i, p in enumerate(doc.get("pathways") or [], start=1):
        key = (p.get("key") or "").strip()
        name = (p.get("name") or "").strip()
        label = name or key or f"pathway {i}"
        if not KEY_PATTERN.match(key):
            errors.append(f"'{label}': the key must be lowercase letters, digits and underscores.")
        elif key in seen:
            errors.append(f"Two pathways share the key '{key}'.")
        seen.add(key)
        if not name:
            errors.append(f"Pathway {i} ('{key}') needs a name.")
        steps = [clean_template_step(s) for s in p.get("steps") or []]
        if not steps:
            errors.append(f"'{label}' has no steps.")
        for n, s in enumerate(steps, start=1):
            if not s["title"]:
                errors.append(f"'{label}' step {n} needs a title.")
            if s["filled_by"] not in sources:
                errors.append(f"'{label}' step {n}: unknown input '{s['filled_by']}'.")
        pathways.append({
            **{k: (p.get(k) or "").strip() for k in PATHWAY_FIELDS},
            "steps": steps,
        })

    rec_types = {}
    for label, key in (doc.get("rec_types") or {}).items():
        label = (label or "").strip()
        if not label or not key:
            continue
        if key not in seen:
            errors.append(f"'{label}' is mapped to '{key}', which is not a pathway.")
        rec_types[label] = key

    return {
        "status": (doc.get("status") or "").strip(),
        "pathways": pathways,
        "rec_types": rec_types,
    }, errors


def clean_step(step: dict) -> dict:
    """Keep only what a reviewer controls, so derived fields never get persisted."""
    out = {k: step.get(k) for k in STEP_FIELDS if step.get(k) is not None}
    out.setdefault("title", "")
    out.setdefault("filled_by", "template")
    out["edited"] = bool(step.get("edited"))
    return out


def _attach(step: dict, funding: list[dict], sources: dict) -> dict:
    """
    Say where the step's content comes from, and attach funding on a funding step.

    input_status is one of: 'template' (fixed wording), 'attached' / 'missing'
    (a live agent did or didn't return anything), or 'needs_sme' (the source
    meant to supply it isn't built, so the reviewer does).
    """
    s = dict(step)
    key = s.get("filled_by") or "template"
    source = sources.get(key, {})
    s["source_label"] = source.get("label", key)
    s["source_kind"] = source.get("kind", "template")
    s["source_note"] = source.get("note")
    if key == "funding" and source.get("live"):
        s["funding"] = funding
        s["input_status"] = "attached" if funding else "missing"
    elif s["source_kind"] == "template":
        s["input_status"] = "template"
    else:
        s["input_status"] = "needs_sme"
    return s


def build_plan(
    rec: dict, funding: list[dict], saved: dict | None = None, templates: dict | None = None
) -> dict:
    """
    The plan for one recommendation.

    rec: a block_recommendations row.
    funding: that recommendation's approved funding fits, strongest first.
    saved: its implementation_plans row, if a reviewer has saved one.
    templates: the templates in force (load_templates(db)); the file if omitted.
    """
    templates = templates or load_templates()
    pathway = pathway_for(rec.get("rec_label"), templates)
    sources = templates.get("filled_by", {})

    if saved:
        steps = saved.get("steps") or []
    elif pathway:
        steps = [clean_step(s) for s in pathway["steps"]]
    else:
        steps = []

    return {
        "rec": {
            "id": rec.get("id"),
            "rec_label": rec.get("rec_label"),
            "dimension": rec.get("dimension"),
            "direction": rec.get("direction"),
            "predicted_score_increase": rec.get("predicted_score_increase"),
        },
        "pathway": (
            {k: pathway.get(k) for k in ("key", "name", "who", "branch")} if pathway else None
        ),
        "template_version": saved.get("template_version") if saved else templates.get("version"),
        "template_status": templates.get("status"),
        # 'template' = nobody has saved this plan yet; it is the template as drafted.
        "status": saved.get("status") if saved else "template",
        "reviewed_by": saved.get("reviewed_by") if saved else None,
        "notes": saved.get("notes") if saved else None,
        "updated_at": saved.get("updated_at") if saved else None,
        "finalized_at": saved.get("finalized_at") if saved else None,
        "funding_count": len(funding),
        "steps": [_attach(s, funding, sources) for s in steps],
    }
