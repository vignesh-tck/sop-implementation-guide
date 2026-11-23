"""
The block assistant's tools: read-only views of this app's data, for
backend/agents/block_assistant.py.

Two kinds of tools:
- The block as it stands: scores, recommendations, plans, the street survey,
  timelines, and the blocks side by side.
- How it got that way: the zoning finding behind an outcome and how a
  recommendation type is read as ordinance uses, the funding matches and the
  program catalogue, the playbooks, a plan's audit trail, the dataset versions
  and the pipelines that produced them, and the tables general pipelines write.

Every document a tool reads from (an ordinance section, a GIS layer, a program
page, a pipeline source, a playbook step's source) is registered as a numbered
reference ("R3") and returned with the data it backs. The answer cites those
ids; block_assistant turns them into numbered links. A reference the tools
never returned cannot be cited, so the assistant can't invent a source.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from supabase import Client

from backend.agents.zoning import ZoningAgent, approved_items, reviewed_finding
from backend.api.plans import _plan_response, list_plans
from backend.repositories.datasets import BlockAnalysisRepository, DatasetTablesMissing, DatasetVersionRepository
from backend.repositories.pathway_templates import PathwayTemplateRepository
from backend.repositories.plans import PlanRevisionRepository, RevisionTableMissing
from backend.repositories.recommendations import BlockRepository
from backend.tools import zoning
from backend.tools.pathways import load_templates, pathway_for
from backend.tools.plan_graph import estimate, upgrade_steps

log = logging.getLogger(__name__)

WEEKS_PER_MONTH = 4.345
FITS_PER_REC = 4

# Score columns on `blocks`, with the dimension names block_recommendations uses.
DIMENSIONS = [
    ("form_score", "Form"), ("density_score", "Density"), ("proximity_score", "Proximity"),
    ("connectivity_score", "Connectivity"), ("parks_score", "Parks & Public Spaces"),
    ("pedestrian_score", "Pedestrian & Bike Amenities"), ("safety_score", "Personal Safety"),
    ("traffic_score", "Traffic Safety"), ("aesthetics_score", "Aesthetics"),
]
BLOCK_COLUMNS = ("id, street_name, intersection_a, intersection_b, sop_index_norm, tract_geoid, median_hh_income, "
                 + ", ".join(c for c, _ in DIMENSIONS))
GP_TABLE = re.compile(r"^gp_[a-z][a-z0-9_]{0,50}$")


@lru_cache(maxsize=1)
def _features_dict() -> dict:
    """SoP feature code -> {description, values}, from 'SoP 150 Feature List.csv'."""
    return json.loads((Path(__file__).resolve().parent.parent / "data" / "sop_features.json").read_text(encoding="utf-8"))


def weeks_text(lo: float | None, hi: float | None) -> str | None:
    """Mirrors weeksRange in frontend/web/src/blocks/plan/graph.js: "3–8 wk", "4–13 mo", "1.6–4.8 yr"."""
    if lo is None or hi is None:
        return None
    if hi < 10:
        a, b = max(1, round(lo)), max(1, round(hi))
        return f"{a} wk" if a == b else f"{a}–{b} wk"
    if hi / WEEKS_PER_MONTH > 36:
        return f"{lo / 52:.1f}–{hi / 52:.1f} yr"
    a, b = max(1, round(lo / WEEKS_PER_MONTH)), max(1, round(hi / WEEKS_PER_MONTH))
    return f"{a} mo" if a == b else f"{a}–{b} mo"


def _num(v: Any, d: int = 1) -> float | None:
    return None if v is None else round(float(v), d)


def _short(v: Any, n: int = 300) -> Any:
    """A long value cut for the model; structures as compact JSON."""
    s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"


# ── The data the tools read, loaded once per turn ────────────────────────────

class Data:
    def __init__(self, db: Client):
        self.db = db
        self._blocks: list[dict] | None = None
        self._recs: dict[int, list[dict]] = {}
        self._reviews: dict[int, dict] = {}
        self._all_recs: list[dict] | None = None
        self._templates: dict | None = None

    def blocks(self) -> list[dict]:
        if self._blocks is None:
            self._blocks = self.db.table("blocks").select(BLOCK_COLUMNS).order("id").execute().data or []
        return self._blocks

    def block(self, block_id: int) -> dict:
        b = next((x for x in self.blocks() if x["id"] == block_id), None)
        if not b:
            raise ValueError(f"There is no block {block_id}.")
        return b

    def templates(self) -> dict:
        if self._templates is None:
            self._templates = load_templates(self.db)
        return self._templates

    def recs(self, block_id: int) -> list[dict]:
        """The block's recommendations as its list shows them (playbook, plan status, estimate, zoning, funding)."""
        if block_id not in self._recs:
            self.block(block_id)
            self._recs[block_id] = list_plans(block_id, self.db)["recommendations"]
        return self._recs[block_id]

    def rec(self, block_id: int, rec_id: int) -> dict:
        r = next((x for x in self.recs(block_id) if x["rec_id"] == rec_id), None)
        if not r:
            raise ValueError(f"Recommendation {rec_id} is not on block {block_id}.")
        return r

    def reviews(self, block_id: int) -> dict:
        """Where the block's funding and zoning reviews stand, in words."""
        if block_id in self._reviews:
            return self._reviews[block_id]
        try:
            repo = BlockAnalysisRepository(self.db)
            runs = [a for a in repo.list_for_block(block_id, light=True) if a.get("status") != "discarded"]
            pinned = {k: repo.pinned(block_id, k) for k in ("funding", "zoning")}
        except DatasetTablesMissing:
            runs, pinned = [], {"funding": None, "zoning": None}

        def status(kind: str) -> str:
            if any(a.get("kind") == kind and a.get("status") == "running" for a in runs):
                return "running"
            if any(a.get("kind") == kind and a.get("status") == "in_review" for a in runs):
                return "awaiting SME approval"
            p = pinned[kind]
            if not p:
                return "not run"
            if kind == "zoning" and approved_items(p) is None:
                return "needs a re-run (the current run predates SME review)"
            return f"approved (v{p.get('dataset_version')})" if p.get("dataset_version") is not None else "approved"

        self._reviews[block_id] = {"funding": status("funding"), "zoning": status("zoning")}
        return self._reviews[block_id]

    def all_recs(self) -> list[dict]:
        if self._all_recs is None:
            self._all_recs = BlockRepository(self.db).all_recommendations(
                "id, block_id, rec_label, dimension, direction, predicted_score_increase")
        return self._all_recs

    def averages(self) -> dict[str, float | None]:
        out = {}
        for col, _ in DIMENSIONS + [("sop_index_norm", "SoP")]:
            vals = [float(b[col]) for b in self.blocks() if b.get(col) is not None]
            out[col] = round(sum(vals) / len(vals), 1) if vals else None
        return out


def _rank(rows: list[dict], col: str, value: Any) -> int | None:
    """1 = the highest of all blocks."""
    if value is None:
        return None
    return 1 + sum(1 for r in rows if r.get(col) is not None and float(r[col]) > float(value))


def rec_row(r: dict) -> dict:
    est = r.get("estimate") or {}
    return {
        "rec_id": r["rec_id"], "label": r["rec_label"], "dimension": r.get("dimension"),
        "direction": r.get("direction"), "predicted_score_increase": r.get("predicted_score_increase"),
        "playbook": r.get("pathway_name") or "none (no playbook covers this type yet)",
        "plan": ("approved" if r.get("plan_status") == "final" else "playbook default")
                + (f"; a draft by {r.get('draft_by')} awaits approval" if r.get("draft_pending") else ""),
        "duration": weeks_text(est.get("fastest_weeks"), est.get("slowest_weeks")),
        "duration_weeks": [est.get("fastest_weeks"), est.get("slowest_weeks")] if est else None,
        "zoning": r.get("zoning_best") or "not checked or not resolved",
        "approved_funding_matches": r.get("funding_count", 0),
    }


def timeline(data: Data, block_id: int, rec_ids: list[int], arrangement: str = "together",
             phases: list[list[int]] | None = None) -> dict:
    """
    How long a set of recommendations takes, from each one's plan estimate
    (its approved plan, else its playbook's default steps), in weeks.

    together: all start at once. one_after_another: in the order given.
    phases: each phase's recommendations together, phases one after another.
    A bar starts at [earliest, latest] start and runs its fastest / slowest.
    """
    if arrangement == "phases":
        groups = [list(p) for p in (phases or []) if p]
    elif arrangement == "one_after_another":
        groups = [[r] for r in rec_ids]
    else:
        arrangement, groups = "together", [list(rec_ids)]
    bars, untimed = [], []
    start_lo = start_hi = 0.0
    for gi, group in enumerate(groups, start=1):
        end_lo, end_hi = start_lo, start_hi
        for rid in group:
            r = data.rec(block_id, rid)
            est = r.get("estimate") or {}
            lo, hi = est.get("fastest_weeks"), est.get("slowest_weeks")
            if lo is None or hi is None or hi <= 0:
                untimed.append({"rec_id": rid, "label": r["rec_label"],
                                "why": "no playbook" if not r.get("pathway_key") else "its steps carry no durations"})
                continue
            bars.append({"rec_id": rid, "label": r["rec_label"], "phase": gi if arrangement == "phases" else None,
                         "start_weeks": [round(start_lo, 1), round(start_hi, 1)],
                         "weeks": [lo, hi], "text": weeks_text(lo, hi),
                         "untimed_steps": est.get("untimed") or []})
            end_lo, end_hi = max(end_lo, start_lo + lo), max(end_hi, start_hi + hi)
        start_lo, start_hi = end_lo, end_hi
    return {"arrangement": arrangement, "bars": bars, "untimed": untimed,
            "total_weeks": [round(start_lo, 1), round(start_hi, 1)], "total": weeks_text(start_lo, start_hi)}


# ── Tool definitions ─────────────────────────────────────────────────────────

_IDS = {"type": "array", "items": {"type": "integer"}}
_BLOCK = {"block_id": {"type": "integer"}}
_BLOCK_REC = {"block_id": {"type": "integer"}, "rec_id": {"type": "integer"}}


def _tool(name: str, description: str, props: dict | None = None, required: list[str] | None = None) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props or {}, "required": required or []}}


TOOLS = [
    # The block as it stands
    _tool("block_overview",
          "One block's facts: street, cross streets, census tract and ACS figures, its SoP score and each dimension "
          "score with the average and rank across all blocks, its gold feasibility score if computed, where its "
          "funding and zoning reviews stand, and how many of its plans are approved.", _BLOCK, ["block_id"]),
    _tool("recommendations",
          "A block's recommendations: label, dimension, direction, predicted score increase, the playbook it follows, "
          "whether its plan is approved or still the playbook default, its duration estimate, its approved zoning "
          "outcome and how many approved funding matches it has.", _BLOCK, ["block_id"]),
    _tool("plan",
          "One recommendation's implementation plan on a block: every step (what, how, who, how long, what it waits "
          "on, when it applies, its source), the duration estimate, the zoning summary and approved funding.",
          _BLOCK_REC, ["block_id", "rec_id"]),
    _tool("street_features",
          "What the State of Place survey recorded on the block's street today, feature by feature, and the "
          "recommended state where it differs. Without `search`: the features present plus those that differ. With "
          "`search`: every feature whose name or code contains one of the words (e.g. [\"crosswalk\", \"zebra\"]).",
          {**_BLOCK, "search": {"type": "array", "items": {"type": "string"}}}, ["block_id"]),
    _tool("timeline",
          "How long a set of a block's recommendations takes, computed from each one's plan estimate. arrangement: "
          "together (all start at once), one_after_another (in the order of rec_ids), or phases (each inner list "
          "together, the phases one after another).",
          {**_BLOCK, "rec_ids": _IDS, "arrangement": {"type": "string", "enum": ["together", "one_after_another", "phases"]},
           "phases": {"type": "array", "items": _IDS}}, ["block_id", "rec_ids", "arrangement"]),
    _tool("find_blocks",
          "All blocks, one row each: street, cross streets, SoP score, number of recommendations, the biggest single "
          "lift, and where funding and zoning reviews stand. Filter by street (substring) or by a recommendation type "
          "(substring of its label; each row then shows that recommendation's predicted increase). Sort by sop "
          "(ascending), sop_desc, or a dimension name (ascending). include_dimensions adds every dimension score.",
          {"street": {"type": "string"}, "rec_label": {"type": "string"}, "sort": {"type": "string"},
           "include_dimensions": {"type": "boolean"}}),

    # How it got that way
    _tool("zoning_finding",
          "Why zoning says what it does for one recommendation on a block, with citations: the zones the block "
          "fronts (from the county GIS), the use-table row and value (P / SE / X) per zone with its ordinance "
          "section, the finding's narrative by section (what we found, what the ordinance says, what it means, how "
          "the recommendation was read, what was checked), open questions, and the SME's decision and note. Reads "
          "the approved Block Zoning run; else the run awaiting review; else a preview from the zoning data in force "
          "(says which).", _BLOCK_REC, ["block_id", "rec_id"]),
    _tool("zoning_reading",
          "How a recommendation type is read for zoning: which ordinance uses it is taken to be (e.g. \"Bars/Night "
          "Clubs\" as a nightclub), with the reading's note and confidence, or that it isn't a land use, or that no "
          "reading exists yet. Also the zoning data version and rules version in force.",
          {"rec_label": {"type": "string"}}, ["rec_label"]),
    _tool("funding_detail",
          "A block's funding, per recommendation, with citations: the approved matches from its pinned Block Funding "
          "run (program, agency, amount planned, fit score, why it fits, recommended action, eligibility, deadline, "
          "source), or the proposals awaiting SME approval, and the Funding version the run used. rec_id narrows it "
          "to one recommendation.", {**_BLOCK, "rec_id": {"type": "integer"}}, ["block_id"]),
    _tool("funding_programs",
          "The funding program catalogue in one Funding version (latest by default), with citations: program, type, "
          "agency, award range, deadline, eligibility, geographic scope, extraction confidence and source. `search` "
          "words match name, agency, type or eligibility.",
          {"search": {"type": "array", "items": {"type": "string"}}, "version": {"type": "integer"}}),
    _tool("playbook",
          "A playbook (the standard steps for one kind of change): its question, every step (what, how, who, how "
          "long, what it waits on, when it applies, what fills it in on a block, its source), the recommendation "
          "types that follow it, its duration estimate, and the playbook version history. Give a key or a "
          "recommendation label; with neither, lists every playbook.",
          {"key": {"type": "string"}, "rec_label": {"type": "string"}}),
    _tool("plan_history",
          "One recommendation's plan audit trail on a block: every draft saved, approval and discarded draft, with "
          "who, when, the note and what changed.", _BLOCK_REC, ["block_id", "rec_id"]),
    _tool("datasets",
          "The dataset versions (Funding v1, v2 … Zoning v1 … general tables): when, by whom, what produced each "
          "(which pipeline and definition version), record counts, and how many blocks' approved runs use each.",
          {"kind": {"type": "string", "enum": ["funding", "zoning", "general"]}}),
    _tool("pipelines",
          "The data pipelines built in the Pipeline development tab. Without pipeline_id: every pipeline with its kind "
          "and the dataset version its last load made. With pipeline_id: how it was built, with citations: its goal, "
          "sources, steps in order (type, what each does, its inputs), the fields it extracts, its outputs, and its "
          "runs (when, which dataset version, how complete the fields were).",
          {"pipeline_id": {"type": "integer"}, "kind": {"type": "string", "enum": ["funding", "zoning", "general"]}}),
    _tool("general_table",
          "Rows of a table a general pipeline wrote (gp_<name>; `datasets` with kind general lists them), each with "
          "how its values were derived. block_id filters to one block when the table has that column.",
          {"table": {"type": "string"}, "block_id": {"type": "integer"}, "limit": {"type": "integer"}}, ["table"]),
]

PROGRESS = {
    "block_overview": "Reading block {block_id}'s scores and reviews",
    "recommendations": "Reading block {block_id}'s recommendations",
    "plan": "Reading a plan's steps",
    "street_features": "Reading the street survey",
    "timeline": "Working out durations",
    "find_blocks": "Looking across all blocks",
    "zoning_finding": "Reading the zoning finding and the ordinance",
    "zoning_reading": "Reading how the recommendation is read for zoning",
    "funding_detail": "Reading the block's funding",
    "funding_programs": "Searching the funding catalogue",
    "playbook": "Reading the playbook",
    "plan_history": "Reading the plan's history",
    "datasets": "Reading the dataset versions",
    "pipelines": "Reading how the pipelines were built",
    "general_table": "Reading a pipeline table",
}


class Tools:
    def __init__(self, data: Data):
        self.data = data
        self.sources: list[dict] = []    # the data read, shown under the answer
        self.refs: list[dict] = []       # documents that back it, citable as R1, R2 …

    # ── bookkeeping ──

    def _source(self, key: str, label: str, detail: str) -> None:
        if not any(s["key"] == key for s in self.sources):
            self.sources.append({"key": key, "label": label, "detail": detail})

    def ref(self, label: str | None, url: str | None = None, detail: str | None = None) -> str | None:
        """Register a document and return its id ("R3"); the same document keeps one id."""
        label = (label or "").strip() or (url or "").strip()
        if not label:
            return None
        url = url if isinstance(url, str) and url.startswith(("http://", "https://")) else None
        for r in self.refs:
            if r["label"] == label and r["url"] == url:
                return r["id"]
        rid = f"R{len(self.refs) + 1}"
        self.refs.append({"id": rid, "label": label, "url": url, "detail": detail})
        return rid

    def run(self, name: str, args: dict) -> tuple[Any, bool]:
        fn = getattr(self, name, None) if name in PROGRESS else None
        if not fn:
            return {"error": f"Unknown tool {name}"}, True
        try:
            return fn(**args), False
        except (ValueError, TypeError, KeyError) as e:
            return {"error": str(e)}, True
        except Exception as e:  # noqa: BLE001 — one failed read shouldn't end the turn
            log.exception("block assistant tool %s failed", name)
            return {"error": f"Could not read that: {type(e).__name__}"}, True

    # ── the block as it stands ──

    def block_overview(self, block_id: int) -> dict:
        d = self.data
        b, rows, avg = d.block(block_id), d.blocks(), d.averages()
        recs = d.recs(block_id)
        gold = (d.db.table("block_implementation_profiles")
                .select("feasibility_score, funding_score, zoning_score, policy_score, computed_at")
                .eq("block_id", block_id).execute().data or [None])[0]
        self._source(f"block:{block_id}", f"Block {block_id} scores",
                     "SoP and dimension scores from the State of Place data, compared with all blocks")
        return {
            "block_id": block_id, "street": b.get("street_name"),
            "between": [b.get("intersection_a"), b.get("intersection_b")],
            "sop_score": _num(b.get("sop_index_norm")), "sop_average_all_blocks": avg["sop_index_norm"],
            "sop_rank": f"{_rank(rows, 'sop_index_norm', b.get('sop_index_norm'))} of {len(rows)} (1 = highest)",
            "tract": b.get("tract_geoid"), "median_household_income": b.get("median_hh_income"),
            "dimensions": [{"dimension": name, "score": _num(b.get(col)), "average_all_blocks": avg[col],
                            "rank": f"{_rank(rows, col, b.get(col))} of {len(rows)}"} for col, name in DIMENSIONS],
            "gold_feasibility": gold,
            "reviews": d.reviews(block_id),
            "plans": {"recommendations": len(recs),
                      "approved": sum(1 for r in recs if r.get("plan_status") == "final"),
                      "drafts_waiting": sum(1 for r in recs if r.get("draft_pending")),
                      "playbook_default": sum(1 for r in recs if r.get("plan_status") != "final")},
            "note": "Scores are 0-100. The SoP score is State of Place's overall index for the block (normalised). "
                    "gold_feasibility is this app's score from approved funding and zoning (null until computed).",
        }

    def recommendations(self, block_id: int) -> dict:
        recs = sorted(self.data.recs(block_id), key=lambda r: -(r.get("predicted_score_increase") or -1e9))
        self._source(f"recs:{block_id}", f"Block {block_id} recommendations",
                     "State of Place's ranked recommendations, with each plan's status and estimate")
        return {"block_id": block_id, "reviews": self.data.reviews(block_id),
                "recommendations": [rec_row(r) for r in recs],
                "note": "Listed by predicted score increase, highest first. predicted_score_increase is State of "
                        "Place's model output, ranked within its dimension in the source file; the file doesn't say "
                        "whether it is the dimension score or the overall index, so don't claim either. Durations "
                        "come from the approved plan, else the playbook's default steps (a strawman, not SME-reviewed)."}

    def _steps(self, steps: list[dict]) -> list[dict]:
        num = {s.get("id"): i for i, s in enumerate(steps, start=1)}
        sources = self.data.templates().get("filled_by") or {}
        out = []
        for s in steps:
            src = (s.get("source") or "").strip()
            out.append({
                "n": num.get(s.get("id")), "title": s.get("title"), "how": s.get("how"), "who": s.get("owner"),
                "actor": s.get("actor"), "also": s.get("secondary_owner"), "time": s.get("time"),
                "after": [num.get(x) for x in s.get("depends_on") or [] if x in num],
                "after_mode": s.get("depends_mode") or "all", "only_if": s.get("condition") or None,
                "filled_by": (sources.get(s.get("filled_by")) or {}).get("label") or s.get("filled_by"),
                "source": src or None, "ref": self.ref(src, None, "Source given on the playbook step") if src else None,
            })
        return out

    def plan(self, block_id: int, rec_id: int) -> dict:
        d = self.data
        rec = d.rec(block_id, rec_id)
        block = BlockRepository(d.db).get_with_recs(block_id)
        raw = next(r for r in block["recommendations"] if r["id"] == rec_id)
        p = _plan_response(d.db, block, raw)
        est = p.get("estimate") or {}
        self._source(f"plan:{rec_id}", f"{rec['rec_label']} plan",
                     "Approved plan" if p.get("status") == "final" else "Playbook default steps (not yet approved)")
        return {
            "rec": rec_row(rec), "status": "approved" if p.get("status") == "final" else "playbook default",
            "reviewed_by": p.get("reviewed_by"), "revision": p.get("revision"),
            "draft_waiting": bool(p.get("draft")),
            "duration": weeks_text(est.get("fastest_weeks"), est.get("slowest_weeks")),
            "untimed_steps": est.get("untimed") or [],
            "zoning": p.get("zoning_summary"),
            "funding": [{k: f.get(k) for k in ("program_name", "fit_score", "planned_amount", "source_agency")}
                        for f in p.get("funding") or []],
            "steps": self._steps(p.get("steps") or []),
            "note": "For why zoning says what it does, call zoning_finding; for the funding detail, funding_detail.",
        }

    def street_features(self, block_id: int, search: list[str] | None = None) -> dict:
        self.data.block(block_id)
        row = (self.data.db.table("blocks").select("features_current, features_recommended")
               .eq("id", block_id).execute().data or [{}])[0]
        cur, rec = row.get("features_current") or {}, row.get("features_recommended") or {}
        fd = _features_dict()
        words = [w.lower().strip() for w in search or [] if w.strip()]

        def label(code: str, v: Any) -> Any:
            vals = (fd.get(code) or {}).get("values") or []
            return vals[v] if isinstance(v, int) and 0 <= v < len(vals) else v

        out = []
        for code in sorted(set(cur) | set(rec)):
            desc = (fd.get(code) or {}).get("description") or code
            c, r = cur.get(code), rec.get(code)
            if words:
                if not any(w in desc.lower() or w in code for w in words):
                    continue
            elif not (c or c != r):
                continue
            item = {"code": code, "feature": desc, "now": label(code, c)}
            if r != c:
                item["recommended"] = label(code, r)
            out.append(item)
        self._source(f"features:{block_id}", f"Block {block_id} street survey",
                     "Features State of Place recorded on the street (SoP 150 Feature List)")
        return {"block_id": block_id, "features": out,
                "note": "Intersection 1 and 2 are the block's two ends. Without a search, absent features are left out."}

    def timeline(self, block_id: int, rec_ids: list[int], arrangement: str = "together",
                 phases: list[list[int]] | None = None) -> dict:
        self._source("durations", "Playbook durations",
                     "Each plan's estimate along its longest chain of steps (approved plan, else playbook default)")
        return timeline(self.data, block_id, rec_ids, arrangement, phases)

    def find_blocks(self, street: str | None = None, rec_label: str | None = None, sort: str | None = None,
                    include_dimensions: bool = False) -> dict:
        d = self.data
        rows = d.blocks()
        try:
            repo = BlockAnalysisRepository(d.db)
            pinned = {k: repo.pinned_for_kind(k) for k in ("funding", "zoning")}
        except DatasetTablesMissing:
            pinned = {"funding": {}, "zoning": {}}
        by_block: dict[int, list[dict]] = {}
        for r in d.all_recs():
            by_block.setdefault(r["block_id"], []).append(r)
        q_street, q_rec = (street or "").lower().strip(), (rec_label or "").lower().strip()

        out = []
        for b in rows:
            if q_street and q_street not in (b.get("street_name") or "").lower():
                continue
            recs = by_block.get(b["id"], [])
            match = [r for r in recs if q_rec in (r.get("rec_label") or "").lower()] if q_rec else None
            if q_rec and not match:
                continue
            top = max(recs, key=lambda r: r.get("predicted_score_increase") or -1e9, default=None)
            zp = pinned["zoning"].get(b["id"])
            row = {"block_id": b["id"], "street": b.get("street_name"),
                   "between": f"{b.get('intersection_a') or '?'} → {b.get('intersection_b') or '?'}",
                   "sop": _num(b.get("sop_index_norm")), "recommendations": len(recs),
                   "biggest_lift": f"{top['rec_label']} +{top['predicted_score_increase']}" if top else None,
                   "funding": "approved" if pinned["funding"].get(b["id"]) else "not run",
                   "zoning": ("approved" if approved_items(zp) is not None else "needs a re-run") if zp else "not run"}
            if match:
                row["matching"] = [f"{r['rec_label']} +{r.get('predicted_score_increase')}" for r in match]
            if include_dimensions:
                row["dimensions"] = {name: _num(b.get(col)) for col, name in DIMENSIONS}
            out.append(row)

        col = "sop_index_norm" if sort in ("sop", "sop_desc") else \
            next((c for c, n in DIMENSIONS if n.lower() == (sort or "").lower()), None)
        if col:
            val = {b["id"]: b.get(col) for b in rows}
            scored = sorted((r for r in out if val[r["block_id"]] is not None),
                            key=lambda r: float(val[r["block_id"]]), reverse=sort == "sop_desc")
            out = scored + [r for r in out if val[r["block_id"]] is None]
        self._source("blocks", f"All {len(rows)} blocks", "Every block's SoP score, recommendations and review status")
        return {"count": len(out), "of": len(rows), "blocks": out}

    # ── how it got that way: zoning ──

    def zoning_finding(self, block_id: int, rec_id: int) -> dict:
        d = self.data
        rec = d.rec(block_id, rec_id)
        agent = ZoningAgent()
        run, items = agent.approved(block_id, d.db)
        basis, item = None, None
        if items is not None:
            item = next((i for i in items if rec_id in (i.get("rec_ids") or [])), None)
            basis = "approved" if item else None
        if item is None:
            pending = agent.pending(block_id, d.db)
            full = BlockAnalysisRepository(d.db).get(pending["id"]) if pending else None
            item = next((i for i in ((full or {}).get("result") or {}).get("findings") or []
                         if rec_id in (i.get("rec_ids") or [])), None)
            if item:
                run, basis = full, "awaiting SME review: proposed, not approved"

        if item is not None:
            f = reviewed_finding(item, run) if basis == "approved" else copy.deepcopy(item["finding"])
        else:
            zoning.refresh_active(d.db)
            preview = agent.assess_block(block_id, d.db)
            f = next((x for x in preview.get("findings") or [] if x.get("rec_id") == rec_id), None)
            if not f:
                return {"rec": rec["rec_label"], "basis": "not assessed",
                        "reading": zoning.reading_for(rec["rec_label"]),
                        "note": "Zoning doesn't assess this recommendation type (it isn't read as a land use, or isn't "
                                "read yet), so no finding exists. Call zoning_reading for its reading."}
            basis, run = "preview from the zoning data in force: not reviewed by an SME", None

        ids = [self.ref(r.get("label"), r.get("url"), r.get("detail")) for r in f.get("references") or []]

        def cite(nums: list | None) -> list[str]:
            return [ids[n - 1] for n in nums or [] if isinstance(n, int) and 1 <= n <= len(ids) and ids[n - 1]]

        sections = f.get("sections") or zoning.SECTIONS
        result = (run or {}).get("result") or {}
        self._source(f"zoning:{block_id}:{rec_id}", f"{rec['rec_label']} zoning finding",
                     f"Block Zoning run #{run['id']} (Zoning v{run.get('dataset_version')}), {basis}" if run
                     else "Preview from the zoning data in force (not reviewed)")
        return {
            "rec": rec["rec_label"], "basis": basis,
            "run": {"analysis_id": run.get("id"), "zoning_version": run.get("dataset_version"),
                    "rules_version": result.get("rules_version"),
                    "use_table_amended_through": result.get("use_table_amended_through"),
                    "approved_by": result.get("approved_by"), "approved_at": result.get("approved_at")} if run else None,
            "outcome": f.get("best"), "summary": f.get("summary"), "status": f.get("status"),
            "review": f.get("review"),
            "zones": [{"zone": z.get("zone"), "name": z.get("name"), "outcome": z.get("outcome"),
                       "uses": [{"use": u.get("row"), "table": u.get("table"), "value": u.get("value"),
                                 "outcome": u.get("outcome"),
                                 "standards": [self.ref(s.get("text"), s.get("url"), "Use-specific standard")
                                               for s in u.get("standards") or []]}
                                for u in z.get("uses") or []]}
                      for z in f.get("zones") or []],
            "narrative": [{"section": sections.get(p.get("section"), p.get("section")), "text": p.get("text"),
                           "refs": cite(p.get("refs"))} for p in f.get("narrative") or []],
            "questions": [{"text": q.get("text"), "refs": cite(q.get("refs"))} for q in f.get("questions") or []],
            "note": "Values: P = permitted by right, SE = special exception, X = prohibited. Cite the refs.",
        }

    def zoning_reading(self, rec_label: str) -> dict:
        zoning.refresh_active(self.data.db)
        rules = zoning.load_rules()
        r = zoning.reading_for(rec_label)
        self._source("zoning-readings", "Zoning readings",
                     f"How each recommendation type is read as ordinance uses (rules {rules.get('version')})")
        if r is None:
            reading = "not read yet: no reading says which ordinance uses this is, so zoning can't assess it"
        elif r.get("governed_by") == zoning.NOT_A_LAND_USE:
            reading = {"not_a_land_use": True, **r}
        else:
            reading = r
        latest = DatasetVersionRepository(self.data.db).latest("zoning") or {}
        return {"rec_type": rec_label, "reading": reading, "rules_version": rules.get("version"),
                "latest_zoning_version": {"version": latest.get("version"), "name": latest.get("name"),
                                          "made_by": latest.get("source"), "at": latest.get("created_at")},
                "note": "Readings are published by the zoning pipeline's readings output (see pipelines); a "
                        "low-confidence reading is flagged for the SME in each Block Zoning run."}

    # ── funding ──

    def _fit(self, f: dict) -> dict:
        return {
            "program": f.get("program_name"), "type": f.get("program_type"), "agency": f.get("source_agency"),
            "fit_score": f.get("fit_score"), "why": _short(f.get("narrative"), 280),
            "recommended_action": f.get("recommended_action"),
            "planned_amount": f.get("planned_amount"), "amount_basis": f.get("amount_basis"),
            "award_range": [f.get("award_amount_min"), f.get("award_amount_max")], "deadline": f.get("deadline"),
            "eligibility_confirmed": f.get("eligibility_confirmed"), "eligibility_basis": f.get("determination_basis"),
            "ref": self.ref(f"{f.get('program_name')}: {f.get('source_name') or 'source'}", f.get("source_url"),
                            f"Fetched {f.get('source_vintage')}" if f.get("source_vintage") else None),
        }

    def funding_detail(self, block_id: int, rec_id: int | None = None) -> dict:
        from backend.agents.deps import get_orchestrator
        from backend.api.blocks import _fit_from_signal

        d = self.data
        if rec_id is not None:
            d.rec(block_id, rec_id)
        try:
            repo = BlockAnalysisRepository(d.db)
            pinned = repo.pinned(block_id, "funding")
            pending = next((a for a in repo.list_for_block(block_id, "funding", light=True)
                            if a.get("status") == "in_review"), None)
        except DatasetTablesMissing:
            pinned = pending = None
        out: dict[str, Any] = {"block_id": block_id, "review": d.reviews(block_id)["funding"]}

        if pinned:
            rows = (d.db.table("funding_signals").select("*").eq("analysis_id", pinned["id"]).eq("reviewed", True)
                    .execute().data or [])
            fits: dict[int, list] = {}
            for s in rows:
                if rec_id is None or s.get("rec_id") == rec_id:
                    f = _fit_from_signal(s)
                    f.update(eligibility_confirmed=s.get("eligibility_confirmed"),
                             determination_basis=s.get("determination_basis"))
                    fits.setdefault(s.get("rec_id"), []).append(self._fit(f))
            out["approved"] = {
                "analysis_id": pinned["id"], "funding_version": pinned.get("dataset_version"), "name": pinned.get("name"),
                "by_recommendation": [
                    {"rec_id": k, "label": d.rec(block_id, k)["rec_label"] if k else None, "approved_matches": len(v),
                     "matches": sorted(v, key=lambda f: -(f["fit_score"] or 0))[:FITS_PER_REC]}
                    for k, v in fits.items()],
                "note": f"Up to {FITS_PER_REC} matches per recommendation, strongest fit first; ask with rec_id for one.",
            }
        if pending:
            review = {}
            try:
                review = get_orchestrator().get_funding_review(pending.get("thread_id")) or {}
            except Exception:  # noqa: BLE001 — the in-memory review is gone after a restart
                log.warning("funding review for %s unavailable", pending.get("thread_id"))
            groups = [g for g in review.get("recommendations") or [] if rec_id is None or g.get("rec_id") == rec_id]
            out["awaiting_approval"] = {
                "analysis_id": pending["id"], "funding_version": pending.get("dataset_version"),
                "proposals": [{"rec_id": g.get("rec_id"), "label": g.get("rec_label"),
                               "matches": [self._fit(f) for f in g.get("fits") or []]} for g in groups]
                if review else "The proposals are held in server memory and were lost on a restart; re-run "
                               "Block Funding to see them.",
            }
        if not pinned and not pending:
            out["note"] = "Block Funding hasn't been run on this block."
        self._source(f"funding:{block_id}", f"Block {block_id} funding",
                     "Approved funding matches" + (" and proposals awaiting approval" if pending else ""))
        return out

    def funding_programs(self, search: list[str] | None = None, version: int | None = None) -> dict:
        d = self.data
        vrepo = DatasetVersionRepository(d.db)
        v = vrepo.get("funding", version) if version is not None else vrepo.latest("funding")
        if not v:
            raise ValueError("There is no Funding version" + (f" {version}." if version is not None else " yet."))
        recs = (d.db.table("dataset_records").select("data").eq("dataset_version_id", v["id"])
                .eq("record_type", "program").execute().data or [])
        words = [w.lower().strip() for w in search or [] if w.strip()]
        rows = []
        for r in recs:
            p = r.get("data") or {}
            hay = " ".join(str(p.get(k) or "") for k in ("program_name", "source_agency", "program_type",
                                                          "eligibility_notes", "geo_scope")).lower()
            if words and not any(w in hay for w in words):
                continue
            rows.append({
                "program": p.get("program_name"), "type": p.get("program_type"), "agency": p.get("source_agency"),
                "award_range": [p.get("award_amount_min"), p.get("award_amount_max")], "deadline": p.get("deadline"),
                "eligibility": _short(p.get("eligibility_notes"), 350), "geo_scope": p.get("geo_scope"),
                "extraction_confidence": p.get("extraction_confidence"),
                "ref": self.ref(f"{p.get('program_name')}: {p.get('source_name') or 'source'}",
                                p.get("source_url") or p.get("application_url"),
                                f"Funding v{v['version']}"),
            })
        self._source(f"programs:{v['version']}", f"Funding v{v['version']} catalogue",
                     f"{v.get('name') or ''} ({v.get('record_count')} programs)".strip())
        return {"funding_version": v["version"], "name": v.get("name"), "made_by": v.get("source"),
                "count": len(rows), "of": len(recs), "programs": rows[:25],
                "note": "Only the first 25 matches are listed; narrow the search for more." if len(rows) > 25 else None}

    # ── playbooks and plan history ──

    def playbook(self, key: str | None = None, rec_label: str | None = None) -> dict:
        t = self.data.templates()
        meta = {"version": t.get("version_number"), "updated_by": t.get("updated_by"),
                "status": t.get("status"), "from": "app" if t.get("source") == "app" else "the committed file (v0)"}
        p = pathway_for(rec_label, t) if rec_label else \
            next((x for x in t["pathways"] if x["key"] == key), None) if key else None
        by_key: dict[str, list[str]] = {}
        for label, k in (t.get("rec_types") or {}).items():
            by_key.setdefault(k, []).append(label)
        self._source("playbooks", f"Playbooks v{meta['version']}", "The playbooks in force (Playbooks tab)")
        if not p:
            listing = [{"key": x["key"], "name": x.get("name"), "who": x.get("who"), "steps": len(x.get("steps") or []),
                        "recommendation_types": sorted(by_key.get(x["key"], []))} for x in t["pathways"]]
            return {**meta, "playbooks": listing,
                    "note": (f"No playbook covers “{rec_label}”." if rec_label else
                             f"No playbook has key “{key}”." if key else None)}
        steps = upgrade_steps(copy.deepcopy(p.get("steps") or []))
        est = estimate(steps)
        try:
            history = PathwayTemplateRepository(self.data.db).history(10)
        except Exception:  # noqa: BLE001 — no table yet: the file is the only version
            history = []
        return {**meta, "key": p["key"], "name": p.get("name"), "who": p.get("who"), "question": p.get("branch"),
                "duration": weeks_text(est.get("fastest_weeks"), est.get("slowest_weeks")),
                "steps": self._steps(steps), "recommendation_types": sorted(by_key.get(p["key"], [])),
                "history": [{"version": h.get("version"), "by": h.get("updated_by"), "note": h.get("note"),
                             "at": h.get("created_at")} for h in history]}

    def plan_history(self, block_id: int, rec_id: int) -> dict:
        rec = self.data.rec(block_id, rec_id)
        try:
            revs = PlanRevisionRepository(self.data.db).list_for_rec(block_id, rec_id)
        except RevisionTableMissing:
            return {"rec": rec["rec_label"], "history": [], "note": "The plan audit table doesn't exist yet."}
        self._source(f"history:{rec_id}", f"{rec['rec_label']} plan history", "The plan's audit trail")
        return {"rec": rec["rec_label"], "plan": rec_row(rec)["plan"],
                "history": [{"revision": r.get("revision"), "action": r.get("action"), "by": r.get("made_by"),
                             "at": r.get("created_at"), "note": r.get("notes"), "based_on": r.get("base_revision"),
                             "changes": _short(r.get("changes"), 600)} for r in revs],
                "note": None if revs else "No draft has been saved or approved: the plan is still its playbook's."}

    # ── datasets and pipelines ──

    def datasets(self, kind: str | None = None) -> dict:
        d = self.data
        repo = DatasetVersionRepository(d.db)
        kinds = [kind] if kind else ["funding", "zoning", "general"]
        try:
            arepo = BlockAnalysisRepository(d.db)
            used = {k: [a.get("dataset_version") for a in arepo.pinned_for_kind(k).values()]
                    for k in ("funding", "zoning")}
        except DatasetTablesMissing:
            used = {}
        out = {}
        for k in kinds:
            versions = repo.list(k)
            out[k] = [{"version": v["version"], "name": v.get("name"), "note": v.get("note"),
                       "made_by": v.get("source"), "records": v.get("record_count"),
                       "summary": _short(v.get("summary"), 300), "created_by": v.get("created_by"),
                       "created_at": v.get("created_at"), "latest": i == 0,
                       **({"blocks_pinned_to_it": used[k].count(v["version"])} if k in used else {})}
                      for i, v in enumerate(versions)]
        self._source("datasets", "Dataset versions", "Every Funding, Zoning and general dataset version")
        return {"versions": out,
                "note": "made_by.type 'pipeline' names the pipeline_id and definition_version that produced it."}

    def pipelines(self, pipeline_id: int | None = None, kind: str | None = None) -> dict:
        db = self.data.db
        if pipeline_id is None:
            q = db.table("pipelines").select("id, kind, name, definition_version, created_by, created_at").order("id")
            rows = (q.eq("kind", kind) if kind else q).execute().data or []
            runs = db.table("pipeline_runs").select("pipeline_id, dataset_version, created_at") \
                .order("id", desc=True).execute().data or []
            last: dict[int, dict] = {}
            for r in runs:
                last.setdefault(r["pipeline_id"], r)
            self._source("pipelines", "Pipelines", "The pipelines built in the Pipeline development tab")
            return {"pipelines": [{**p, "loaded": bool(last.get(p["id"])),
                                   "last_load": last.get(p["id"])} for p in rows],
                    "note": "definition_version 0 = never loaded."}

        p = (db.table("pipelines").select("id, kind, name, definition, definition_version, created_by, created_at")
             .eq("id", pipeline_id).execute().data or [None])[0]
        if not p:
            raise ValueError(f"There is no pipeline {pipeline_id}.")
        d = p.get("definition") or {}

        def fields(schema: dict | None) -> list[dict]:
            return [{"name": f.get("name"), "type": f.get("type"), "required": f.get("required"),
                     "description": _short(f.get("description"), 160)} for f in (schema or {}).get("fields") or []]

        sources = [{"id": s.get("id"), "label": s.get("label"), "kind": s.get("kind"),
                    "ref": self.ref(s.get("label"), s.get("url"), f"Source of pipeline “{p['name']}”")}
                   for s in d.get("sources") or []]
        steps = [{"id": s.get("id"), "type": s.get("type"), "does": s.get("note"), "inputs": s.get("inputs"),
                  "status": s.get("status"), "config": _short(s.get("config"), 240)} for s in d.get("steps") or []]
        runs = (db.table("pipeline_runs").select("id, definition_version, name, coverage, dataset_version, "
                                                 "created_by, created_at")
                .eq("pipeline_id", pipeline_id).order("id", desc=True).limit(5).execute().data or [])

        def coverage(c: dict | None) -> Any:
            if not c:
                return None
            per = c if "fields" not in c else {"records": c}
            out = {}
            for oid, o in per.items():
                fs = (o or {}).get("fields") or {}
                gaps = {n: f"{x.get('missing')} missing" for n, x in fs.items() if x.get("missing")}
                total = max((x.get("filled", 0) + x.get("missing", 0) for x in fs.values()), default=0)
                out[oid] = {"records": total, "fields_with_gaps": dict(list(gaps.items())[:8])}
            return out

        self._source(f"pipeline:{pipeline_id}", f"Pipeline “{p['name']}”", "Its definition, sources, steps and runs")
        return {
            "id": p["id"], "kind": p["kind"], "name": p["name"], "definition_version": p.get("definition_version"),
            "created_by": p.get("created_by"), "created_at": p.get("created_at"),
            "goal": _short(d.get("goal"), 800), "sources": sources, "steps": steps,
            "fields": fields(d.get("schema")),
            "outputs": [{"id": o.get("id"), "name": o.get("name"), "fields": [f["name"] for f in fields(o.get("schema"))]}
                        for o in d.get("outputs") or []],
            "runs": [{"id": r["id"], "definition_version": r.get("definition_version"), "name": r.get("name"),
                      "dataset_version": r.get("dataset_version"), "by": r.get("created_by"), "at": r.get("created_at"),
                      "coverage": coverage(r.get("coverage"))} for r in runs],
            "note": "Steps run in order over the sources; each step was proposed by the pipeline agent and approved "
                    "by the planner. A run's Load wrote the dataset version it names.",
        }

    def general_table(self, table: str, block_id: int | None = None, limit: int = 30) -> dict:
        db = self.data.db
        known: set[str] = set()
        for v in DatasetVersionRepository(db).list("general"):
            known.update((v.get("summary") or {}).get("tables") or {})
        if not GP_TABLE.match(table or "") or table not in known:
            raise ValueError(f"No general pipeline wrote a table named {table!r}. Tables: {sorted(known) or 'none'}.")
        n = max(1, min(int(limit or 30), 50))
        note = "_pipeline_id is the pipeline that wrote the row; _evidence says how each value was derived."
        try:
            q = db.table(table).select("*").limit(n)
            rows = (q.eq("block_id", block_id) if block_id is not None else q).execute().data or []
        except Exception:  # noqa: BLE001 — no block_id column: show the table unfiltered
            rows = db.table(table).select("*").limit(n).execute().data or []
            note = f"This table has no block_id column, so it isn't filtered to block {block_id}. " + note
        self._source(f"gp:{table}", table, "A table written by a general pipeline")
        return {"table": table, "rows": [{k: (_short(v, 300) if k == "_evidence" else v) for k, v in r.items()}
                                         for r in rows],
                "note": note}
