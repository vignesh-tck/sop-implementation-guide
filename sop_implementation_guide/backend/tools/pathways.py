"""
Implementation pathways — turn one recommendation into an ordered plan of steps.

The app calls a pathway a "playbook" and the plan built from it an
"implementation plan". The code, API routes and tables keep the name pathway.

The 65 recommendation types collapse into six pathways (building and façade, land
use, parks, traffic control, streetscape, road and curb), each a template of the
steps a city follows to get that kind of change done. The templates are data, not
Python branching. They start as backend/data/pathways.json (version v0) and are
edited in the app's Playbooks tab, which saves each edit as a new version in the
pathway_templates table. The latest saved version wins, so the file serves as
the seed and the fallback. A host's disk gets wiped on redeploy, so edits can't
be written back to the file. Templates are read on every call, so an edit shows
up without a restart.

A plan is the template plus whatever this system can attach to it. Each step's
`filled_by` names the source meant to supply it: the template itself, an agent
(funding, zoning, policy) or a lookup (parcel, road owner, program terms). The
`live` flag on each source says whether it actually supplies anything yet. Two
do: the funding agent (approved funding signals for that exact recommendation)
and the zoning lookup (the block's zones checked against the use tables, see
tools/zoning.py). A step whose source is not live is labelled as needing the
SME, with why the step needs it and the specific question to answer, so the
reviewer can tell findings from blanks they have to fill.

Every step carries references: what a live source cited, the reference a
reviewer should check for an unconnected source, and the template version or
the reviewer who set the wording. A step never renders without one.

Each step is a node in a graph (tools/plan_graph.py): who does it (an actor),
how, how long, and which steps it waits on. Steps saved before that existed
are upgraded on read.

Funding and zoning are re-attached on every read, including for a plan someone
already saved. The saved plan holds the reviewer's wording of the steps; the
findings are live, so a signal approved or a zone looked up after the plan was
finalised still appears on it.
"""

import json
import re
from pathlib import Path

from supabase import Client

from backend.repositories.pathway_templates import PathwayTemplateRepository, TemplateTableMissing
from backend.tools.plan_graph import ACTORS, estimate, find_cycle, upgrade_steps

TEMPLATES_PATH = Path(__file__).resolve().parent.parent / "data" / "pathways.json"

# The step fields a reviewer can set. Anything else in a saved step (attached
# funding, computed status) is derived on read and never stored.
STEP_FIELDS = ("id", "title", "how", "actor", "owner", "time", "depends_on", "depends_mode",
               "condition", "filled_by", "note", "edited")

# A template step has no `edited` flag: it is what every plan starts from.
TEMPLATE_STEP_FIELDS = tuple(f for f in STEP_FIELDS if f != "edited")

PATHWAY_FIELDS = ("key", "name", "who", "branch")

KEY_PATTERN = re.compile(r"^[a-z0-9_]+$")


def _upgrade_doc(doc: dict) -> dict:
    """Every pathway's steps with ids, actors and structured dependencies."""
    doc["pathways"] = [{**p, "steps": upgrade_steps(p.get("steps") or [])} for p in doc.get("pathways") or []]
    return doc


def load_file_templates() -> dict:
    with open(TEMPLATES_PATH, encoding="utf-8") as f:
        return _upgrade_doc(json.load(f))


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
        "pathways": _upgrade_doc({"pathways": doc.get("pathways")})["pathways"],
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
    for k in ("how", "owner", "time"):
        out.setdefault(k, "")
    out["depends_on"] = [d for d in out.get("depends_on") or [] if isinstance(d, str)]
    if out.get("depends_mode") != "any":
        out.pop("depends_mode", None)
    if out.get("actor") not in ACTORS:
        out.pop("actor", None)
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
        label = name or key or f"playbook {i}"
        if not KEY_PATTERN.match(key):
            errors.append(f"'{label}': the key must be lowercase letters, digits and underscores.")
        elif key in seen:
            errors.append(f"Two playbooks share the key '{key}'.")
        seen.add(key)
        if not name:
            errors.append(f"Playbook {i} ('{key}') needs a name.")
        steps = upgrade_steps([clean_template_step(s) for s in p.get("steps") or []])
        loop = find_cycle(steps)
        if loop:
            errors.append(f"'{label}': these steps wait on each other in a loop: {'; '.join(loop)}.")
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
            errors.append(f"'{label}' is mapped to '{key}', which is not a playbook.")
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


def clean_steps(steps: list[dict]) -> list[dict]:
    """A reviewer's steps, ready to store: cleaned, upgraded, and checked for loops."""
    out = upgrade_steps([clean_step(s) for s in steps])
    loop = find_cycle(out)
    if loop:
        raise ValueError(f"These steps wait on each other in a loop: {'; '.join(loop)}.")
    return out


def _template_ref(templates: dict) -> dict:
    by = f", last edited by {templates['updated_by']}" if templates.get("updated_by") else ""
    return {
        "label": f"Playbook template {templates.get('version')}",
        "url": None,
        "detail": (templates.get("status") or "Draft template") + by,
    }


def _references(s: dict, key: str, source: dict, templates: dict, saved: dict | None) -> list[dict]:
    """
    Where this step's content comes from, as a list a reviewer can follow.

    Every step has at least one entry. A step nothing supplies yet says so
    rather than showing an empty list, and a reviewer's edit is cited as the
    reviewer.
    """
    refs: list[dict] = []
    if s.get("zoning"):
        refs += s["zoning"]["references"]
    elif s.get("funding"):
        for f in s["funding"]:
            refs.append({
                "label": f.get("program_name") or "Funding program",
                "url": f.get("source_url") or f.get("application_url"),
                "detail": ", ".join(x for x in (f.get("source_name"), f.get("source_vintage")) if x) or None,
            })
    elif key == "funding":
        refs.append({"label": "Funding agent", "url": None,
                     "detail": "No approved funding for this recommendation yet"})
    elif source.get("kind") != "template":
        if source.get("ref"):
            refs.append(source["ref"])
        refs.append({"label": source.get("label", key), "url": None,
                     "detail": "Not connected yet; the SME's answer becomes the reference"})

    # The wording itself: the template, or the reviewer who changed or confirmed it.
    if saved and (s.get("edited") or saved.get("status") == "final") and saved.get("reviewed_by"):
        verb = "edited" if s.get("edited") else "confirmed"
        when = (saved.get("finalized_at") or saved.get("updated_at") or "")[:10]
        refs.append({"label": f"SME {verb}: {saved['reviewed_by']}", "url": None,
                     "detail": f"Saved {when}" if when else None})
    else:
        refs.append(_template_ref(templates))
    return refs


def _attach(
    step: dict,
    funding: list[dict],
    sources: dict,
    zoning: dict | None = None,
    ctx: dict | None = None,
    templates: dict | None = None,
    saved: dict | None = None,
) -> dict:
    """
    Say where the step's content comes from, and attach what a live source found.

    input_status is one of: 'template' (fixed wording), 'attached' / 'missing'
    (a live source did or didn't return anything), 'partial' (a live source
    answered part of it and asks the SME the rest), or 'needs_sme' (the source
    meant to supply it isn't built, so the reviewer does).
    """
    s = dict(step)
    key = s.get("filled_by") or "template"
    source = sources.get(key, {})
    ctx = ctx or {}
    s["source_label"] = source.get("label", key)
    s["source_kind"] = source.get("kind", "template")
    s["source_note"] = source.get("note")
    if key == "funding" and source.get("live"):
        s["funding"] = funding
        s["input_status"] = "attached" if funding else "missing"
    elif key == "zoning" and source.get("live") and zoning is not None:
        s["zoning"] = zoning
        s["input_status"] = zoning["status"]
    elif s["source_kind"] == "template":
        s["input_status"] = "template"
    else:
        s["input_status"] = "needs_sme"
        # Explain before asking: why the step needs this, then the question itself.
        if source.get("why"):
            s["sme_why"] = source["why"]
        if source.get("ask"):
            s["sme_ask"] = source["ask"].format(
                thing=ctx.get("thing") or "this", street=ctx.get("street") or "this street"
            )
    s["references"] = _references(s, key, source, templates or {}, saved)
    return s


def build_plan(
    rec: dict,
    funding: list[dict],
    saved: dict | None = None,
    templates: dict | None = None,
    zoning: dict | None = None,
    block: dict | None = None,
) -> dict:
    """
    The plan for one recommendation.

    rec: a block_recommendations row.
    funding: that recommendation's approved funding fits, strongest first.
    saved: its implementation_plans row, if a reviewer has saved one.
    templates: the templates in force (load_templates(db)); the file if omitted.
    zoning: the zoning finding for this recommendation (tools/zoning.assess).
    block: the blocks row, for the street name in SME questions.
    """
    templates = templates or load_templates()
    pathway = pathway_for(rec.get("rec_label"), templates)
    sources = templates.get("filled_by", {})
    ctx = {
        "street": (block or {}).get("street_name"),
        "thing": (rec.get("rec_label") or "").lower() or None,
    }

    if saved:
        steps = upgrade_steps(saved.get("steps") or [])
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
        # Rough total along the longest chain of steps, in weeks.
        "estimate": estimate(steps),
        "zoning_summary": zoning["summary"] if zoning else None,
        # The whole finding and funding list, for the recommendation's own Zoning
        # and Funding views; the steps below carry the same data where it applies.
        "zoning": zoning,
        "funding": funding,
        "steps": _mark_zoning_detail(
            [_attach(s, funding, sources, zoning, ctx, templates, saved) for s in steps]
        ),
    }


def _mark_zoning_detail(steps: list[dict]) -> list[dict]:
    """
    Show the full zoning finding on the first zoning step only.

    A land-use plan has two zoning steps (look up the zone, check the use). Both
    rest on the same finding, so later ones point back to the first rather than
    repeating it.
    """
    first = None
    for i, s in enumerate(steps, start=1):
        if s.get("zoning"):
            if first is None:
                first = i
                s["zoning_detail"] = "full"
            else:
                s["zoning_detail"] = "summary"
                s["zoning_see_step"] = first
    return steps
