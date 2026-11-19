"""
Zoning findings — is this recommendation allowed on this block, and why.

Everything here is a determination, never an inference: the zone comes from
the county's GIS (stored by scripts/sync_block_zoning.py), and whether a use is
allowed comes from the Principal Use Tables of the Zoning Ordinance (§27-5101),
extracted verbatim by scripts/extract_use_table.py. No model is asked whether a
use is allowed. The one judgement in the chain is which ordinance use a SoP
recommendation type means ("Plaza" has no row of its own). Those readings live
in backend/data/zoning_rules.json, are shown on every finding, and become an
SME question when they are a stretch.

A finding is written for the reviewer who has to act on it, so it explains
before it asks. It says what was found, what the ordinance says for each
fronting zone, what that means for the plan, and only then what is still open.
Every sentence carries the numbers of the references that back it, and the
reference list is always returned, even when the lookup has not run.

A recommendation type can also be read as not a land use at all (benches,
crosswalks, signals: street and right-of-way items). The use tables do not
decide those, so the finding says so, cites the reading, and asks nothing.

Outcomes per zone:
  by_right           P in the use table
  special_exception  SE: needs its own application and approval (§27-3604)
  prohibited         X, or not listed ("All uses not listed are prohibited")
  legacy             LCD/LMXC: the use tables do not apply (§27-4205(c)(3))
  unknown            a zone or use we could not resolve
Only the first three are scored; legacy and unknown are never counted as "no".
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from backend.config import settings

DATA = Path(__file__).resolve().parent.parent / "data"

OUTCOMES = {"P": "by_right", "A": "by_right", "SE": "special_exception", "X": "prohibited"}
RANK = {"by_right": 2, "special_exception": 1, "prohibited": 0}
OUTCOME_TEXT = {
    "by_right": "permitted by right",
    "special_exception": "allowed only with a special exception",
    "prohibited": "prohibited",
}
SHORT = {
    "by_right": "by right",
    "special_exception": "special exception",
    "prohibited": "prohibited",
    "legacy": "legacy zone, needs SME",
    "unknown": "needs SME",
}


@lru_cache(maxsize=1)
def load_file_rules() -> dict:
    with open(DATA / "zoning_rules.json", encoding="utf-8") as f:
        return json.load(f)


def load_rules() -> dict:
    """The rules in force: a pinned dataset version's (see using()), else the newest applied version's, else the file."""
    pinned = _PINNED.get()
    return (pinned or {}).get("rules") or _ACTIVE["rules"] or load_file_rules()


@lru_cache(maxsize=1)
def load_file_use_table() -> dict:
    """The use table committed in the repo: version 0, and the fallback."""
    with open(DATA / "zoning_use_table.json", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# The zoning data in force
#
# The use table and the GIS settings can be refreshed from the Zoning tab. An
# applied refresh is a row in zoning_data_versions (the hosted server's disk is
# wiped on redeploy, so it can't be a file). Version 0 is the committed file
# plus the default GIS settings. refresh_active(db) is called at the start of
# each request that reads zoning; it costs one small query, and reloads the
# full version only when a newer one has been applied.
# ---------------------------------------------------------------------------

# rules: the recommendation readings and the rest of zoning_rules.json, when a
# version carries its own (a zoning pipeline's publish does); else the file.
_ACTIVE: dict = {"version": 0, "use_table": None, "settings": None, "rules": None, "applied_by": None,
                 "created_at": None, "note": None}


def refresh_active(db) -> dict:
    """Load the newest applied data version if it changed. Returns its metadata."""
    from backend.repositories.zoning import ZoningTablesMissing, ZoningVersionRepository

    repo = ZoningVersionRepository(db)
    try:
        latest = repo.latest_number()
    except ZoningTablesMissing:
        latest = 0
    if latest != _ACTIVE["version"]:
        if latest == 0:
            _ACTIVE.update(version=0, use_table=None, settings=None, rules=None, applied_by=None, created_at=None,
                           note=None)
        else:
            row = repo.get_version(latest)
            _ACTIVE.update(version=latest, use_table=row["use_table"], settings=row["settings"],
                           rules=row.get("rules"), applied_by=row.get("applied_by"),
                           created_at=row.get("created_at"), note=row.get("note"))
    return active_version()


def active_version() -> dict:
    return {k: _ACTIVE[k] for k in ("version", "applied_by", "created_at", "note")}


# ---------------------------------------------------------------------------
# Evaluating against a dataset version
#
# A block analysis is tied to one Zoning dataset version (see
# backend/tools/datasets.py), which may be older than the data in force.
# using() makes that version's use table, rules and settings the ones every
# function below reads, for the current request only: a ContextVar is per
# thread, so concurrent requests on other versions are unaffected.
# ---------------------------------------------------------------------------

_PINNED: ContextVar[dict | None] = ContextVar("zoning_pinned_version", default=None)


@contextmanager
def using(use_table: dict | None, rules: dict | None, settings_: dict | None):
    token = _PINNED.set({"use_table": use_table, "rules": rules, "settings": settings_})
    try:
        yield
    finally:
        _PINNED.reset(token)


def load_use_table() -> dict:
    """The use table in force: a pinned dataset version's, the newest applied version, else the file."""
    pinned = _PINNED.get()
    return (pinned or {}).get("use_table") or _ACTIVE["use_table"] or load_file_use_table()


def active_settings() -> dict:
    """GIS settings in force: defaults overlaid with a pinned version's, else the newest applied one's."""
    from backend.tools.zoning_lookup import default_settings

    pinned = _PINNED.get()
    if pinned and pinned.get("settings"):
        return {**default_settings(load_rules()), **pinned["settings"]}
    return {**default_settings(load_rules()), **(_ACTIVE["settings"] or {})}


def _norm(s: str) -> str:
    s = (s or "").replace("’", "'").replace("‘", "'").lower()
    return re.sub(r"\s+", " ", s).strip()


def _use_index() -> dict[str, dict]:
    return build_index(load_use_table())


# id(doc) -> (doc, index). The doc is kept so its id can't be reused by another
# object while cached, and is compared by identity before a hit is trusted.
_INDEX_CACHE: dict[int, tuple[dict, dict]] = {}


def build_index(doc: dict) -> dict[str, dict]:
    """
    Base-zone use rows by name, merged across tables (c), (d) and (e).

    The overlay table (f) is left out: its X and P* mean "overrides the base
    zone", not "allowed here", so mixing it in would corrupt the base answer.
    Cached per document, so switching data versions rebuilds it once.
    """
    hit = _INDEX_CACHE.get(id(doc))
    if hit and hit[0] is doc:
        return hit[1]
    if len(_INDEX_CACHE) > 4:
        _INDEX_CACHE.clear()
    idx: dict[str, dict] = {}
    for u in doc["uses"]:
        if u["table"] == "27-5101(f)":
            continue
        e = idx.setdefault(_norm(u["use"]), {"use": u["use"], "zones": {}, "tables": {}, "standards": []})
        for zone, value in u["zones"].items():
            e["zones"][zone] = value
            e["tables"][zone] = u["table"]
        for s in u["standards"]:
            if s not in e["standards"]:
                e["standards"].append(s)
    _INDEX_CACHE[id(doc)] = (doc, idx)
    return idx


NOT_A_LAND_USE = "not_a_land_use"


def reading_for(rec_label: str | None) -> dict | None:
    """How a recommendation type is read: as ordinance uses, or as not a land use. None = not read yet."""
    return load_rules()["rec_uses"].get((rec_label or "").strip())


def mapping_for(rec_label: str | None) -> dict | None:
    """The ordinance uses a recommendation type is read as. None when it is unread or not a land use."""
    r = reading_for(rec_label)
    return None if not r or r.get("governed_by") == NOT_A_LAND_USE else r


def block_lookup(rows: list[dict]) -> dict | None:
    """Collapse a block's zoning_signals rows into one lookup. None = never looked up."""
    if not rows:
        return None
    lookup = dict((rows[0].get("raw_data") or {}).get("lookup") or {})
    lookup["zones"] = sorted({r["current_zone"] for r in rows if r.get("current_zone")})
    lookup["signal_ids"] = [r["id"] for r in rows if r.get("id") is not None]
    return lookup


# ---------------------------------------------------------------------------
# Determination
# ---------------------------------------------------------------------------

def zone_finding(code: str, mapping: dict | None, index: dict | None = None) -> dict:
    """
    What the ordinance says about one recommendation's uses in one zone.

    index: a use index from build_index(); the one in force if omitted. A
    refresh passes the proposed table's index to show what would change.
    """
    zinfo = load_rules()["zones"].get(code, {})
    f = {"zone": code, "name": zinfo.get("name"), "uses": []}
    if zinfo.get("legacy"):
        f["outcome"] = "legacy"
        return f
    if mapping is None:
        f["outcome"] = "unknown"
        return f

    for u in mapping["uses"]:
        row = (index if index is not None else _use_index()).get(_norm(u["row"]))
        value = row["zones"].get(code) if row else None
        f["uses"].append({
            "label": u["label"],
            "row": u["row"],
            "value": value,
            "outcome": OUTCOMES.get(value or ""),
            "table": row["tables"].get(code) if row else None,
            # Use-specific standards bind P uses; SE uses follow the SE standards.
            "standards": (row["standards"] if row and value in ("P", "A") else []),
        })

    known = [u for u in f["uses"] if u["outcome"]]
    if not known or (mapping["mode"] == "all" and len(known) < len(f["uses"])):
        f["outcome"] = "unknown"
    elif mapping["mode"] == "all":
        # Every part of a mixed-use building has to be allowed.
        f["outcome"] = min((u["outcome"] for u in known), key=RANK.get)
    else:
        f["outcome"] = max((u["outcome"] for u in known), key=RANK.get)
    return f


def best_outcome(findings: list[dict]) -> str | None:
    resolved = [f["outcome"] for f in findings if f["outcome"] in RANK]
    return max(resolved, key=RANK.get) if resolved else None


# ---------------------------------------------------------------------------
# Narrative with references
# ---------------------------------------------------------------------------

class _Refs:
    """An ordered, de-duplicated reference list; add() returns a 1-based number."""

    def __init__(self):
        self.items: list[dict] = []

    def add(self, label: str, url: str | None = None, detail: str | None = None) -> int:
        for i, r in enumerate(self.items, start=1):
            if r["label"] == label and r["url"] == url:
                return i
        self.items.append({"label": label, "url": url, "detail": detail})
        return len(self.items)

    def source(self, key: str, page: int | None = None, detail: str | None = None) -> int:
        r = source_ref(key, page, detail)
        return self.add(r["label"], r["url"], r["detail"])


def source_ref(key: str, page: int | None = None, detail: str | None = None) -> dict:
    """A named source from zoning_rules.json as a reference, pinned to a PDF page if given."""
    src = load_rules()["sources"][key]
    return {
        "label": src["label"] + (f", PDF p. {page}" if page else ""),
        "url": src["url"] + (f"#page={page}" if page else ""),
        "detail": detail,
    }


def _join(items: list[str], word: str = "and") -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {word} " + items[-1]


def _cases_text(cases: dict) -> str:
    parts = []
    for layer, nums in cases.items():
        shown = ", ".join(nums[:4]) + (f" and {len(nums) - 4} more" if len(nums) > 4 else "")
        parts.append(f"{layer}: {shown}")
    return "; ".join(parts)


def _key_cases(cases: dict) -> str:
    """The parent approvals an SME would pull first: Basic Plans and CDPs, else the first SDPs."""
    top = cases.get("Zoning Map Amendment / Basic Plan", []) + cases.get("Comprehensive Design Plan", [])
    return ", ".join(top[:3] or cases.get("Specific Design Plan", [])[:3])


def _zone_sentence(f: dict, mode: str, refs: _Refs) -> tuple[str, list[int]]:
    use_src = load_use_table()["source"]
    known = [u for u in f["uses"] if u["outcome"]]
    tables = sorted({u["table"] for u in known if u["table"]})
    nums = [refs.add(f"Zoning Ordinance Table {t}", use_src["url"],
                     f"Section 27-5101, current through {use_src['amended_through']}") for t in tables]

    outcomes = {u["outcome"] for u in known}
    if len(known) > 1 and len(outcomes) == 1:
        body = (f"{_join([u['label'] for u in known])} are {'both' if len(known) == 2 else 'all'} "
                f"{OUTCOME_TEXT[known[0]['outcome']]}")
    else:
        body = "; ".join(f"{u['label']} is {OUTCOME_TEXT[u['outcome']]}" for u in known)
    text = f"{f['zone']} ({f['name']}): {body} (Table {_join(tables)})"

    standards = []
    for u in known:
        if u["outcome"] == "by_right":
            standards += [s for s in u["standards"] if s not in standards]
    if standards:
        text += ", subject to the use-specific standards in §" + _join([s["text"] for s in standards])
        nums += [refs.add(f"Zoning Ordinance §{s['text']}, use-specific standards", s["url"]) for s in standards]
    text += "."
    if mode == "all" and len(f["uses"]) > 1:
        text += f" A mixed-use building needs every part allowed, so it is {OUTCOME_TEXT[f['outcome']]} here."
    return text, nums


# Narrative sections, in reading order: the answer comes before the background.
SECTIONS = {
    "found": "What we found",
    "ordinance": "What the ordinance says",
    "meaning": "What it means for the plan",
    "reading": "How we read the recommendation",
    "background": "Background",
    "checks": "What we checked",
}


def assess(rec: dict, rows: list[dict], block: dict | None = None) -> dict:
    """
    The zoning finding for one recommendation on one block.

    rec: a block_recommendations row. rows: the block's zoning_signals rows.
    block: the blocks row, for the street name in the narrative (optional).

    Returns {status, summary, zones, narrative: [{section, text, refs}],
    sections, questions: [{text, refs}], references: [{label, url, detail}],
    best, scorable}. refs are 1-based indexes into references.
    status: 'attached' (resolved, nothing to confirm), 'partial' (resolved
    but with SME questions), 'needs_sme' (nothing resolved), or 'missing'
    (the block has not been looked up).
    """
    rules = load_rules()
    refs = _Refs()
    narrative: list[dict] = []
    questions: list[dict] = []

    def say(section: str, text: str, *nums: int | None):
        narrative.append({"section": section, "text": text, "refs": sorted({n for n in nums if n})})

    def ask(text: str, *nums: int | None):
        questions.append({"text": text, "refs": sorted({n for n in nums if n})})

    label = (rec.get("rec_label") or "").strip()
    down = rec.get("direction") == "Decrease"
    mapping = mapping_for(label)
    reading = reading_for(label)
    lookup = block_lookup(rows)
    where = f"Block {rec.get('block_id')}" + (f" ({block['street_name']})" if block and block.get("street_name") else "")
    # Name the thing in the ordinance's terms ("apartments above and ground-floor retail").
    if mapping:
        thing = _join([u["label"] for u in mapping["uses"]], "or" if mapping["mode"] == "any" else "and")
    else:
        thing = f"“{label}”"

    def result(status: str, summary: str, findings: list[dict], best: str | None) -> dict:
        order = list(SECTIONS)
        narrative.sort(key=lambda p: order.index(p["section"]))
        return {
            "status": status,
            "summary": summary,
            "zones": findings,
            "narrative": narrative,
            "sections": SECTIONS,
            "questions": questions,
            "references": refs.items,
            "best": best,
            "scorable": (not down) and mapping is not None and best is not None,
        }

    if reading and reading.get("governed_by") == NOT_A_LAND_USE:
        # Not a use of land (a bench, a crosswalk): the zone does not matter, so nothing is asked.
        r_mapping = refs.add("Recommendation-to-use mapping", None,
                             f"Zoning readings {rules['version']}: “{label}” read as not a land use")
        text = (f"We read “{label}” as not a land use: it is a street or site feature, not a use the Principal "
                "Use Tables list, so zoning does not decide whether it is allowed.")
        if reading.get("note"):
            text += " " + reading["note"]
        say("reading", text, r_mapping)
        say("meaning", "No zoning approval is needed for this recommendation; whoever owns the street or site "
            "decides it.", r_mapping)
        return result("attached", "Not a land use: zoning does not decide it", [], None)

    if lookup is None:
        r = refs.source("zoning_map", detail="Not looked up for this block yet")
        say("found", f"{where} has not been looked up on the county zoning map yet. "
            "Run python -m scripts.sync_block_zoning to fill this step.", r)
        return result("missing", "Zoning lookup not run for this block", [], None)

    buffer_m = lookup.get("buffer_m", rules.get("buffer_m", 25))
    looked_up = (lookup.get("fetched_at") or "")[:10]
    # Cite the layer this block was actually looked up from, which a refresh can change.
    map_ref = source_ref("zoning_map", detail=f"Zones within {buffer_m} m of the block centerline, looked up {looked_up}")
    r_map = refs.add(map_ref["label"], lookup.get("layer_url") or map_ref["url"], map_ref["detail"])

    # What we found.
    codes = lookup["zones"]
    if not codes:
        say("found", f"No zoning polygon lies within {buffer_m} m of {where}'s centerline, so its zone "
            "could not be determined; the segment may border only public right-of-way.", r_map)
        ask(f"What zoning applies to the land along {where}?", r_map)
    else:
        names = [f"{c} ({rules['zones'].get(c, {}).get('name') or 'name not on file'})" for c in codes]
        n = len(codes)
        say("found", f"{where} fronts {n} zone{'s' if n > 1 else ''} within {buffer_m} m of its centerline: "
            f"{_join(names)}.", r_map)

    # How we read the recommendation.
    r_mapping = refs.add("Recommendation-to-use mapping", None,
                         f"Recommendation readings {rules['version']}: our reading, not yet confirmed by an SME")
    if mapping:
        rows_q = _join([f"“{u['row']}”" for u in mapping["uses"]], "or" if mapping["mode"] == "any" else "and")
        text = f"We read “{label}” as the ordinance use {rows_q}."
        nums = [r_mapping]
        if mapping.get("note"):
            text += " " + mapping["note"]
            if mapping.get("note_ref"):
                nums.append(refs.source(mapping["note_ref"]))
        say("reading", text, *nums)
        if mapping.get("confidence") == "low":
            ask(f"Is our reading of “{label}” right? {mapping.get('note') or ''}".strip(), *nums)
    else:
        say("reading", f"“{label}” is not mapped to an ordinance use yet, so its zoning cannot be checked.",
            r_mapping)
        ask(f"Which ordinance use in Table 27-5101 matches “{label}”?", r_mapping)

    # What the ordinance says in each zone.
    findings = [zone_finding(c, mapping) for c in codes]
    support: dict[str, list[int]] = {}
    for f in findings:
        if f["outcome"] in RANK:
            text, nums = _zone_sentence(f, mapping["mode"], refs)
            say("ordinance", text, *nums)
            support.setdefault(f["outcome"], []).extend(nums)
        elif f["outcome"] == "unknown" and mapping:
            say("ordinance", f"{f['zone']}: this zone is not in the use tables we extracted, so we cannot say.", r_map)
            ask(f"What does the ordinance allow in {f['zone']} for {thing}?", r_map)

    # Legacy zones: explain the rule, name the approvals, then ask.
    legacy = [f for f in findings if f["outcome"] == "legacy"]
    if legacy:
        lr = rules["legacy_rule"]
        r_rule = refs.source(lr["ref"]["source"], detail=lr["ref"].get("detail"))
        zl = _join([f["zone"] for f in legacy])
        say("ordinance", f"{zl}: {lr['text']}", r_rule)

        prior = [p for p in lookup.get("prior_zones", []) if p in rules["prior_design_zones"]]
        prior_ref = source_ref("prior_zoning", detail=f"Prior zones within {buffer_m} m")
        r_prior = refs.add(prior_ref["label"], lookup.get("prior_layer_url") or prior_ref["url"], prior_ref["detail"])
        if prior:
            prior_txt = _join([f"{p} ({rules['prior_design_zones'][p]})" for p in prior], "or")
            say("ordinance", f"On the prior map, the comprehensive design zone near this block was {prior_txt}.", r_prior)
        else:
            say("ordinance", f"The prior map shows no comprehensive design zone within {buffer_m} m, "
                "so which prior zone applies is unclear.", r_prior)

        cases = lookup.get("cases") or {}
        r_cases = refs.source("cases", detail=f"Cases within {buffer_m} m, looked up {looked_up}")
        if cases:
            say("ordinance", f"Approved plans on file near this block: {_cases_text(cases)}.", r_cases)
        else:
            say("ordinance", f"No Basic Plan, CDP or SDP case was found within {buffer_m} m of this block.", r_cases)

        verb = "allow or limit" if down else "allow"
        governing = f" ({_key_cases(cases)})" if cases else ""
        prior_hint = f", given the prior {_join(prior, 'or')} zone" if prior else ""
        ask(f"For the {zl} frontage: which approved plan governs it{governing}, and does it {verb} "
            f"{thing}{prior_hint}?", r_rule, r_prior, r_cases)

    # Background: why the map looks the way it does.
    for f in findings:
        for c in rules["zones"].get(f["zone"], {}).get("context", []):
            ref = c["ref"]
            say("background", c["text"], refs.source(ref["source"], page=ref.get("page"), detail=ref.get("detail")))

    # What it means for the plan.
    best = best_outcome(findings)
    unresolved = [f["zone"] for f in findings if f["outcome"] not in RANK]
    r_se = None
    if "special_exception" in support:
        r_se = refs.add("Zoning Ordinance §27-3604, Special Exception",
                        load_use_table()["source"]["special_exception_url"])
    if down:
        by = {o: [f["zone"] for f in findings if f["outcome"] == o] for o in RANK}
        parts = []
        if by["by_right"]:
            parts.append(f"permitted by right in {_join(by['by_right'])}")
        if by["special_exception"]:
            parts.append(f"allowed only with a special exception in {_join(by['special_exception'])}")
        if by["prohibited"]:
            parts.append(f"prohibited in {_join(by['prohibited'])}")
        text = "This recommendation is a reduction, and zoning cannot remove an existing use."
        if parts:
            text += f" A new {thing} is {_join(parts, 'and')}."
        text += " A reduction depends on redevelopment or owner decisions, not on zoning."
        say("meaning", text, r_se, *[n for v in support.values() for n in v])
    elif best == "by_right":
        zs = _join([f["zone"] for f in findings if f["outcome"] == "by_right"])
        say("meaning", f"Zoning allows this by right on the {zs} frontage, so no zoning hearing is needed there.",
            *support["by_right"])
    elif best == "special_exception":
        zs = _join([f["zone"] for f in findings if f["outcome"] == "special_exception"])
        say("meaning", f"The best case is a special exception ({zs}). That needs its own application and "
            "approval under §27-3604, so plan for a hearing.", r_se, *support["special_exception"])
    elif best == "prohibited":
        say("meaning", "Zoning prohibits this on every frontage we could check. Only a rezoning or a text "
            "amendment to the ordinance would change that.", *support["prohibited"])
    if unresolved and codes:
        say("meaning", f"The {_join(unresolved)} frontage is not resolved yet; the questions below say what is needed.")

    # What we checked.
    overlays = lookup.get("overlays") or []
    if overlays:
        r_ov = refs.source("overlay_table")
        names = _join([
            o["layer"] + (f" ({', '.join(v for v in o['values'] if v != 'present')})"
                          if any(v != "present" for v in o["values"]) else "")
            for o in overlays
        ])
        say("checks", f"The block also lies in the {names}, which can add or remove uses whatever the base "
            "zone says.", r_ov, r_map)
        ask(f"Does the {names} change this?", r_ov)
    else:
        checked = ", ".join(lookup.get("overlays_checked") or [])
        say("checks", f"No overlay zone was found within {buffer_m} m (checked: {checked}). "
            f"{rules['overlays_not_checked']}", r_map)

    if questions and best is not None:
        status = "partial"
    elif questions:
        status = "needs_sme"
    else:
        status = "attached"
    summary = " · ".join(f"{f['zone']}: {SHORT[f['outcome']]}" for f in findings) or "No zone found"
    return result(status, summary, findings, best)


# ---------------------------------------------------------------------------
# Block score
# ---------------------------------------------------------------------------

def score_block(recs: list[dict], rows: list[dict], index: dict | None = None) -> tuple[float | None, dict]:
    """
    Zoning sub-score 0-100 for a block, or None if nothing on it can be scored.

    Each "Increase" recommendation that maps to an ordinance use scores its best
    fronting zone: by right, special exception or prohibited (points in
    config.py). Legacy and unresolved zones are left out, never scored as
    prohibited. "Decrease" recommendations are left out: zoning does not decide
    whether a use goes away.
    """
    lookup = block_lookup(rows)
    detail = {"looked_up": lookup is not None, "scored": 0, "unscorable": 0}
    if lookup is None:
        return None, detail
    points = {
        "by_right": settings.zoning_points_by_right,
        "special_exception": settings.zoning_points_special_exception,
        "prohibited": settings.zoning_points_prohibited,
    }
    scores = []
    for rec in recs:
        mapping = mapping_for(rec.get("rec_label"))
        if rec.get("direction") == "Decrease" or mapping is None:
            continue
        best = best_outcome([zone_finding(c, mapping, index) for c in lookup["zones"]])
        if best is None:
            detail["unscorable"] += 1
            continue
        scores.append(points[best])
    detail["scored"] = len(scores)
    return (round(sum(scores) / len(scores), 1) if scores else None), detail


# ---------------------------------------------------------------------------
# Stored rows
# ---------------------------------------------------------------------------

def signal_rows(block_id: int, lookup: dict, run_id: str) -> list[dict]:
    """
    zoning_signals rows for one block's lookup: one per zone, or a single row
    with current_zone NULL when the lookup found no zone, so "looked up, no
    zone" stays distinguishable from "not looked up".
    """
    rules = load_rules()
    park = mapping_for("Park/Playground")
    mixed = mapping_for("Vertical mixed used buildings")

    def allows(code: str, mapping: dict) -> bool | None:
        outcome = zone_finding(code, mapping)["outcome"]
        if outcome in ("by_right", "special_exception"):
            return True
        return False if outcome == "prohibited" else None

    raw = {"lookup": lookup, "rules_version": rules["version"], "data_version": _ACTIVE["version"]}
    if not lookup["zones"]:
        return [{
            "block_id": block_id, "current_zone": None, "zone_description": None,
            "barrier_notes": f"No zoning polygon within {lookup['buffer_m']} m of the block.",
            "raw_data": raw, "thread_id": run_id, "reviewed": False,
        }]
    out = []
    for code in lookup["zones"]:
        zinfo = rules["zones"].get(code, {})
        out.append({
            "block_id": block_id,
            "current_zone": code,
            "zone_description": zinfo.get("name"),
            "allows_parks": allows(code, park),
            "allows_mixed_use": allows(code, mixed),
            "barrier_notes": (
                "Legacy zone: uses are set by the site's approved plans (§27-4205(c)(3))."
                if zinfo.get("legacy") else None
            ),
            "raw_data": raw,
            "thread_id": run_id,
            "reviewed": False,
        })
    return out
