"""
BlockAssistant: "Ask about this block" on the Blocks tab.

A starting point for the SME who works a block: questions about the block, its
recommendations, their plans and how long they would take, or (scope "all")
about all the blocks at once. It reads only this app's data, through tools, and
changes nothing.

Every turn ends with the `answer` tool: a one-line headline, a short body, at
most one visual, what the answer assumes, next actions and what to show on the
map. The visual names ids only; the numbers drawn (durations, scores, block
lists) are filled in here from the same data the tools read, so a chart can
never disagree with the data. The sources shown under an answer are the tools
the turn actually called, for the same reason.

The chat is held by the browser and sent with each turn as plain text (each
question and the answer's headline and body), not the raw tool exchange: a
follow-up re-reads whatever it needs, which keeps requests small.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from supabase import Client

from backend.agents.pipeline_builder import _create
from backend.agents.zoning import approved_items
from backend.api.plans import _plan_response, list_plans
from backend.repositories.datasets import BlockAnalysisRepository, DatasetTablesMissing
from backend.repositories.recommendations import BlockRepository
from backend.tools.llm_extractor import _get_client

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 8
HISTORY_TURNS = 12
WEEKS_PER_MONTH = 4.345

# Score columns on `blocks`, with the dimension names block_recommendations uses.
DIMENSIONS = [
    ("form_score", "Form"), ("density_score", "Density"), ("proximity_score", "Proximity"),
    ("connectivity_score", "Connectivity"), ("parks_score", "Parks & Public Spaces"),
    ("pedestrian_score", "Pedestrian & Bike Amenities"), ("safety_score", "Personal Safety"),
    ("traffic_score", "Traffic Safety"), ("aesthetics_score", "Aesthetics"),
]
BLOCK_COLUMNS = ("id, street_name, intersection_a, intersection_b, sop_index_norm, tract_geoid, median_hh_income, "
                 + ", ".join(c for c, _ in DIMENSIONS))


@lru_cache(maxsize=1)
def _features_dict() -> dict:
    """SoP feature code -> {description, values}, from 'SoP 150 Feature List.csv'."""
    return json.loads((Path(__file__).resolve().parent.parent / "data" / "sop_features.json").read_text(encoding="utf-8"))


def _weeks_text(lo: float | None, hi: float | None) -> str | None:
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


# ── The data the tools read, loaded once per turn ────────────────────────────

class Data:
    def __init__(self, db: Client):
        self.db = db
        self._blocks: list[dict] | None = None
        self._recs: dict[int, list[dict]] = {}
        self._reviews: dict[int, dict] = {}
        self._all_recs: list[dict] | None = None

    def blocks(self) -> list[dict]:
        if self._blocks is None:
            self._blocks = self.db.table("blocks").select(BLOCK_COLUMNS).order("id").execute().data or []
        return self._blocks

    def block(self, block_id: int) -> dict:
        b = next((x for x in self.blocks() if x["id"] == block_id), None)
        if not b:
            raise ValueError(f"There is no block {block_id}.")
        return b

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


def _rec_row(r: dict) -> dict:
    est = r.get("estimate") or {}
    return {
        "rec_id": r["rec_id"], "label": r["rec_label"], "dimension": r.get("dimension"),
        "direction": r.get("direction"), "predicted_score_increase": r.get("predicted_score_increase"),
        "playbook": r.get("pathway_name") or "none (no playbook covers this type yet)",
        "plan": ("approved" if r.get("plan_status") == "final" else "playbook default")
                + (f"; a draft by {r.get('draft_by')} awaits approval" if r.get("draft_pending") else ""),
        "duration": _weeks_text(est.get("fastest_weeks"), est.get("slowest_weeks")),
        "duration_weeks": [est.get("fastest_weeks"), est.get("slowest_weeks")] if est else None,
        "zoning": r.get("zoning_best") or "not checked or not resolved",
        "approved_funding_matches": r.get("funding_count", 0),
    }


# ── Timelines, shared by the tool and the visual ─────────────────────────────

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
                         "weeks": [lo, hi], "text": _weeks_text(lo, hi),
                         "untimed_steps": est.get("untimed") or []})
            end_lo, end_hi = max(end_lo, start_lo + lo), max(end_hi, start_hi + hi)
        start_lo, start_hi = end_lo, end_hi
    return {"arrangement": arrangement, "bars": bars, "untimed": untimed,
            "total_weeks": [round(start_lo, 1), round(start_hi, 1)], "total": _weeks_text(start_lo, start_hi)}


# ── Tools ────────────────────────────────────────────────────────────────────

_IDS = {"type": "array", "items": {"type": "integer"}}

TOOLS = [
    {"name": "block_overview",
     "description": "One block's facts: street, cross streets, census tract and ACS figures, its SoP score and each "
                    "dimension score with the average and rank across all blocks, where its funding and zoning reviews "
                    "stand, and how many of its plans are approved.",
     "input_schema": {"type": "object", "properties": {"block_id": {"type": "integer"}}, "required": ["block_id"]}},
    {"name": "recommendations",
     "description": "A block's recommendations: label, dimension, direction, predicted score increase, the playbook it "
                    "follows, whether its plan is approved or still the playbook default, its duration estimate, its "
                    "approved zoning outcome and how many approved funding matches it has.",
     "input_schema": {"type": "object", "properties": {"block_id": {"type": "integer"}}, "required": ["block_id"]}},
    {"name": "plan",
     "description": "One recommendation's implementation plan on a block: every step (what, how, who, how long, what it "
                    "waits on, when it applies, its source), the duration estimate, the zoning finding and approved funding.",
     "input_schema": {"type": "object", "properties": {"block_id": {"type": "integer"}, "rec_id": {"type": "integer"}},
                      "required": ["block_id", "rec_id"]}},
    {"name": "street_features",
     "description": "What the State of Place survey recorded on the block's street today, feature by feature, and the "
                    "recommended state where it differs. Without `search`: the features present plus those that differ. "
                    "With `search`: every feature whose name or code contains one of the words (e.g. [\"crosswalk\", \"zebra\"]).",
     "input_schema": {"type": "object", "properties": {"block_id": {"type": "integer"},
                                                       "search": {"type": "array", "items": {"type": "string"}}},
                      "required": ["block_id"]}},
    {"name": "timeline",
     "description": "How long a set of a block's recommendations takes, computed from each one's plan estimate. "
                    "arrangement: together (all start at once), one_after_another (in the order of rec_ids), or phases "
                    "(each inner list together, the phases one after another).",
     "input_schema": {"type": "object", "properties": {
         "block_id": {"type": "integer"}, "rec_ids": _IDS,
         "arrangement": {"type": "string", "enum": ["together", "one_after_another", "phases"]},
         "phases": {"type": "array", "items": _IDS}},
         "required": ["block_id", "rec_ids", "arrangement"]}},
    {"name": "find_blocks",
     "description": "All blocks, one row each: street, cross streets, SoP score, number of recommendations, the biggest "
                    "single lift, and where funding and zoning reviews stand. Filter by street (substring) or by a "
                    "recommendation type (substring of its label; each row then shows that recommendation's predicted "
                    "increase). Sort by sop (ascending), sop_desc, or a dimension name (ascending). include_dimensions "
                    "adds every dimension score.",
     "input_schema": {"type": "object", "properties": {
         "street": {"type": "string"}, "rec_label": {"type": "string"},
         "sort": {"type": "string"}, "include_dimensions": {"type": "boolean"}}}},
    {"name": "answer",
     "description": "Give your answer. Call this exactly once, last, after reading what you need.",
     "input_schema": {"type": "object", "properties": {
         "headline": {"type": "string", "description": "The direct answer in one sentence, under 16 words."},
         "body": {"type": "string", "description": "Two or three short sentences, or a few '- ' bullets, that support the "
                                                   "headline. **bold** allowed. Under 110 words."},
         "visual": {"type": "object", "description": "At most one chart or list; the app draws it from the data.",
                    "properties": {
                        "type": {"type": "string", "enum": ["timeline", "scores", "recommendations", "blocks"]},
                        "rec_ids": _IDS, "block_ids": _IDS,
                        "arrangement": {"type": "string", "enum": ["together", "one_after_another", "phases"]},
                        "phases": {"type": "array", "items": _IDS},
                        "phase_names": {"type": "array", "items": {"type": "string"}}},
                    "required": ["type"]},
         "assumptions": {"type": "array", "items": {"type": "string"},
                         "description": "What the answer assumes or doesn't know yet, one short line each."},
         "actions": {"type": "array", "description": "Two or three next steps, as buttons.", "items": {
             "type": "object", "properties": {
                 "label": {"type": "string", "description": "Button text, under 5 words."},
                 "type": {"type": "string", "enum": ["ask", "open_recommendation", "run_funding", "run_zoning",
                                                     "highlight_blocks", "open_block"]},
                 "question": {"type": "string", "description": "For ask: the follow-up question it sends."},
                 "rec_id": {"type": "integer"}, "block_ids": _IDS},
             "required": ["label", "type"]}},
         "map": {"type": "object", "description": "What to show on the map.", "properties": {
             "rec_ids": {**_IDS, "description": "The block's recommendations this answer is about (labelled on its street)."},
             "block_ids": {**_IDS, "description": "Blocks to highlight (across-block answers)."}}}},
         "required": ["headline", "body"]}},
]

TOOL_PROGRESS = {
    "block_overview": "Reading block {block_id}'s scores and reviews",
    "recommendations": "Reading block {block_id}'s recommendations",
    "plan": "Reading a plan's steps",
    "street_features": "Reading the street survey",
    "timeline": "Working out durations",
    "find_blocks": "Looking across all blocks",
}


class Tools:
    def __init__(self, data: Data):
        self.data = data
        self.sources: list[dict] = []

    def _source(self, key: str, label: str, detail: str) -> None:
        if not any(s["key"] == key for s in self.sources):
            self.sources.append({"key": key, "label": label, "detail": detail})

    def block_overview(self, block_id: int) -> dict:
        d = self.data
        b, rows, avg = d.block(block_id), d.blocks(), d.averages()
        recs = d.recs(block_id)
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
            "reviews": d.reviews(block_id),
            "plans": {"recommendations": len(recs),
                      "approved": sum(1 for r in recs if r.get("plan_status") == "final"),
                      "drafts_waiting": sum(1 for r in recs if r.get("draft_pending")),
                      "playbook_default": sum(1 for r in recs if r.get("plan_status") != "final")},
            "note": "Scores are 0-100. The SoP score is State of Place's overall index for the block (normalised).",
        }

    def recommendations(self, block_id: int) -> dict:
        recs = sorted(self.data.recs(block_id), key=lambda r: -(r.get("predicted_score_increase") or -1e9))
        self._source(f"recs:{block_id}", f"Block {block_id} recommendations",
                     "State of Place's ranked recommendations, with each plan's status and estimate")
        return {"block_id": block_id, "reviews": self.data.reviews(block_id),
                "recommendations": [_rec_row(r) for r in recs],
                "note": "Listed by predicted score increase, highest first. predicted_score_increase is State of "
                        "Place's model output, ranked within its dimension in the source file; the file doesn't say "
                        "whether it is the dimension score or the overall index, so don't claim either. Durations "
                        "come from the approved plan, else the playbook's default steps (a strawman, not SME-reviewed)."}

    def plan(self, block_id: int, rec_id: int) -> dict:
        d = self.data
        rec = d.rec(block_id, rec_id)
        block = BlockRepository(d.db).get_with_recs(block_id)
        raw = next(r for r in block["recommendations"] if r["id"] == rec_id)
        p = _plan_response(d.db, block, raw)
        steps = p.get("steps") or []
        num = {s.get("id"): i for i, s in enumerate(steps, start=1)}
        est = p.get("estimate") or {}
        self._source(f"plan:{rec_id}", f"{rec['rec_label']} plan",
                     "Approved plan" if p.get("status") == "final" else "Playbook default steps (not yet approved)")
        return {
            "rec": _rec_row(rec), "status": "approved" if p.get("status") == "final" else "playbook default",
            "reviewed_by": p.get("reviewed_by"), "revision": p.get("revision"),
            "draft_waiting": bool(p.get("draft")),
            "duration": _weeks_text(est.get("fastest_weeks"), est.get("slowest_weeks")),
            "untimed_steps": est.get("untimed") or [],
            "zoning": p.get("zoning_summary"),
            "funding": [{k: f.get(k) for k in ("program_name", "fit_score", "amount", "source_agency")}
                        for f in p.get("funding") or []],
            "steps": [{"n": num.get(s.get("id")), "title": s.get("title"), "how": s.get("how"), "who": s.get("owner"),
                       "actor": s.get("actor"), "also": s.get("secondary_owner"), "time": s.get("time"),
                       "after": [num.get(x) for x in s.get("depends_on") or [] if x in num],
                       "after_mode": s.get("depends_mode") or "all", "only_if": s.get("condition") or None,
                       "source": s.get("source")} for s in steps],
        }

    def street_features(self, block_id: int, search: list[str] | None = None) -> dict:
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

    def run(self, name: str, args: dict) -> tuple[Any, bool]:
        fn = getattr(self, name, None) if name in TOOL_PROGRESS else None
        if not fn:
            return {"error": f"Unknown tool {name}"}, True
        try:
            return fn(**args), False
        except (ValueError, TypeError, KeyError) as e:
            return {"error": str(e)}, True


# ── The answer, filled in from the data ──────────────────────────────────────

def _finish(raw: dict, data: Data, tools: Tools, scope: str, block_id: int | None) -> dict:
    """The model's answer with every number it shows read from the data, and anything that doesn't resolve dropped."""
    on_block = {r["rec_id"]: r for r in data.recs(block_id)} if block_id else {}
    known = {b["id"]: b for b in data.blocks()}
    ans: dict[str, Any] = {
        "headline": (raw.get("headline") or "").strip(),
        "body": (raw.get("body") or "").strip(),
        "assumptions": [a.strip() for a in raw.get("assumptions") or [] if isinstance(a, str) and a.strip()][:5],
    }

    v = raw.get("visual") or {}
    rec_ids = [i for i in v.get("rec_ids") or [] if i in on_block]
    try:
        if v.get("type") == "timeline" and block_id and (rec_ids or v.get("phases")):
            phases = [[i for i in p if i in on_block] for p in v.get("phases") or []]
            arr = v.get("arrangement") or "together"
            t = timeline(data, block_id, rec_ids or [i for p in phases for i in p], arr, phases)
            if t["bars"]:
                names = [n for n in v.get("phase_names") or [] if isinstance(n, str)]
                ans["visual"] = {"type": "timeline", **t, "phase_names": names}
                tools._source("durations", "Playbook durations",
                              "Each plan's estimate along its longest chain of steps (approved plan, else playbook default)")
        elif v.get("type") == "scores" and block_id:
            b, avg = data.block(block_id), data.averages()
            ans["visual"] = {"type": "scores", "sop": _num(b.get("sop_index_norm")), "sop_average": avg["sop_index_norm"],
                             "rows": [{"dimension": n, "score": _num(b.get(c)), "average": avg[c]} for c, n in DIMENSIONS]}
        elif v.get("type") == "recommendations" and rec_ids:
            ans["visual"] = {"type": "recommendations", "rows": [
                {"rec_id": i, "label": on_block[i]["rec_label"], "dimension": on_block[i].get("dimension"),
                 "direction": on_block[i].get("direction"), "gain": on_block[i].get("predicted_score_increase"),
                 "duration": (_rec_row(on_block[i]))["duration"]} for i in rec_ids]}
        elif v.get("type") == "blocks":
            ids = [i for i in v.get("block_ids") or [] if i in known]
            if ids:
                ans["visual"] = {"type": "blocks", "rows": [
                    {"block_id": i, "street": known[i].get("street_name"), "sop": _num(known[i].get("sop_index_norm"))}
                    for i in ids]}
    except ValueError as e:   # an id the data doesn't hold: answer without the visual
        log.warning("block assistant visual dropped: %s", e)
    ans["sources"] = [{"label": s["label"], "detail": s["detail"]} for s in tools.sources]

    actions = []
    for a in raw.get("actions") or []:
        t, label = a.get("type"), (a.get("label") or "").strip()
        if not label:
            continue
        if t == "ask" and (a.get("question") or "").strip():
            actions.append({"type": "ask", "label": label, "question": a["question"].strip()})
        elif t == "open_recommendation" and a.get("rec_id") in on_block:
            actions.append({"type": t, "label": label, "rec_id": a["rec_id"]})
        elif t in ("run_funding", "run_zoning") and block_id:
            actions.append({"type": t, "label": label})
        elif t == "highlight_blocks" and [i for i in a.get("block_ids") or [] if i in known]:
            actions.append({"type": t, "label": label, "block_ids": [i for i in a["block_ids"] if i in known]})
        elif t == "open_block" and len(a.get("block_ids") or []) == 1 and a["block_ids"][0] in known:
            actions.append({"type": t, "label": label, "block_ids": a["block_ids"]})
    ans["actions"] = actions[:4]

    m = raw.get("map") or {}
    ans["map"] = {
        "block_id": block_id,
        "recs": [{"rec_id": i, "label": on_block[i]["rec_label"]} for i in m.get("rec_ids") or [] if i in on_block][:6],
        "block_ids": [i for i in m.get("block_ids") or [] if i in known],
    }
    if not ans["headline"]:
        ans["headline"], ans["body"] = ans["body"], ""
    return ans


# ── The turn ─────────────────────────────────────────────────────────────────

SYSTEM = """You are the block assistant in a planning app for the City of Bowie, Maryland (Prince George's County).
The app turns State of Place (SoP) recommendations into implementation plans. Each of Bowie's {n_blocks} street
blocks has a SoP score (0-100), dimension scores, and recommendations (e.g. "Arcades", "Crosswalks 2"). Each
recommendation follows a playbook (standard steps) and gets an implementation plan per block, which an SME (subject
matter expert) edits and approves. Funding and zoning are each reviewed per block by an agent run plus SME approval.

You answer the SME who works a block. It is a starting point for their work: be useful, quick and honest.

How to answer:
- Read before you answer: use the tools; never state a number, duration, status or step the tools didn't give you.
  Durations come from plan estimates (approved plan, else the playbook default, which is a strawman not yet
  SME-reviewed). For durations, sequencing or a first plan, call `timeline`.
- Finish by calling `answer` once. The headline is the direct answer, like "About 20 months to 4 years if all five
  start together." The body backs it up briefly; name recommendations by their label.
- Pick the visual that helps: `timeline` for durations, sequencing or a first plan; `scores` for why a block scores
  what it does; `recommendations` for a set of the block's recommendations; `blocks` for a list of blocks.
  The app draws it from the data, so only give ids.
- `assumptions`: always say what the answer rests on or lacks, e.g. "Funding hasn't been run, so waits for funding
  aren't included." Check the block's reviews; if the question depends on funding or zoning that isn't approved,
  say so and offer a run_funding or run_zoning action.
- `actions`: two or three next steps the SME would click, e.g. "Sequence them instead", "Leave out Bars/Night Clubs"
  (type ask, with the full question), or open_recommendation for a plan to open.
- `map`: on a block, the recommendations your answer is about (labelled on its street); across blocks, the block ids.
- "The top N" means the N recommendations with the largest predicted score increase on the block.
- Recommendations overlap when they share a playbook, touch the same feature or the same crossing (e.g. Crosswalks 2
  and Zebra Striping 2 are both at intersection 2), or need the same permit or agency; they conflict when one
  removes what the other needs. Say which, from the data, and mark judgement as such.
- A first (initial) plan: phases of the block's recommendations, using `timeline` with arrangement "phases": quick,
  low-dependency street work first, things that share steps or a crossing together, long-lead items (private
  development, land use, anything needing a special exception) started early. Name each phase in phase_names. Say
  it's a starting point: each recommendation's own plan is still edited and approved on its page.
- If you need the SME's judgement, give what you found first, then one specific question.
- Be concrete about Bowie, Prince George's County and Maryland agencies; write "(confirm)" after any agency, permit
  or body you are not sure of.
- Plain words, no headings, no preamble. Never say something was saved or changed: you can't change anything."""


def _context_text(data: Data, scope: str, block_id: int | None, rec_ids: list[int]) -> str:
    if scope == "all" or not block_id:
        lines = [f"Scope: all {len(data.blocks())} blocks."]
        if block_id:
            b = data.block(block_id)
            lines.append(f"The SME has block {block_id} ({b.get('street_name')}) open.")
        return "\n".join(lines)
    b = data.block(block_id)
    lines = [f"Scope: block {block_id}, {b.get('street_name')} "
             f"({b.get('intersection_a') or '?'} → {b.get('intersection_b') or '?'})."]
    for rid in rec_ids:
        try:
            lines.append(f"About recommendation {rid}: {data.rec(block_id, rid)['rec_label']}.")
        except ValueError:
            pass
    return "\n".join(lines)


def ask(db: Client, question: str, scope: str = "block", block_id: int | None = None,
        rec_ids: list[int] | None = None, history: list[dict] | None = None,
        progress: Callable[[str], None] | None = None) -> dict:
    """One question and its answer. history: [{role: user | assistant, text}], oldest first."""
    data = Data(db)
    tools = Tools(data)
    scope = "all" if scope == "all" or not block_id else "block"
    if block_id:
        data.block(block_id)

    messages: list[dict] = []
    for h in (history or [])[-HISTORY_TURNS:]:
        role, text = h.get("role"), (h.get("text") or "").strip()
        if role not in ("user", "assistant") or not text:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n\n" + text
        else:
            messages.append({"role": role, "content": text})
    if messages and messages[0]["role"] == "assistant":
        messages.pop(0)
    if messages and messages[-1]["role"] == "user":
        messages.pop()
    messages.append({"role": "user", "content": [
        {"type": "text", "text": f"<context>\n{_context_text(data, scope, block_id, rec_ids or [])}\n</context>"},
        {"type": "text", "text": question.strip()}]})

    client = _get_client()
    system = [{"type": "text", "text": SYSTEM.format(n_blocks=len(data.blocks()))}]
    final: dict | None = None
    text_reply = ""
    for _ in range(MAX_TOOL_ROUNDS):
        resp = _create(client, model=MODEL, max_tokens=8000, system=system, tools=TOOLS, messages=messages,
                       cache_control={"type": "ephemeral"}, output_config={"effort": "medium"})
        content = [b.model_dump(mode="json", exclude_none=True) for b in resp.content]
        messages.append({"role": "assistant", "content": content})
        text_reply = "\n\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text")) or text_reply
        uses = [b for b in content if b.get("type") == "tool_use"]
        answer_use = next((b for b in uses if b["name"] == "answer"), None)
        if answer_use:
            final = answer_use.get("input") or {}
            break
        if resp.stop_reason == "refusal":
            return {"headline": "The model declined to answer this.", "body": "", "sources": [], "actions": [],
                    "assumptions": [], "map": {"block_id": block_id, "recs": [], "block_ids": []}}
        if resp.stop_reason != "tool_use" or not uses:
            break
        results = []
        for b in uses:
            args = b.get("input") or {}
            if progress and b["name"] in TOOL_PROGRESS:
                progress(TOOL_PROGRESS[b["name"]].format(block_id=args.get("block_id", "")))
            result, is_error = tools.run(b["name"], args)
            results.append({"type": "tool_result", "tool_use_id": b["id"], "content": json.dumps(result, default=str),
                            **({"is_error": True} if is_error else {})})
        messages.append({"role": "user", "content": results})

    if final is None:
        # Ended in plain text, or ran out of rounds: show what it said.
        first, _, rest = (text_reply or "I couldn't finish that answer. Try asking it more narrowly.").partition("\n")
        final = {"headline": first.strip(), "body": rest.strip()}
    if progress:
        progress("Writing the answer")
    return _finish(final, data, tools, scope, block_id)
