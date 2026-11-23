"""
FeasibilityAgent: an LLM judgement of how likely one recommendation on one
block is to get done.

The plan, the funding and the zoning each answer their own question and are
each approved by the SME. None of them says whether the whole thing is a good
candidate: whether the approved money covers what the work would cost, and how
the zoning path, the plan's length and the parties who must act weigh against
it. That judgement is made here, per (block, recommendation), and only once all
three inputs are approved:

- the recommendation's implementation plan is approved (implementation_plans);
- the block's pinned Block Funding analysis is complete (approved);
- the block's pinned Block Zoning run is approved and covers the recommendation.

The server gathers the evidence (every fact tagged with the reference it came
from, R1, R2 ...) and does the arithmetic; the model estimates the cost,
weighs the evidence and gives a 0-100 score with its reasons. The assessment
waits in_review until the SME accepts the score or sets another with a note.
Then it is the recommendation's current one, and the block's gold
feasibility_score becomes the best current score (orchestrator.compute_gold_for_block).

Each assessment records the inputs it read (plan revision, funding and zoning
runs), so one whose inputs have changed since shows as stale.

The SME can send an assessment back with their own input ("the plan means
depave and landscape, not redevelopment", "the owner has applied for a
permit"). The rework is a new run that reads the same evidence plus the SME's
input, cited like any other reference, and the earlier assessment, and says
what changed. SME input is kept in the run's `inputs.guidance` and carries
forward to later runs of the same recommendation, since what the SME knows
stays true when the plan or funding changes.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from supabase import Client

from backend.agents.zoning import ZoningAgent, reviewed_finding
from backend.api.blocks import _fit_from_signal
from backend.db.session import get_supabase
from backend.repositories.datasets import BlockAnalysisRepository, DatasetTablesMissing
from backend.repositories.feasibility import FeasibilityRepository
from backend.repositories.funding import FundingRepository
from backend.repositories.plans import PlanRepository, PlanTableMissing
from backend.repositories.recommendations import BlockRepository
from backend.tools.llm_extractor import structured_call
from backend.tools.pathways import load_templates, pathway_for
from backend.tools.plan_graph import estimate, upgrade_steps

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
# A run still 'running' after this long was lost (the server restarted mid-run).
LOST_AFTER = timedelta(minutes=15)
# Opus calls take 30-60 s; a few at once keeps a block's batch quick without a burst.
_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="feasibility")

FACTORS = {
    "funding": "Funding adequacy",
    "zoning": "Zoning path",
    "timeline": "Timeline and deadlines",
    "dependencies": "Outside parties",
    "need": "Need and impact",
}
RATINGS = ["strong", "adequate", "weak", "blocking"]
BANDS = [(70, "strong", "Strong candidate"), (50, "possible", "Possible"),
         (30, "weak", "Weak"), (0, "not_viable", "Not viable as it stands")]

INPUT_KEYS = ("plan_revision", "funding_analysis_id", "zoning_analysis_id")
INPUT_NAMES = {"plan_revision": "the implementation plan", "funding_analysis_id": "the block's funding analysis",
               "zoning_analysis_id": "the block's zoning review"}


class FeasibilityError(ValueError):
    """A request the rules refuse (not ready, wrong status, missing note). Shown to the user as is."""


def band_for(score: int | None) -> str | None:
    if score is None:
        return None
    return next(key for floor, key, _ in BANDS if score >= floor)


# ── What the block's approved inputs are right now ───────────────────────────

def context(db: Client, block_id: int) -> dict:
    """The block, its approved plans, and its pinned funding and zoning runs: one read for every recommendation."""
    block = BlockRepository(db).get_with_recs(block_id)
    if not block:
        raise FeasibilityError(f"Block {block_id} not found.")
    try:
        plans = {p["rec_id"]: p for p in PlanRepository(db).list_for_block(block_id) if p.get("status") == "final"}
    except PlanTableMissing:
        plans = {}
    try:
        funding = BlockAnalysisRepository(db).pinned(block_id, "funding")
    except DatasetTablesMissing:
        funding = None
    zoning, items = ZoningAgent().approved(block_id, db)
    return {"block": block, "recs": {r["id"]: r for r in block.get("recommendations", [])},
            "plans": plans, "funding": funding, "zoning": zoning, "zoning_items": items}


def _zoning_item(ctx: dict, rec_id: int) -> dict | None:
    return next((i for i in ctx["zoning_items"] or [] if rec_id in (i.get("rec_ids") or [])), None)


def inputs_for(ctx: dict, rec_id: int) -> dict:
    """The approved inputs an assessment of this recommendation reads now."""
    plan = ctx["plans"].get(rec_id) or {}
    f, z = ctx["funding"], ctx["zoning"]
    return {
        "plan_revision": plan.get("revision"),
        "plan_approved_by": plan.get("reviewed_by"),
        "funding_analysis_id": f["id"] if f else None,
        "funding_version": f.get("dataset_version") if f else None,
        "zoning_analysis_id": z["id"] if z and ctx["zoning_items"] is not None else None,
        "zoning_version": z.get("dataset_version") if z else None,
    }


def missing_for(ctx: dict, rec: dict) -> list[dict]:
    """What stops this recommendation being assessed: [{key, text}], empty when it is ready."""
    out = []
    if rec["id"] not in ctx["plans"]:
        out.append({"key": "plan", "text": "Its implementation plan is not approved yet."})
    f = ctx["funding"]
    if not f or f.get("status") != "complete":
        out.append({"key": "funding", "text": "The block has no approved funding analysis."})
    if ctx["zoning_items"] is None:
        out.append({"key": "zoning", "text": "The block has no approved zoning review."})
    elif _zoning_item(ctx, rec["id"]) is None:
        out.append({"key": "zoning", "text": "The block's approved zoning review does not cover this recommendation. "
                                             "Run Block Zoning again and approve it."})
    return out


def changed_inputs(row: dict, now: dict) -> list[str]:
    """Which approved inputs changed since this assessment read them, by name."""
    was = row.get("inputs") or {}
    return [INPUT_NAMES[k] for k in INPUT_KEYS if was.get(k) != now.get(k)]


# ── The evidence the model reads ─────────────────────────────────────────────

class _Refs:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, label: str, url: str | None = None, detail: str | None = None) -> str:
        rid = f"R{len(self.items) + 1}"
        self.items.append({"id": rid, "label": label, "url": url, "detail": detail})
        return rid


def _days_until(iso: str | None) -> int | None:
    try:
        return (date.fromisoformat(str(iso)[:10]) - date.today()).days
    except (TypeError, ValueError):
        return None


def _fits(db: Client, ctx: dict, rec_id: int) -> list[dict]:
    """The recommendation's approved funding in the block's pinned analysis, strongest first."""
    signals = FundingRepository(db).get_for_block(ctx["block"]["id"], reviewed_only=True,
                                                  analysis_id=ctx["funding"]["id"])
    fits = [_fit_from_signal(s) for s in signals if s.get("rec_id") == rec_id]
    return sorted(fits, key=lambda f: f.get("fit_score") or 0.0, reverse=True)


def build_evidence(db: Client, ctx: dict, rec: dict, guidance: list[dict] | None = None) -> dict:
    """
    {facts, references, funding_total}: everything the model may rely on, each fact tagged with its reference.
    guidance: the SME's input for this recommendation, oldest first ([{text, by, at}]).
    """
    block, refs = ctx["block"], _Refs()
    templates = load_templates(db)
    pathway = pathway_for(rec.get("rec_label"), templates) or {}
    plan = ctx["plans"][rec["id"]]
    steps = upgrade_steps(plan.get("steps") or [])
    est = estimate(steps)

    r_rec = refs.add("State of Place recommendations for this block",
                     detail=f"Block {block['id']}, recommendation rank {rec.get('rec_rank')}")
    r_block = refs.add("State of Place block data with ACS demographics for the census tract",
                       detail=f"Tract {block.get('tract_geoid') or 'unknown'}")
    r_plan = refs.add(f"Approved implementation plan, revision {plan.get('revision') or '—'}",
                      detail=f"Approved by {plan.get('reviewed_by') or 'the SME'} on {(plan.get('finalized_at') or '')[:10]}"
                             + (f"; playbook “{pathway.get('name')}”" if pathway.get("name") else ""))

    item = _zoning_item(ctx, rec["id"])
    finding = reviewed_finding(item, ctx["zoning"])
    z_refs = finding.get("references") or []
    r_zone = refs.add(f"Approved zoning review, Block Zoning run #{ctx['zoning']['id']} "
                      f"(Zoning v{ctx['zoning'].get('dataset_version')})",
                      url=next((r.get("url") for r in z_refs if r.get("url")), None),
                      detail="; ".join(r.get("label") for r in z_refs[:3] if r.get("label")) or None)

    fits = _fits(db, ctx, rec["id"])
    funding_facts, total = [], 0.0
    for f in fits:
        rid = refs.add(f.get("program_name") or "Funding program", url=f.get("source_url") or f.get("application_url"),
                       detail=" · ".join(x for x in (f.get("source_agency"), f.get("source_name"),
                                                     "added by the SME" if f.get("added_by_reviewer") else None) if x))
        amount = f.get("planned_amount")
        if amount and amount > 0:
            total += float(amount)
        days = _days_until(f.get("deadline"))
        funding_facts.append({
            "ref": rid, "program": f.get("program_name"), "type": f.get("program_type"), "agency": f.get("source_agency"),
            "amount_this_recommendation_would_ask": amount, "amount_basis": f.get("amount_basis"),
            "award_floor": f.get("award_amount_min"), "award_ceiling": f.get("award_amount_max"),
            "deadline": f.get("deadline"),
            "deadline_status": None if days is None else ("passed" if days < 0 else f"in {days} days"),
            "fit_score_0_to_1": f.get("fit_score"), "why_it_fits": f.get("narrative"),
            "added_by_sme": bool(f.get("added_by_reviewer")),
        })

    review = finding.get("review") or {}
    facts = {
        "today": date.today().isoformat(),
        "recommendation": {
            "ref": r_rec, "label": rec.get("rec_label"), "dimension": rec.get("dimension"),
            "direction": rec.get("direction"), "predicted_score_increase": rec.get("predicted_score_increase"),
            "rank_on_block": rec.get("rec_rank"),
        },
        "block": {
            "ref": r_block, "street": block.get("street_name"),
            "between": [block.get("intersection_a"), block.get("intersection_b")],
            "neighborhood": block.get("neighborhood"),
            "sop_index_0_to_100": block.get("sop_index_norm"),
            "dimension_scores": {k: block.get(k) for k in (
                "form_score", "density_score", "proximity_score", "connectivity_score", "parks_score",
                "pedestrian_score", "safety_score", "traffic_score", "aesthetics_score")},
            "tract": {"geoid": block.get("tract_geoid"), "population": block.get("total_pop"),
                      "median_household_income": block.get("median_hh_income"),
                      "median_home_value": block.get("median_home_value"),
                      "commuters": block.get("commuters_total"), "transit_commuters": block.get("commuters_transit"),
                      "renter_households_without_a_vehicle": block.get("renter_no_vehicle")},
        },
        "plan": {
            "ref": r_plan, "playbook": pathway.get("name"), "who_drives_it": pathway.get("who"),
            "estimate_weeks": {"fastest": est.get("fastest_weeks"), "slowest": est.get("slowest_weeks"),
                               "untimed_steps": est.get("untimed")},
            "steps": [{"n": i, "title": s.get("title"), "how": s.get("how"), "actor": s.get("actor"),
                       "owner": s.get("owner"), "time": s.get("time"), "only_if": s.get("condition") or None,
                       "after_steps": [next((j for j, t in enumerate(steps, 1) if t["id"] == d), d)
                                       for d in s.get("depends_on") or []]}
                      for i, s in enumerate(steps, 1)],
        },
        "zoning": {
            "ref": r_zone, "summary": finding.get("summary"), "outcome": finding.get("best"),
            "zones": [z.get("zone") for z in finding.get("zones") or []],
            "sme_decision": review.get("action"), "sme_note": review.get("note"),
            "open_questions": [q.get("text") for q in finding.get("questions") or []],
            "narrative": [p.get("text") for p in finding.get("narrative") or []][:6],
        },
        "funding": {
            "approved_sources": funding_facts,
            "total_the_recommendation_would_ask": round(total, 2),
            "note": "Approved means the SME kept the match. It is not an award." if fits else
                    "No funding source was approved for this recommendation.",
        },
    }
    if guidance:
        facts["sme_input"] = [
            {"ref": refs.add(f"SME input from {g.get('by') or 'the SME'}", detail=f"{(g.get('at') or '')[:10]}: {g['text']}"),
             "from": g.get("by"), "date": (g.get("at") or "")[:10], "text": g["text"]}
            for g in guidance
        ]
    return {"facts": facts, "references": refs.items, "funding_total": round(total, 2)}


# ── The judgement ────────────────────────────────────────────────────────────

_REFS = {"type": "array", "items": {"type": "string"}}

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["headline", "score", "cost_estimate", "funding_coverage", "factors", "rationale", "risks",
                 "raise_score", "sme_questions"],
    "properties": {
        "headline": {"type": "string"},
        "score": {"type": "integer"},
        "cost_estimate": {"type": "object", "additionalProperties": False,
                          "required": ["low", "high", "basis", "confidence"],
                          "properties": {"low": {"type": "number"}, "high": {"type": "number"},
                                         "basis": {"type": "string"},
                                         "confidence": {"type": "string", "enum": ["low", "medium", "high"]}}},
        "funding_coverage": {"type": "object", "additionalProperties": False, "required": ["summary", "refs"],
                             "properties": {"summary": {"type": "string"}, "refs": _REFS}},
        "factors": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["key", "rating", "note", "refs"],
            "properties": {"key": {"type": "string", "enum": list(FACTORS)},
                           "rating": {"type": "string", "enum": RATINGS},
                           "note": {"type": "string"}, "refs": _REFS}}},
        "rationale": {"type": "string"},
        "risks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "refs"],
            "properties": {"text": {"type": "string"}, "refs": _REFS}}},
        "raise_score": {"type": "array", "items": {"type": "string"}},
        "sme_questions": {"type": "array", "items": {"type": "string"}},
        "what_changed": {"type": "string"},
    },
}
SCHEMA["required"].append("what_changed")

PROMPT = """You are judging whether one recommended street improvement on one block in Bowie, Maryland (Prince George's County) is likely to get done. City planners and a subject-matter expert (SME) will review your judgement.

The recommendation comes from the State of Place model, which predicts how much it would raise the block's walkability score. The SME has already reviewed everything below: the implementation plan (the steps, who acts, how long), the funding sources matched to this recommendation with the amount it would ask each for, and the zoning finding. Your job is the judgement they leave open: given the money, the zoning path, the time and the parties involved, how good a candidate is this?

The evidence. Each part names the reference (R1, R2 ...) it comes from:
<evidence>
{evidence}
</evidence>

The references:
<references>
{references}
</references>

Work through it in this order.

1. Cost. Estimate what this recommendation would cost on this block, as a low-high range in US dollars, from typical costs for this kind of work in the Washington, DC region (per linear foot of sidewalk, per crossing, per facade, per acre of park, and so on). Size it to the block as the evidence describes it, and say in `basis` what you assumed (quantity and unit cost) so the SME can correct it. This is your estimate, not a sourced figure, so cite nothing for it. For owner-driven work (a building, a development), estimate the cost of the work itself, whoever pays.

2. Funding coverage. Compare the approved amounts with your cost range. An approved match is not an award: these programs are competitive, so weigh fit and program type, and do not count a deadline that has passed. Some programs cannot be combined. Where the plan relies on a property owner or developer paying, say so: public grants rarely pay for private work, and the owner's own decision matters more.

3. The rest. Zoning: by right is no obstacle; a special exception adds a hearing, months and some risk; prohibited is close to blocking unless a rezoning is realistic; a finding left open is uncertainty. Timeline: the plan's estimate, and whether the funding deadlines fit it. Outside parties: how many (owners, the Maryland State Highway Administration, utilities, the county) must act. Need and impact: the predicted gain and the block's context.

4. Score from 0 to 100: how likely this recommendation is to get done on this block within the plan's slowest estimate, given all of the above. As a rough guide: 70 or more, the funding plausibly covers most of the cost and nothing blocks it; 50-69, workable but with a real gap or hurdle; 30-49, a major gap or hurdle; under 30, unlikely unless something changes. Funding adequacy weighs most.

Give one factor for each of: funding, zoning, timeline, dependencies, need.
{rework}
Rules:
- Cite the references behind a claim as [R3] in the prose fields, and list them in that item's `refs`. Use only ids from the references list. Cite nothing for your own estimates or general knowledge.
- Do not invent programs, amounts, dates, ordinances or facts about the block. If something you need is not in the evidence, say it is unknown and, if it matters, ask it as an SME question.
- predicted_score_increase is the model's predicted gain. Do not say whether it is on the overall index or on one dimension.
- Write plainly and briefly. headline: one sentence. rationale: 3-5 sentences. Each factor note: one sentence. risks: at most 3. raise_score: at most 3 concrete changes. sme_questions: at most 3, only what the evidence cannot settle, naming specifics."""

SME_INPUT = """
The SME's input. The SME has told you what they know (`sme_input` in the evidence, each with its reference). Treat it as the most reliable information about this block and this project: where it changes the scope, the cost, who owns or pays, the funding or the timing, follow it, and cite it like any other reference. Do not reopen what it settles with an SME question. If it conflicts with a sourced fact, say so plainly rather than choosing silently.
"""

PREVIOUS = """
This is a rework. Your earlier assessment of this recommendation, which the SME sent back with their input:
<previous_assessment>
{previous}
</previous_assessment>
Rework it in the light of the SME's input: keep what still holds, change what the input changes. In `what_changed`, say in one or two sentences what changed from the earlier assessment and why (score, cost, factors), citing the SME's input.
"""

NO_PREVIOUS = """
In `what_changed`, give an empty string: this is a first assessment.
"""

_CITE = re.compile(r"\[(R\d+(?:\s*,\s*R\d+)*)\]")


def _clean_text(text: str, known: set[str]) -> str:
    """Drop cited ids the evidence never had, so a reference shown always exists."""
    def keep(m: re.Match) -> str:
        ids = [i.strip() for i in m.group(1).split(",") if i.strip() in known]
        return f"[{', '.join(ids)}]" if ids else ""
    return _CITE.sub(keep, text or "").strip()


def _clean_refs(refs: list, known: set[str]) -> list[str]:
    return [r for r in dict.fromkeys(refs or []) if r in known]


def _previous_for_prompt(prev: dict) -> dict:
    """The parts of an earlier assessment the rework needs: what it concluded, and how the SME decided on it."""
    p = prev.get("proposed") or {}
    return {
        "score": prev.get("proposed_score"), "headline": p.get("headline"), "cost_estimate": p.get("cost_estimate"),
        "funding_coverage": (p.get("funding_coverage") or {}).get("summary"),
        "factors": [{k: f.get(k) for k in ("key", "rating", "note")} for f in p.get("factors") or []],
        "rationale": p.get("rationale"), "sme_questions": p.get("sme_questions"),
        "sme_decision": prev.get("decision"),
    }


def judge(evidence: dict, previous: dict | None = None) -> dict:
    """
    Ask the model for its assessment, then tidy it: clamp the score, order the factors, keep only real references.
    previous: the assessment being reworked, if this is a rework.
    """
    rework = (SME_INPUT if evidence["facts"].get("sme_input") else "") + (
        PREVIOUS.format(previous=json.dumps(_previous_for_prompt(previous), indent=1, default=str)) if previous
        else NO_PREVIOUS)
    # replace, not format: the rework text holds JSON braces.
    prompt = PROMPT.format(
        evidence=json.dumps(evidence["facts"], indent=1, default=str),
        references=json.dumps(evidence["references"], indent=1),
        rework="{rework}").replace("{rework}", rework)
    out = structured_call(prompt, SCHEMA, max_tokens=16000, model=MODEL, effort="high")
    known = {r["id"] for r in evidence["references"]}

    score = max(0, min(100, int(round(float(out.get("score") or 0)))))
    cost = out.get("cost_estimate") or {}
    lo, hi = sorted([max(0.0, float(cost.get("low") or 0)), max(0.0, float(cost.get("high") or 0))])
    by_key = {}
    for f in out.get("factors") or []:
        if f.get("key") in FACTORS and f["key"] not in by_key:
            by_key[f["key"]] = {"key": f["key"], "label": FACTORS[f["key"]], "rating": f.get("rating"),
                                "note": _clean_text(f.get("note"), known), "refs": _clean_refs(f.get("refs"), known)}
    return {
        "score": score,
        "band": band_for(score),
        "headline": _clean_text(out.get("headline"), known),
        "cost_estimate": {"low": lo, "high": hi, "basis": cost.get("basis"), "confidence": cost.get("confidence"),
                          "source": "LLM estimate"},
        "funding_coverage": {"summary": _clean_text((out.get("funding_coverage") or {}).get("summary"), known),
                             "refs": _clean_refs((out.get("funding_coverage") or {}).get("refs"), known)},
        "coverage": coverage(evidence["funding_total"], lo, hi),
        "factors": [by_key[k] for k in FACTORS if k in by_key],
        "rationale": _clean_text(out.get("rationale"), known),
        "risks": [{"text": _clean_text(r.get("text"), known), "refs": _clean_refs(r.get("refs"), known)}
                  for r in (out.get("risks") or [])[:3]],
        "raise_score": [s for s in (out.get("raise_score") or [])[:3] if s],
        "sme_questions": [q for q in (out.get("sme_questions") or [])[:3] if q],
        "what_changed": _clean_text(out.get("what_changed"), known) if previous else None,
    }


def coverage(total: float, low: float, high: float) -> dict:
    """The approved funding as a share of the cost range. Computed here, not by the model."""
    return {
        "funding_total": total, "cost_low": low, "cost_high": high,
        # Against the high cost, the worst case; against the low cost, the best.
        "share_low": round(total / high, 3) if high else None,
        "share_high": round(total / low, 3) if low else None,
    }


# ── Runs ─────────────────────────────────────────────────────────────────────

def _run(assessment_id: int, block_id: int, rec_id: int, guidance: list[dict], base_id: int | None) -> None:
    """Background: gather the evidence, ask the model, and leave the assessment in_review (or failed)."""
    db = get_supabase()
    repo = FeasibilityRepository(db)
    try:
        ctx = context(db, block_id)
        evidence = build_evidence(db, ctx, ctx["recs"][rec_id], guidance)
        repo.update(assessment_id, {"evidence": evidence})
        proposed = judge(evidence, repo.get(base_id) if base_id else None)
        repo.finish(assessment_id, {"status": "in_review", "proposed": proposed,
                                    "proposed_score": proposed["score"], "band": proposed["band"]})
    except Exception as e:  # noqa: BLE001 — the run records its own failure for the user
        log.exception("feasibility assessment %s failed", assessment_id)
        repo.finish(assessment_id, {"status": "failed", "error": str(e)[:1000]})


def _carried(rows: list[dict]) -> list[dict]:
    """
    The SME input a new run of a recommendation starts with: that of its current assessment, or of a newer one
    awaiting review or failed. Input on an assessment the SME discarded is not carried.
    """
    row = next((a for a in rows if a.get("current") or a["status"] in ("in_review", "failed")), None)
    return list(((row or {}).get("inputs") or {}).get("guidance") or [])


def start(db: Client, block_id: int, rec_ids: list[int] | None = None, created_by: str | None = None,
          guidance: str | None = None, base_id: int | None = None) -> dict:
    """
    Assess the given recommendations (every ready one on the block when omitted). Each run goes in the background;
    one not ready, or already running, is skipped with the reason. Returns {started, skipped}.

    guidance and base_id make it a rework: the SME's input is added to what the base assessment carried, and the
    model sees the base assessment. Without them, a run still carries the recommendation's earlier SME input.
    """
    ctx = context(db, block_id)
    repo = FeasibilityRepository(db)
    wanted = rec_ids if rec_ids else list(ctx["recs"])
    unknown = [r for r in wanted if r not in ctx["recs"]]
    if unknown:
        raise FeasibilityError(f"Recommendation(s) {unknown} are not on block {block_id}.")
    running = {a["rec_id"] for a in repo.for_block(block_id) if a["status"] == "running" and not _lost(a)}

    started, skipped = [], []
    for rec_id in wanted:
        rec = ctx["recs"][rec_id]
        missing = missing_for(ctx, rec)
        if missing:
            if rec_ids:   # asked for by name: say why; a block-wide run skips the unready quietly
                skipped.append({"rec_id": rec_id, "rec_label": rec.get("rec_label"), "missing": missing})
            continue
        if rec_id in running:
            skipped.append({"rec_id": rec_id, "rec_label": rec.get("rec_label"),
                            "missing": [{"key": "running", "text": "An assessment is already running."}]})
            continue
        if base_id:
            notes = list(((repo.get(base_id) or {}).get("inputs") or {}).get("guidance") or [])
        else:
            notes = _carried(repo.for_block(block_id, rec_id, limit=20))
        if guidance:
            notes.append({"text": guidance.strip(), "by": (created_by or "").strip() or None,
                          "at": datetime.now(timezone.utc).isoformat()})
        repo.discard_open(block_id, rec_id)
        row = repo.create({"block_id": block_id, "rec_id": rec_id, "rec_label": rec.get("rec_label"),
                           "status": "running", "model": MODEL,
                           "inputs": {**inputs_for(ctx, rec_id), "guidance": notes, "reworks": base_id},
                           "created_by": (created_by or "").strip() or None})
        _pool.submit(_run, row["id"], block_id, rec_id, notes, base_id)
        started.append({"rec_id": rec_id, "rec_label": rec.get("rec_label"), "assessment_id": row["id"]})
    return {"block_id": block_id, "started": started, "skipped": skipped}


def _lost(a: dict) -> bool:
    try:
        made = datetime.fromisoformat(str(a.get("created_at")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return a.get("status") == "running" and datetime.now(timezone.utc) - made > LOST_AFTER


def _shown(a: dict | None) -> dict | None:
    """A run as the UI shows it: a lost run reads as failed."""
    if a and _lost(a):
        return {**a, "status": "failed", "error": "The run was interrupted (the server restarted). Assess again."}
    return a


def review(db: Client, assessment_id: int, action: str, approved_by: str, score: int | None = None,
           cost_low: float | None = None, cost_high: float | None = None, note: str | None = None) -> dict:
    """
    Approve an assessment awaiting review and make it the recommendation's current one.

    accept: the proposed score stands. set: the SME's score replaces it, and a note must say why. Correcting the
    cost range also needs a note. Refused when the plan, funding or zoning changed since the run read them.
    """
    who = (approved_by or "").strip()
    if not who:
        raise FeasibilityError("Say who is approving: the name is recorded with the score.")
    repo = FeasibilityRepository(db)
    a = repo.get(assessment_id)
    if not a:
        raise FeasibilityError(f"Assessment {assessment_id} not found.")
    if a["status"] != "in_review":
        raise FeasibilityError(f"Assessment #{assessment_id} is {a['status']}; only one awaiting review can be approved.")
    ctx = context(db, a["block_id"])
    changed = changed_inputs(a, inputs_for(ctx, a["rec_id"]))
    if changed:
        raise FeasibilityError(f"{', '.join(changed).capitalize()} changed since this assessment ran. Assess again.")

    text = (note or "").strip() or None
    proposed = a.get("proposed") or {}
    cost = proposed.get("cost_estimate") or {}
    if action == "accept":
        final = a["proposed_score"]
    elif action == "set":
        if score is None or not 0 <= int(score) <= 100:
            raise FeasibilityError("Set a score from 0 to 100.")
        final = int(score)
        if final != a["proposed_score"] and not text:
            raise FeasibilityError(f"Say why you set {final} where the assessment proposed {a['proposed_score']}.")
    else:
        raise FeasibilityError("action must be accept or set.")
    new_cost = None
    if cost_low is not None or cost_high is not None:
        lo, hi = sorted([float(cost_low if cost_low is not None else cost.get("low") or 0),
                         float(cost_high if cost_high is not None else cost.get("high") or 0)])
        if (lo, hi) != (cost.get("low"), cost.get("high")):
            if not text:
                raise FeasibilityError("Say where the corrected cost comes from: a note is required.")
            new_cost = {"low": lo, "high": hi}

    decision = {"action": "set" if final != a["proposed_score"] else "accept", "score": final,
                "proposed_score": a["proposed_score"], "cost": new_cost, "note": text, "by": who,
                "at": datetime.now(timezone.utc).isoformat()}
    if new_cost:
        total = (proposed.get("coverage") or {}).get("funding_total") or 0
        decision["coverage"] = coverage(total, new_cost["low"], new_cost["high"])
    repo.make_current(a, {"score": final, "band": band_for(final), "decision": decision, "approved_by": who})
    return repo.get(assessment_id)


def rework(db: Client, assessment_id: int, guidance: str, made_by: str) -> dict:
    """
    Send an assessment back with the SME's input: a new run reads it (and this assessment) and reworks the judgement.
    The base may be awaiting review (it is replaced) or the current one (it stays current until the rework is approved).
    """
    text, who = (guidance or "").strip(), (made_by or "").strip()
    if not text:
        raise FeasibilityError("Say what the agent should know or reconsider.")
    if not who:
        raise FeasibilityError("Say who you are: your input is recorded with your name and cited in the assessment.")
    a = FeasibilityRepository(db).get(assessment_id)
    if not a:
        raise FeasibilityError(f"Assessment {assessment_id} not found.")
    if not (a["status"] == "in_review" or a.get("current")):
        raise FeasibilityError(f"Assessment #{assessment_id} is {a['status']}; rework one awaiting review or the current one.")
    out = start(db, a["block_id"], [a["rec_id"]], who, guidance=text, base_id=assessment_id)
    if not out["started"]:
        raise FeasibilityError(" ".join(m["text"] for s in out["skipped"] for m in s["missing"]) or "Could not start the rework.")
    return out


def discard(db: Client, assessment_id: int) -> dict:
    repo = FeasibilityRepository(db)
    a = repo.get(assessment_id)
    if not a:
        raise FeasibilityError(f"Assessment {assessment_id} not found.")
    if a["status"] == "approved":
        raise FeasibilityError("An approved assessment is kept as history; assess again to replace it.")
    repo.update(assessment_id, {"status": "discarded"})
    return repo.get(assessment_id)


# ── Reads ────────────────────────────────────────────────────────────────────

def for_rec(db: Client, block_id: int, rec_id: int) -> dict:
    """
    One recommendation's feasibility: what it is missing, its current (approved) assessment, its latest
    open one (running, in review or failed), what changed since the current one, and the evidence behind each.
    """
    ctx = context(db, block_id)
    rec = ctx["recs"].get(rec_id)
    if not rec:
        raise FeasibilityError(f"Recommendation {rec_id} is not on block {block_id}.")
    repo = FeasibilityRepository(db)
    rows = [_shown(a) for a in repo.for_block(block_id, rec_id, limit=20)]
    current = next((a for a in rows if a.get("current")), None)
    open_ = next((a for a in rows if a["status"] in ("running", "in_review", "failed")), None)
    if open_ and current and open_["created_at"] < current["created_at"]:
        open_ = None
    now = inputs_for(ctx, rec_id)
    full = lambda a: {**a, **{k: v for k, v in (repo.get(a["id"]) or {}).items() if k == "evidence"}} if a else None
    return {
        "block_id": block_id, "rec_id": rec_id, "rec_label": rec.get("rec_label"),
        "missing": missing_for(ctx, rec), "inputs": now,
        "current": full(current),
        "stale": changed_inputs(current, now) if current else [],
        "open": full(open_) if open_ and open_["status"] != "running" else open_,
        "open_stale": changed_inputs(open_, now) if open_ and open_["status"] == "in_review" else [],
        "history": [{k: a.get(k) for k in ("id", "status", "score", "proposed_score", "approved_by", "created_at",
                                            "approved_at", "current")} for a in rows],
        "bands": [{"min": floor, "key": key, "text": text} for floor, key, text in BANDS],
    }


def summary_for_block(db: Client, ctx: dict) -> dict[int, dict]:
    """rec_id -> {score, band, status, stale, ready} for the block's recommendation list."""
    try:
        rows = FeasibilityRepository(db).for_block(ctx["block"]["id"])
    except Exception:  # noqa: BLE001 — the table may not exist yet; the list still loads
        return {}
    out: dict[int, dict] = {}
    for rec_id, rec in ctx["recs"].items():
        mine = [_shown(a) for a in rows if a["rec_id"] == rec_id]
        current = next((a for a in mine if a.get("current")), None)
        latest = mine[0] if mine else None
        out[rec_id] = {
            "score": current.get("score") if current else None,
            "band": current.get("band") if current else None,
            # running / in_review / failed when a newer run is open, else approved or None.
            "status": latest["status"] if latest and latest["status"] != "discarded" else
                      ("approved" if current else None),
            "stale": bool(current and changed_inputs(current, inputs_for(ctx, rec_id))),
            "ready": not missing_for(ctx, rec),
        }
    return out


def profile_stale(db: Client, profile: dict | None) -> list[str]:
    """
    Which inputs of the assessment behind a gold profile's feasibility_score have
    changed since it ran, by name; [] when none, or the score is the formula.
    Worked out on read, since a funding or zoning run approved after the gold was
    computed makes it stale without touching the profile row.
    """
    aid = (profile or {}).get("feasibility_assessment_id")
    if not aid or profile.get("feasibility_basis") != "assessed":
        return []
    try:
        a = FeasibilityRepository(db).get(aid)
        if not a:
            return []
        return changed_inputs(a, inputs_for(context(db, profile["block_id"]), a["rec_id"]))
    except Exception:  # noqa: BLE001 — the profile still loads without the flag
        log.warning("Could not check whether block %s's feasibility is stale", profile.get("block_id"), exc_info=True)
        return []
