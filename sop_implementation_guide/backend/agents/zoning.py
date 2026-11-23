"""
ZoningAgent — is each recommendation allowed on its block, and what does that
mean for the plan.

Unlike FundingAgent there is no model and no LangGraph graph here, on purpose.
Whether a use is allowed in a zone is written in the Zoning Ordinance, and a
parcel's zone is on the county map, so both are looked up and cited rather
than inferred (see backend/tools/zoning.py). The agent reads the zones that
scripts/sync_block_zoning.py stored, applies the use tables, and returns a
finding per recommendation plus the block's zoning sub-score.

Review works like Block Funding. A Block Zoning analysis (analyze()) runs
against one Zoning dataset version and stores one finding per distinct
recommendation on the block (label and direction) in its result. A finding
with nothing to ask comes pre-accepted at its proposed outcome; one with
questions (an unread recommendation type, a low-confidence reading, a legacy
zone, an overlay, a block not looked up) waits for the SME, who accepts it,
sets the outcome with a note, or leaves it open. approve() completes the run
and pins it. Plans, the block's list and the gold score read only the pinned,
approved run: until a block has one, its zoning is "not reviewed".

The findings are a snapshot taken when the run was made, so an approved
answer never changes under the SME. A newer Zoning version reaches a block
only by running and approving again.
"""

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from supabase import Client

from backend.agents.base import SpecialistAgent
from backend.config import settings
from backend.db.session import get_supabase
from backend.repositories.datasets import BlockAnalysisRepository, DatasetTablesMissing, DatasetVersionRepository
from backend.repositories.recommendations import BlockRepository
from backend.repositories.zoning import ZoningRepository
from backend.tools import datasets, zoning

REVIEW_NOTE = "Zoning findings are reviewed and approved for the whole block in its Block Zoning review."

# What a finding means for the plan once the SME has set its outcome.
SET_MEANING = {
    "by_right": "Per the SME's review, zoning allows this by right, so no zoning hearing is needed.",
    "special_exception": "Per the SME's review, this needs a special exception: its own application and approval "
                         "under §27-3604, so plan for a hearing.",
    "prohibited": "Per the SME's review, zoning prohibits this here. Only a rezoning or a text amendment to the "
                  "ordinance would change that.",
    zoning.NOT_APPLICABLE: "Per the SME's review, zoning does not decide this; whoever owns the street or site does.",
}


class ReviewError(ValueError):
    """A review that cannot be applied: the run is not awaiting review, or a decision is malformed."""


def _checked(label: str | None) -> bool:
    """
    Whether zoning has a say in a recommendation type: every type except one read as not a land use.
    A type with no reading yet is checked. It is an open question, never "zoning does not apply".
    """
    r = zoning.reading_for(label)
    return not (r and r.get("governed_by") == zoning.NOT_A_LAND_USE)


def _proposed(label: str, finding: dict) -> str | None:
    """The outcome the analysis proposes: the best zone's, not_applicable for a non-land use, None if unresolved."""
    if not _checked(label):
        return zoning.NOT_APPLICABLE
    return finding.get("best")


def review_items(recs: list[dict], rows: list[dict], block: dict) -> list[dict]:
    """
    One finding per distinct recommendation on the block, for the SME to review.

    Recommendations are grouped on (label, direction): the same improvement listed under two dimensions gets
    one zoning answer, and the decision on it applies to every rec_id in the group.
    """
    groups: dict[tuple, dict] = {}
    for r in recs:
        label = (r.get("rec_label") or "").strip()
        if not label or r.get("id") is None:
            continue
        direction = r.get("direction") or "Increase"
        g = groups.get((label, direction))
        if g is None:
            g = groups[(label, direction)] = {
                "key": f"{label}|{direction}", "rec_label": label, "direction": direction,
                "dimension": r.get("dimension"), "rec_ids": [], "gain": None, "_rec": r,
            }
        g["rec_ids"].append(r["id"])
        gain = r.get("predicted_score_increase")
        if gain is not None and (g["gain"] is None or gain > g["gain"]):
            g["gain"] = gain

    items = []
    for g in groups.values():
        rec = g.pop("_rec")
        f = zoning.assess({**rec, "block_id": block["id"]}, rows, block)
        proposed = _proposed(g["rec_label"], f)
        flagged = f["status"] != "attached" or proposed is None
        items.append({
            **g, "finding": f, "proposed": proposed, "flagged": flagged,
            "decision": None if flagged else {"action": "accept", "outcome": proposed, "auto": True},
        })
    items.sort(key=lambda i: -(i["gain"] if i["gain"] is not None else float("-inf")))
    return items


def _counts(items: list[dict]) -> dict:
    """Counts for list views. The run list reads only result.summary, so these live there."""
    decisions = [i.get("decision") or {} for i in items]
    return {
        "findings": len(items),
        "flagged": sum(i["flagged"] for i in items),
        "decided": sum(d.get("action") in ("accept", "set") for d in decisions),
        "open": sum(d.get("action") not in ("accept", "set") for d in decisions),
        "overridden": sum(d.get("action") == "set" for d in decisions),
    }


def _when(iso: str | None) -> str:
    return (iso or "")[:10]


def reviewed_finding(item: dict, analysis: dict) -> dict:
    """
    The finding as the SME approved it, in the shape tools/zoning.assess returns, plus `review`.

    Accepted: the proposed answer stands and its questions count as answered. Set: the SME's outcome replaces
    the proposed one, with the note as the reason. Open: unchanged, questions and all.
    """
    f = copy.deepcopy(item["finding"])
    result = analysis.get("result") or {}
    d = item.get("decision") or {"action": "open"}
    who = d.get("by") or result.get("approved_by") or "the SME"
    on = _when(d.get("at") or result.get("approved_at"))
    f["review"] = {
        "action": d["action"], "outcome": d.get("outcome"), "note": d.get("note"), "auto": bool(d.get("auto")),
        "proposed": item.get("proposed"), "by": who, "at": d.get("at") or result.get("approved_at"),
        "questions": f["questions"], "analysis_id": analysis.get("id"),
        "dataset_version": analysis.get("dataset_version"),
    }
    approved = f"Approved in Block Zoning run #{analysis.get('id')} (Zoning v{analysis.get('dataset_version')}) by {who}" \
               + (f" on {on}" if on else "")

    if d["action"] not in ("accept", "set"):
        f["narrative"].insert(0, {"section": "review", "refs": [],
                                  "text": f"{approved}, with this finding left open: the questions below still need "
                                          "an answer."})
        return f

    outcome = d.get("outcome")
    f["questions"] = []
    f["status"] = "attached"
    note = f" {d['note'].strip()}" if (d.get("note") or "").strip() else ""
    if d["action"] == "set" and outcome != item.get("proposed"):
        was = zoning.SHORT.get(item.get("proposed") or "unknown", "not resolved")
        f["best"] = outcome if outcome in zoning.RANK else None
        f["scorable"] = item.get("direction") != "Decrease" and outcome in zoning.RANK
        f["summary"] = f"SME: {zoning.SHORT[outcome]}"
        f["narrative"] = [p for p in f["narrative"] if p["section"] != "meaning"]
        f["narrative"].insert(0, {"section": "review", "refs": [],
                                  "text": f"{approved}. The SME set this to “{zoning.SHORT[outcome]}”, where the "
                                          f"analysis proposed “{was}”.{note}"})
        f["narrative"].append({"section": "meaning", "refs": [], "text": SET_MEANING[outcome]})
    else:
        how = "with nothing to ask" if d.get("auto") else "as proposed"
        f["narrative"].insert(0, {"section": "review", "refs": [], "text": f"{approved}, {how}.{note}"})
    return f


def not_reviewed(pinned: dict | None, pending: dict | None = None) -> dict:
    """The finding a plan shows when the block has no approved Block Zoning run."""
    if pinned:
        text = (f"Block Zoning run #{pinned['id']} was made before zoning findings were reviewed. Run Block Zoning "
                "again on the block page and approve it to show zoning here.")
    elif pending:
        text = (f"Block Zoning run #{pending['id']} (Zoning v{pending.get('dataset_version')}) is waiting for the "
                "SME's approval. Zoning shows here once it is approved.")
    else:
        text = "This block has no approved zoning review yet. Run Block Zoning on the block page and approve it."
    return {"status": "not_reviewed", "summary": "Zoning not reviewed", "zones": [], "sections": zoning.SECTIONS,
            "narrative": [{"section": "meaning", "text": text, "refs": []}], "questions": [], "references": [],
            "best": None, "scorable": False}


def approved_items(analysis: dict | None) -> list[dict] | None:
    """The reviewed findings of an approved run; None for no run, one not approved, or one made before reviews."""
    if not analysis or analysis.get("status") != "complete":
        return None
    items = (analysis.get("result") or {}).get("findings")
    return items if isinstance(items, list) else None


def best_by_rec(items: list[dict] | None) -> dict[int, str | None]:
    """rec_id -> its approved outcome if it is by_right / special_exception / prohibited, for the block's list."""
    out: dict[int, str | None] = {}
    for i in items or []:
        d = i.get("decision") or {}
        best = d.get("outcome") if d.get("action") in ("accept", "set") and d.get("outcome") in zoning.RANK else None
        for rid in i.get("rec_ids") or []:
            out[rid] = best
    return out


def score_items(items: list[dict]) -> tuple[float | None, dict]:
    """
    Zoning sub-score 0-100 from approved findings, or None if nothing can be scored.

    Each "Increase" recommendation settled by right, special exception or prohibited scores those points
    (config.py), counted once per recommendation as before. Open findings are "not checked", never scored as
    prohibited; reductions and "zoning does not decide it" are left out.
    """
    points = {
        "by_right": settings.zoning_points_by_right,
        "special_exception": settings.zoning_points_special_exception,
        "prohibited": settings.zoning_points_prohibited,
    }
    scores, not_checked = [], 0
    for i in items:
        if i.get("direction") == "Decrease":
            continue
        d = i.get("decision") or {}
        n = len(i.get("rec_ids") or []) or 1
        if d.get("action") in ("accept", "set") and d.get("outcome") in points:
            scores += [points[d["outcome"]]] * n
        elif d.get("outcome") != zoning.NOT_APPLICABLE:
            not_checked += n
    detail = {"reviewed": True, "scored": len(scores), "unscorable": not_checked, "not_checked": not_checked}
    return (round(sum(scores) / len(scores), 1) if scores else None), detail


def analysis_label(a: dict | None) -> dict | None:
    """The short form of an analysis that travels with block responses."""
    if not a:
        return None
    return {"id": a["id"], "kind": a.get("kind"), "name": a.get("name"), "status": a.get("status"),
            "dataset_version": a.get("dataset_version"), "created_at": a.get("created_at"),
            "created_by": a.get("created_by")}


def _statuses(recs: list[dict], rows: list[dict], block: dict) -> dict[str, int]:
    """How many of the block's recommendations zoning has a say in land in each finding status."""
    out: dict[str, int] = {}
    for rec in recs:
        if _checked(rec.get("rec_label")):
            st = zoning.assess({**rec, "block_id": block["id"]}, rows, block)["status"]
            out[st] = out.get(st, 0) + 1
    return out


class ZoningAgent(SpecialistAgent):
    def build_graph(self):
        # Deterministic: a lookup plus a table, so there is no graph to build.
        return None

    @contextmanager
    def scope(self, block_id: int, db: Client) -> Iterator[tuple[list[dict], dict]]:
        """
        The zoning data this block is tied to, made current for the block of code.

        Yields (the block's zone rows, tie). tie names the pinned analysis and
        the dataset version in use, and its basis: 'pinned', 'latest' (never
        analysed, so the newest version applies) or 'in_force' (no versions
        exist yet, so the working data applies, as before versioning).
        """
        try:
            analysis = BlockAnalysisRepository(db).pinned(block_id, "zoning")
            version = (DatasetVersionRepository(db).get_by_id(analysis["dataset_version_id"])
                       if analysis and analysis.get("dataset_version_id") else datasets.resolve(db, "zoning"))
        except DatasetTablesMissing:
            analysis, version = None, None
        with datasets.zoning_scope(db, version) as bundle:
            rows = bundle["rows"].get(block_id, []) if bundle else ZoningRepository(db).get_for_block(block_id)
            tie = {
                "analysis": analysis_label(analysis),
                "dataset_version": datasets.label(version),
                "basis": "pinned" if analysis else ("latest" if version else "in_force"),
            }
            yield rows, tie

    def assess_block(self, block_id: int, db: Client | None = None) -> dict:
        db = db or get_supabase()
        block = BlockRepository(db).get_with_recs(block_id)
        if not block:
            return {"status": "not_found", "agent": "ZoningAgent", "block_id": block_id}
        with self.scope(block_id, db) as (rows, tie):
            lookup = zoning.block_lookup(rows)
            recs = block.get("recommendations", [])
            score, detail = zoning.score_block(recs, rows)
            findings = [
                {"rec_id": r.get("id"), "rec_label": r.get("rec_label"), "direction": r.get("direction"),
                 **zoning.assess(r, rows, block)}
                for r in recs if _checked(r.get("rec_label"))
            ]
        return {
            "status": "ok" if lookup else "not_looked_up",
            "agent": "ZoningAgent",
            "block_id": block_id,
            "zones": lookup["zones"] if lookup else [],
            "lookup": lookup,
            "zoning_score": score,
            "score_detail": detail,
            "findings": findings,
            **tie,
        }

    def analyze(self, db: Client, block_ids: list[int] | None = None, version: int | None = None,
                name: str | None = None, created_by: str | None = None) -> dict:
        """
        Run Block Zoning against one Zoning dataset version, for the SME to review.

        block_ids None means every block; version None means the latest. Each
        block gets an analysis row awaiting review (in_review) holding its
        findings (review_items). Nothing reads it until approve(). A block's
        earlier run still awaiting review is discarded: the new one replaces
        it, and since a run is a lookup, nothing is lost. Raises ValueError for
        an unknown version or block.
        """
        v = datasets.resolve(db, "zoning", version)
        if v is None:
            raise ValueError(f"Zoning version {version} does not exist." if version is not None
                             else "There is no Zoning dataset version yet: run scripts/seed_dataset_versions.py.")
        query = db.table("blocks").select("id, street_name").order("id")
        if block_ids:
            query = query.in_("id", block_ids)
        blocks = query.execute().data or []
        if block_ids and len(blocks) != len(set(block_ids)):
            raise ValueError(f"Unknown block(s): {sorted(set(block_ids) - {b['id'] for b in blocks})}")

        recs_by_block: dict[int, list[dict]] = {}
        for r in BlockRepository(db).all_recommendations(
                "id, block_id, rec_label, direction, rec_rank, dimension, predicted_score_increase"):
            recs_by_block.setdefault(r["block_id"], []).append(r)

        new_rows = []
        with datasets.zoning_scope(db, v) as bundle:
            rules_version = zoning.load_rules().get("version")
            amended = zoning.load_use_table()["source"].get("amended_through")
            for b in blocks:
                rows = bundle["rows"].get(b["id"], []) if bundle else ZoningRepository(db).get_for_block(b["id"])
                lookup = zoning.block_lookup(rows)
                items = review_items(recs_by_block.get(b["id"], []), rows, b)
                new_rows.append({
                    "block_id": b["id"], "kind": "zoning",
                    "dataset_version_id": v["id"], "dataset_version": v["version"],
                    "name": (name or "").strip() or None,
                    "status": "in_review", "pinned": False,
                    "created_by": (created_by or "").strip() or None,
                    "result": {
                        "summary": {**_counts(items), "zones": (lookup or {}).get("zones", []),
                                    "looked_up": lookup is not None},
                        "findings": items,
                        "zones": (lookup or {}).get("zones", []),
                        "rules_version": rules_version,
                        "use_table_amended_through": amended,
                    },
                })

        repo = BlockAnalysisRepository(db)
        repo.discard_in_review([b["id"] for b in blocks], "zoning")
        created = repo.create_many(new_rows)
        return {
            "kind": "zoning",
            "dataset_version": datasets.label(v),
            "status": "in_review",
            "analyses": [{"block_id": a["block_id"], "analysis_id": a["id"],
                          **((a.get("result") or {}).get("summary") or {})} for a in created],
        }

    def approve(self, db: Client, analysis_id: int, decisions: list[dict], approved_by: str,
                note: str | None = None) -> dict:
        """
        Apply the SME's decisions to a run awaiting review, complete it and pin it to the block.

        decisions: [{key, action: accept | set | open, outcome, note}]. accept keeps the proposed outcome (there
        must be one); set needs one of REVIEW_OUTCOMES and a note saying why; open leaves the questions standing.
        A flagged finding with no decision is left open. A pre-accepted one with no decision stays accepted.
        Raises ReviewError.
        """
        who = (approved_by or "").strip()
        if not who:
            raise ReviewError("Say who is approving: the approval is recorded with every finding.")
        repo = BlockAnalysisRepository(db)
        a = repo.get(analysis_id)
        if not a or a.get("kind") != "zoning":
            raise ReviewError(f"Zoning analysis {analysis_id} not found.")
        if a["status"] != "in_review":
            raise ReviewError(f"Run #{analysis_id} is {a['status']}; only a run awaiting review can be approved.")
        result = dict(a.get("result") or {})
        items = result.get("findings")
        if not isinstance(items, list):
            raise ReviewError(f"Run #{analysis_id} holds no findings to review. Run Block Zoning again.")

        by_key = {i["key"]: i for i in items}
        now = datetime.now(timezone.utc).isoformat()
        given: dict[str, dict] = {}
        for d in decisions or []:
            key = d.get("key")
            if key not in by_key:
                raise ReviewError(f"No finding {key!r} in run #{analysis_id}.")
            action = d.get("action")
            item = by_key[key]
            text = (d.get("note") or "").strip() or None
            if action == "accept":
                if item["proposed"] is None:
                    raise ReviewError(f"“{item['rec_label']}” has no proposed outcome to accept: set one, or leave it open.")
                outcome = item["proposed"]
            elif action == "set":
                outcome = d.get("outcome")
                if outcome not in zoning.REVIEW_OUTCOMES:
                    raise ReviewError(f"“{item['rec_label']}”: the outcome must be one of {', '.join(zoning.REVIEW_OUTCOMES)}.")
                if outcome != item["proposed"] and not text:
                    raise ReviewError(f"“{item['rec_label']}”: say why you set it to {zoning.SHORT[outcome]} (a note is "
                                      "required when the outcome differs from the proposed one).")
                if outcome == item["proposed"]:
                    action = "accept"
            elif action == "open":
                outcome = None
            else:
                raise ReviewError(f"“{item['rec_label']}”: action must be accept, set or open.")
            given[key] = {"action": action, "outcome": outcome, "note": text, "by": who, "at": now, "auto": False}

        for i in items:
            if i["key"] in given:
                i["decision"] = given[i["key"]]
            elif i.get("decision"):
                i["decision"] = {**i["decision"], "by": who, "at": now}
            else:
                i["decision"] = {"action": "open", "outcome": None, "note": None, "by": who, "at": now, "auto": False}

        summary = {**(result.get("summary") or {}), **_counts(items)}
        score, detail = score_items(items)
        summary.update(zoning_score=score, not_checked=detail["not_checked"])
        result.update(findings=items, summary=summary, approved_by=who, approved_at=now,
                      note=(note or "").strip() or None)
        repo.complete(analysis_id, result)
        repo.pin({"id": analysis_id, "block_id": a["block_id"], "kind": "zoning"})
        return {"analysis_id": analysis_id, "block_id": a["block_id"], "status": "complete", "pinned": True,
                **summary}

    def approved(self, block_id: int, db: Client) -> tuple[dict | None, list[dict] | None]:
        """(the pinned zoning run, its approved findings or None). (None, None) before the dataset tables exist."""
        try:
            a = BlockAnalysisRepository(db).pinned(block_id, "zoning")
        except DatasetTablesMissing:
            return None, None
        return a, approved_items(a)

    def finding_for(self, block_id: int, rec_id: int, db: Client) -> tuple[dict, dict]:
        """
        (the approved zoning finding for one recommendation, the tie to its run) for plans.

        Without an approved run the finding is not_reviewed(), naming a run that awaits review if there is one.
        """
        a, items = self.approved(block_id, db)
        tie = {"analysis": analysis_label(a) if items is not None else None,
               "dataset_version": a.get("dataset_version") if items is not None else None,
               "basis": "approved" if items is not None else "not_reviewed"}
        if items is None:
            return not_reviewed(a, self.pending(block_id, db) if not a else None), tie
        item = next((i for i in items if rec_id in (i.get("rec_ids") or [])), None)
        if item is None:
            # A recommendation added to the block after the run was made.
            return {**not_reviewed(None), "narrative": [{
                "section": "meaning", "refs": [],
                "text": f"This recommendation was not on the block when Block Zoning run #{a['id']} was made. "
                        "Run Block Zoning again and approve it to cover it."}]}, tie
        return reviewed_finding(item, a), tie

    @staticmethod
    def pending(block_id: int, db: Client) -> dict | None:
        """The block's newest zoning run awaiting review, if any."""
        try:
            runs = BlockAnalysisRepository(db).list_for_block(block_id, "zoning", light=True)
        except DatasetTablesMissing:
            return None
        return next((r for r in runs if r["status"] == "in_review"), None)

    def overview(self, db: Client | None = None) -> dict:
        """
        Everything behind the zoning findings, for the Zoning tab.

        Per block: zones, prior zones, case numbers, overlays, when it was looked
        up, its score and how many findings still need the SME. Per
        recommendation type: the ordinance uses it is read as, and the outcome in
        every zone found on our blocks. Plus every source with its date. Three
        queries in all; the rest is computed here from the stored rows.
        """
        db = db or get_supabase()
        data_version = zoning.refresh_active(db)
        rules = zoning.load_rules()
        use_src = zoning.load_use_table()["source"]
        block_repo = BlockRepository(db)

        streets = {
            b["id"]: b.get("street_name")
            for b in (db.table("blocks").select("id, street_name").order("id").execute().data or [])
        }
        rows_by_block: dict[int, list[dict]] = {}
        for r in ZoningRepository(db).get_all():
            rows_by_block.setdefault(r["block_id"], []).append(r)
        recs_by_block: dict[int, list[dict]] = {}
        for r in block_repo.all_recommendations():
            recs_by_block.setdefault(r["block_id"], []).append(r)

        blocks, zone_blocks = [], {}
        statuses_total: dict[str, int] = {}
        for block_id, street in streets.items():
            rows = rows_by_block.get(block_id, [])
            lookup = zoning.block_lookup(rows)
            recs = recs_by_block.get(block_id, [])
            score, detail = zoning.score_block(recs, rows)
            statuses: dict[str, int] = {}
            block = {"id": block_id, "street_name": street}
            for rec in recs:
                if _checked(rec.get("rec_label")):
                    st = zoning.assess(rec, rows, block)["status"]
                    statuses[st] = statuses.get(st, 0) + 1
                    statuses_total[st] = statuses_total.get(st, 0) + 1
            for code in (lookup or {}).get("zones", []):
                zone_blocks.setdefault(code, []).append(block_id)
            cases = (lookup or {}).get("cases") or {}
            blocks.append({
                "id": block_id,
                "street_name": street,
                "looked_up": lookup is not None,
                "zones": (lookup or {}).get("zones", []),
                "prior_zones": (lookup or {}).get("prior_zones", []),
                "cases": cases,
                "case_count": sum(len(v) for v in cases.values()),
                "overlays": (lookup or {}).get("overlays", []),
                "fetched_at": (lookup or {}).get("fetched_at"),
                "zoning_score": score,
                "scored": detail["scored"],
                "unscorable": detail["unscorable"],
                "statuses": statuses,
            })

        # Zones in the order they front the most blocks.
        present = sorted(zone_blocks, key=lambda c: (-len(zone_blocks[c]), c))
        zones = []
        for code in present:
            zinfo = rules["zones"].get(code, {})
            zones.append({
                "code": code,
                "name": zinfo.get("name"),
                "legacy": bool(zinfo.get("legacy")),
                "blocks": zone_blocks[code],
                "context": [
                    {"text": c["text"], "ref": zoning.source_ref(c["ref"]["source"], c["ref"].get("page"),
                                                                 c["ref"].get("detail"))}
                    for c in zinfo.get("context", [])
                ],
            })

        rec_counts: dict[str, int] = {}
        for recs in recs_by_block.values():
            for r in recs:
                rec_counts[r.get("rec_label")] = rec_counts.get(r.get("rec_label"), 0) + 1
        matrix_rows = []
        for label, mapping in rules["rec_uses"].items():
            if mapping.get("governed_by") == zoning.NOT_A_LAND_USE:
                continue   # not a land use: no row in the use-table matrix
            cells = {}
            for code in present:
                f = zoning.zone_finding(code, mapping)
                cells[code] = {"outcome": f["outcome"], "uses": f["uses"]}
            matrix_rows.append({
                "rec_label": label,
                "mode": mapping["mode"],
                "confidence": mapping.get("confidence"),
                "note": mapping.get("note"),
                "uses": mapping["uses"],
                "rec_count": rec_counts.get(label, 0),
                "cells": cells,
            })

        sources = [
            {**zoning.source_ref(k), "key": k} for k in rules["sources"]
        ] + [{
            "key": "use_table",
            "label": use_src["name"],
            "url": use_src["url"],
            "detail": f"Current through {use_src['amended_through']}; extracted {use_src['extracted']} "
                      f"by {use_src['extracted_by']}",
        }]
        fetched = sorted(b["fetched_at"] for b in blocks if b["fetched_at"])
        return {
            "data_version": data_version,
            "settings": zoning.active_settings(),
            "rules_version": rules["version"],
            "rules_status": rules["status"],
            "buffer_m": rules.get("buffer_m", 25),
            "use_table": {
                "amended_through": use_src["amended_through"],
                "extracted": use_src["extracted"],
                "url": use_src["url"],
                "key": zoning.load_use_table()["key"],
                "unlisted_rule": zoning.load_use_table()["unlisted_rule"],
            },
            "legacy_rule": {**rules["legacy_rule"], "ref": zoning.source_ref(
                rules["legacy_rule"]["ref"]["source"], detail=rules["legacy_rule"]["ref"].get("detail"))},
            "overlays_checked": sorted(rules.get("overlay_layers", {}).values()),
            "overlays_not_checked": rules.get("overlays_not_checked"),
            "case_layers": sorted(rules.get("case_layers", {}).values()),
            "score_points": {
                "by_right": settings.zoning_points_by_right,
                "special_exception": settings.zoning_points_special_exception,
                "prohibited": settings.zoning_points_prohibited,
            },
            "stats": {
                "blocks": len(blocks),
                "looked_up": sum(b["looked_up"] for b in blocks),
                "first_lookup": fetched[0] if fetched else None,
                "last_lookup": fetched[-1] if fetched else None,
                "legacy_blocks": sum(any(rules["zones"].get(z, {}).get("legacy") for z in b["zones"]) for b in blocks),
                "overlay_blocks": sum(bool(b["overlays"]) for b in blocks),
                "findings": statuses_total,
            },
            "sources": sources,
            "zones": zones,
            "blocks": blocks,
            "matrix": {"zones": present, "rows": matrix_rows},
        }

    def score_block(self, block_id: int, db: Client | None = None) -> tuple[float | None, dict, list]:
        """
        (score or None, detail plus the run tie, zoning_signals ids) for the gold layer, from the block's
        approved Block Zoning run. Without one the score is None and detail says it was not reviewed.
        """
        db = db or get_supabase()
        a, items = self.approved(block_id, db)
        with self.scope(block_id, db) as (rows, tie):
            ids = [r["id"] for r in rows if r.get("id") is not None]
        if items is None:
            # No version is named: nothing was approved against one.
            return None, {"looked_up": bool(rows), "reviewed": False, "scored": 0, "unscorable": 0,
                          "not_checked": 0, **tie, "analysis": None, "dataset_version": None}, ids
        score, detail = score_items(items)
        looked_up = bool(((a.get("result") or {}).get("summary") or {}).get("looked_up"))
        return score, {**detail, "looked_up": looked_up, **tie}, ids

    def run(self, block_id: int, thread_id: str) -> dict:
        return self.assess_block(block_id)

    def get_pending_review(self, thread_id: str) -> dict:
        return {"status": "review_in_block_zoning", "agent": "ZoningAgent", "note": REVIEW_NOTE}

    def apply_feedback(self, thread_id: str, feedback: str) -> dict:
        return {"status": "review_in_block_zoning", "agent": "ZoningAgent", "note": REVIEW_NOTE}
