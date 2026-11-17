"""
Implementation pathways — turn one recommendation into an ordered plan of steps.

The 65 recommendation types collapse into six pathways (building and façade, land
use, parks, traffic control, streetscape, road and curb), each a template of the
steps a city follows to get that kind of change done. The templates are data, in
backend/data/pathways.json, so an SME's corrections are an edit to that file rather
than Python branching. It is read on every call, so an edit shows up without a
restart.

A plan is the template plus whatever this system can attach to it. Today that is
funding only: the step a template marks `filled_by: "funding"` gets the approved
funding signals for that exact recommendation. Every other lookup (zoning, road
owner, policy, parcel records, program terms) is marked as not automated, so the
reviewer can see which parts are assumptions waiting on them rather than findings.

Funding is re-attached on every read, including for a plan someone already saved.
The saved plan holds the reviewer's wording of the steps; the money is live, so a
signal approved after the plan was finalised still appears on it.
"""

import json
from pathlib import Path

TEMPLATES_PATH = Path(__file__).resolve().parent.parent / "data" / "pathways.json"

# The step fields a reviewer can set. Anything else in a saved step (attached
# funding, computed status) is derived on read and never stored.
STEP_FIELDS = ("title", "owner", "waits_on", "time", "filled_by", "note", "edited")


def load_templates() -> dict:
    with open(TEMPLATES_PATH, encoding="utf-8") as f:
        return json.load(f)


def pathway_for(rec_label: str | None, templates: dict | None = None) -> dict | None:
    """The pathway a recommendation type belongs to, or None if it is unmapped."""
    templates = templates or load_templates()
    key = templates["rec_types"].get((rec_label or "").strip())
    return next((p for p in templates["pathways"] if p["key"] == key), None)


def clean_step(step: dict) -> dict:
    """Keep only what a reviewer controls, so derived fields never get persisted."""
    out = {k: step.get(k) for k in STEP_FIELDS if step.get(k) is not None}
    out.setdefault("title", "")
    out.setdefault("filled_by", "template")
    out["edited"] = bool(step.get("edited"))
    return out


def _attach(step: dict, funding: list[dict], sources: dict) -> dict:
    """Add the step's input status, and the funding itself on a funding step."""
    s = dict(step)
    kind = s.get("filled_by") or "template"
    source = sources.get(kind, {})
    s["source_label"] = source.get("label", kind)
    if kind == "funding":
        s["funding"] = funding
        s["input_status"] = "attached" if funding else "missing"
    elif kind == "template":
        s["input_status"] = "template"
    else:
        s["input_status"] = "manual"
    s["source_note"] = source.get("note")
    return s


def build_plan(rec: dict, funding: list[dict], saved: dict | None = None) -> dict:
    """
    The plan for one recommendation.

    rec: a block_recommendations row.
    funding: that recommendation's approved funding fits, strongest first.
    saved: its implementation_plans row, if a reviewer has saved one.
    """
    templates = load_templates()
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
