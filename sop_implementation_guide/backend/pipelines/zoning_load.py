"""
What a zoning pipeline loads into, and how its outputs become a Zoning version.

A Zoning dataset version is one bundle: the use table, the rules (zone facts
and the recommendation readings), the GIS settings and every block's zones. A
zoning pipeline has up to three outputs, one per part it refreshes:

    block_zones      one record per block: its zones, prior zones, cases, overlays
    use_permissions  one record per ordinance use: P / SE / X / A per zone
    readings         one record per SoP recommendation type: the ordinance uses
                     it is read as, or not a land use

Publishing works like the Zoning tab's refresh: it stores the merged use table,
rules and settings as the next zoning_data_versions row (the data in force),
replaces the published blocks' zoning_signals rows, and snapshots the whole as
the next Zoning dataset version. Parts the pipeline has no output for are
carried over unchanged, and so are blocks, uses and readings it does not list.

Nothing is guessed to make a gap go away. A block whose lookup failed keeps its
rows. A reading that names a use the use table does not have stops the
publish, with the reason.
"""

from __future__ import annotations

import copy
from collections import Counter
from datetime import datetime, timezone

from postgrest.exceptions import APIError
from supabase import Client

from backend.pipelines import engine
from backend.pipelines.engine import PipelineError
from backend.pipelines.zoning_steps import ALL_ZONES, BASE_TABLES, STATUS_VALUES, USE_VALUES, ZONES, zone_field
from backend.repositories.recommendations import BlockRepository
from backend.repositories.zoning import ZoningRepository, ZoningVersionRepository
from backend.tools import datasets, zoning, zoning_refresh

GOVERNED = ["use_table", zoning.NOT_A_LAND_USE, "unknown"]
MIGRATION = ("Publishing recommendation readings needs the rules column on zoning_data_versions. Run this once "
             "in the Supabase SQL editor, then publish again: "
             "ALTER TABLE zoning_data_versions ADD COLUMN IF NOT EXISTS rules JSONB;")


# ── Targets ──────────────────────────────────────────────────────────────────

def _col(use: str, required: bool = False, types=None, names=()) -> dict:
    return {"types": types, "required": required, "use": use, "names": list(names)}


BLOCK_CATALOGUE = {
    "block_id": _col("Which block. Must be a study block id.", True, ("number",), ("block", "block_no", "segment_id", "id")),
    "lookup_status": _col("found, no_zone or failed. A failed block keeps the zones it has.", True, None,
                          ("status", "lookup", "zone_status")),
    "zones": _col("The zones within the buffer, '; '-separated. Findings and scores read them.", False, None,
                  ("zone", "current_zones", "current_zone", "zoning")),
    "prior_zones": _col("The pre-2022 zones, which decide uses in the legacy LCD zone.", False, None,
                        ("prior_zone", "previous_zones", "old_zones")),
    "cases": _col("Development cases near the block, 'Layer: case'-separated; an SME pulls these for LCD questions.",
                  False, None, ("case_numbers", "development_cases", "case")),
    "overlays": _col("Overlay zones near the block, 'Layer: name'-separated; they can add or remove uses.", False, None,
                     ("overlay", "overlay_zones")),
}

BLOCK_FIELDS = [
    {"name": "block_id", "type": "number", "required": True, "description": "The study block's id."},
    {"name": "street_name", "type": "text", "required": False, "description": "The block's street, for reading the output."},
    {"name": "lookup_status", "type": "enum", "required": True, "enum_values": STATUS_VALUES, "extract": False,
     "description": "found, no_zone (no zoning polygon within the buffer) or failed (the county map could not be read)."},
    {"name": "zones", "type": "text", "required": False, "extract": False,
     "description": "Zones within the buffer of the block's centreline, '; '-separated."},
    {"name": "prior_zones", "type": "text", "required": False, "extract": False,
     "description": "Zones on the prior (pre-April 2022) map within the buffer."},
    {"name": "cases", "type": "text", "required": False, "extract": False,
     "description": "Basic Plan, CDP, SDP and Special Exception cases within the buffer, 'Layer: case'."},
    {"name": "overlays", "type": "text", "required": False, "extract": False,
     "description": "Overlay zones within the buffer, 'Layer: name'."},
]

USE_CATALOGUE = {
    "use": _col("The use's name as the use table writes it. Load matches the use table's row by it.", True, None,
                ("use_name", "principal_use", "name")),
    "use_group": _col("The use table's group (e.g. Commercial Uses).", False, None, ("group",)),
    "use_category": _col("The use table's category.", False, None, ("category",)),
    "standards": _col("Use-specific standards (§27-5102 references), '; '-separated.", False, None, ("use_standards",)),
    "amended_through": _col("The amendment the table is current through, cited on every finding.", False, None,
                            ("current_through", "amendment")),
    **{zone_field(z): _col(f"{z} ({(zoning.load_file_rules()['zones'].get(z) or {}).get('name') or 'zone'}): "
                           "P, SE, X or A. Blank keeps the use table's value.", False, ("enum", "text"), (z.lower(),))
       for z in ALL_ZONES},
}


def use_fields(db: Client | None = None) -> list[dict]:
    """The default use-permission fields: a P / SE / X / A field for each zone the study blocks front."""
    zones = []
    if db is not None:
        try:
            zoning.refresh_active(db)
            legacy = {z for z, i in zoning.load_rules()["zones"].items() if i.get("legacy")}
            zones = sorted({r["current_zone"] for r in ZoningRepository(db).get_all()
                            if r.get("current_zone") in ALL_ZONES and r["current_zone"] not in legacy},
                           key=ALL_ZONES.index)
        except Exception:  # noqa: BLE001 — fall back to every zone
            zones = []
    names = zoning.load_file_rules()["zones"]
    return [
        {"name": "use", "type": "text", "required": True, "description": "The use, as the Principal Use Tables name it."},
        {"name": "use_group", "type": "text", "required": False, "description": "Its group in the use table."},
        {"name": "standards", "type": "text", "required": False,
         "description": "Its use-specific standards (§27-5102), '; '-separated."},
        {"name": "amended_through", "type": "text", "required": False,
         "description": "The amendment the table is current through."},
    ] + [{"name": zone_field(z), "type": "enum", "required": False, "enum_values": USE_VALUES,
          "description": f"{z} ({(names.get(z) or {}).get('name') or 'zone'}): P by right, SE special exception, "
                         "X prohibited, A accessory."} for z in zones or ALL_ZONES]


READING_CATALOGUE = {
    "rec_label": _col("The SoP recommendation type, exactly as the blocks carry it.", True, None,
                      ("recommendation", "label", "rec_type", "recommendation_type")),
    "governed_by": _col("use_table, not_a_land_use or unknown. Unknown removes the reading, so findings ask the SME.",
                        False, None, ("governed", "reading", "kind")),
    "ordinance_uses": _col("The use-table rows it is read as, ' | '-separated (use names contain semicolons), named "
                           "exactly as the use table does.",
                           False, None, ("uses", "ordinance_use", "use_rows")),
    "mode": _col("any (one of the uses is enough) or all (a mixed-use building needs every part allowed).", False, None, ()),
    "confidence": _col("high, medium or low. A low reading is put to the SME on every finding.", False, None, ()),
    "note": _col("Why it is read this way; shown on every finding.", False, None, ("reason", "notes", "rationale")),
}

READING_FIELDS = [
    {"name": "rec_label", "type": "text", "required": True, "description": "The recommendation type as the blocks carry it."},
    {"name": "governed_by", "type": "enum", "required": True, "extract": False, "enum_values": GOVERNED,
     "description": "use_table: a land use the Principal Use Tables decide; not_a_land_use: a street or site feature "
                    "(bench, crosswalk) zoning does not decide; unknown: not read yet."},
    {"name": "ordinance_uses", "type": "text", "required": False, "extract": False,
     "description": "The use-table rows it means, ' | '-separated (use names contain semicolons), named exactly "
                    "as the use table does."},
    {"name": "mode", "type": "enum", "required": False, "enum_values": ["any", "all"],
     "description": "any: one of the uses is enough; all: every part must be allowed (a mixed-use building)."},
    {"name": "confidence", "type": "enum", "required": False, "enum_values": ["high", "medium", "low"],
     "description": "How sure the reading is; low readings are put to the SME."},
    {"name": "note", "type": "text", "required": False, "description": "Why it is read this way."},
]

TARGETS = {
    "block_zones": {"label": "Block zones", "default_fields": BLOCK_FIELDS, "catalogue": BLOCK_CATALOGUE},
    "use_permissions": {"label": "Use permissions", "default_fields": use_fields, "catalogue": USE_CATALOGUE},
    "readings": {"label": "Recommendation readings", "default_fields": READING_FIELDS, "catalogue": READING_CATALOGUE},
}


# ── Building the proposal ────────────────────────────────────────────────────

def _blank(v) -> bool:
    return engine._blank(v)


def _split(v, sep: str = ";") -> list[str]:
    """A list held as text. Use names contain semicolons, so ordinance_uses is split on "|" instead."""
    if _blank(v):
        return []
    return [x.strip() for x in str(v).replace("\n", sep).split(sep) if x.strip()]


def _getter(part: dict):
    cols = part["columns"]
    return lambda rec, col: rec["values"].get(cols[col]) if cols.get(col) else None


def _merge_uses(doc: dict, part: dict, pipeline: dict, problems: list[str]) -> dict:
    """The use table with the output's values written in. Uses and cells the output leaves blank keep theirs."""
    doc = copy.deepcopy(doc)
    val = _getter(part)
    cols = part["columns"]
    by_name: dict[str, list[dict]] = {}
    for u in doc["uses"]:
        if u["table"] in BASE_TABLES:
            by_name.setdefault(zoning._norm(u["use"]), []).append(u)
    zone_table = {z: t for t in BASE_TABLES for z in ZONES[t]}
    zone_cols = [(zone_field(z), z) for z in ALL_ZONES if cols.get(zone_field(z))]
    known_urls = {s["text"]: s["url"] for u in doc["uses"] for s in u.get("standards") or []}
    amended: Counter = Counter()
    cites: Counter = Counter()
    for rec in part["records"]:
        name = val(rec, "use")
        if _blank(name):
            continue
        name = str(name).strip()
        rows = by_name.setdefault(zoning._norm(name), [])
        for col, code in zone_cols:
            v = val(rec, col)
            if _blank(v):
                continue
            v = str(v).strip().upper()
            if v not in USE_VALUES:
                problems.append(f"“{name}” has {v!r} for {code}; a use-table value is P, SE, X or A.")
                continue
            t = zone_table[code]
            row = next((r for r in rows if r["table"] == t), None)
            if row is None:
                row = {"use": name, "group": val(rec, "use_group"), "category": val(rec, "use_category"),
                       "table": t, "zones": {}, "standards_text": "", "standards": []}
                doc["uses"].append(row)
                rows.append(row)
            row["zones"][code] = v
        if cols.get("standards") and not _blank(val(rec, "standards")):
            texts = _split(val(rec, "standards"))
            for row in rows:
                if [s["text"] for s in row.get("standards") or []] != texts:
                    row["standards"] = [{"text": t, "url": known_urls.get(t) or doc["source"].get("standards_url")}
                                        for t in texts]
                    row["standards_text"] = "; ".join(texts)
        if not _blank(val(rec, "amended_through")):
            amended[str(val(rec, "amended_through")).strip()] += 1
        if rec.get("source_url"):
            cites[rec["source_url"]] += 1
    src = doc["source"]
    if amended:
        src["amended_through"] = amended.most_common(1)[0][0]
    if cites:
        src["url"] = cites.most_common(1)[0][0]
    src["extracted"] = datetime.now(timezone.utc).date().isoformat()
    src["extracted_by"] = f"pipeline “{pipeline['name']}”"
    src["row_counts"] = dict(Counter(u["table"] for u in doc["uses"]))
    return doc


def _merge_readings(rules: dict, part: dict, index: dict, pipeline: dict, problems: list[str]) -> dict:
    """The rules with the output's readings. Readings the output does not list are kept; unknown removes one."""
    rules = copy.deepcopy(rules)
    val = _getter(part)
    rec_uses = rules["rec_uses"]
    for rec in part["records"]:
        label = str(val(rec, "rec_label") or "").strip()
        if not label:
            continue
        uses_text = val(rec, "ordinance_uses")
        gov = str(val(rec, "governed_by") or "").strip() or ("use_table" if _split(uses_text, "|") else "unknown")
        old = rec_uses.get(label) or {}
        note = None if _blank(val(rec, "note")) else str(val(rec, "note")).strip()
        conf = None if _blank(val(rec, "confidence")) else str(val(rec, "confidence")).strip().lower()
        if gov == "unknown":
            rec_uses.pop(label, None)
            continue
        entry = {k: old[k] for k in ("note_ref",) if k in old}
        if gov == zoning.NOT_A_LAND_USE:
            entry.update({"governed_by": gov, "mode": "any", "confidence": conf or old.get("confidence") or "medium",
                          "uses": []})
        elif gov == "use_table":
            names = _split(uses_text, "|")
            if not names:
                problems.append(f"“{label}” is read as a land use but names no use-table row in ordinance_uses.")
                continue
            old_labels = {zoning._norm(u["row"]): u["label"] for u in old.get("uses") or []}
            uses, missing = [], []
            for n in names:
                row = index.get(zoning._norm(n))
                if not row:
                    missing.append(n)
                    continue
                uses.append({"row": row["use"], "label": old_labels.get(zoning._norm(n)) or row["use"].split(";")[0].strip().lower()})
            if missing:
                problems.append(f"“{label}” names {', '.join(f'“{m}”' for m in missing)}, which "
                                f"{'is' if len(missing) == 1 else 'are'} not a use in the use table being published.")
                continue
            mode = str(val(rec, "mode") or old.get("mode") or "any").strip().lower()
            entry.update({"mode": mode if mode in ("any", "all") else "any",
                          "confidence": conf or old.get("confidence") or "low", "uses": uses})
        else:
            problems.append(f"“{label}”: governed_by is {gov!r}; it must be use_table, not_a_land_use or unknown.")
            continue
        if note or old.get("note"):
            entry["note"] = note or old.get("note")
        rec_uses[label] = entry
    today = datetime.now(timezone.utc).date().isoformat()
    rules["version"] = f"{rules.get('version') or 'v0'} + pipeline “{pipeline['name']}” {today}"
    rules["updated"] = today
    return rules


def _layers(step: dict, sources: dict[str, dict]) -> list[tuple[str | None, str, str]]:
    """(layer id, layer URL, name) for each layer a spatial_lookup step reads."""
    c = step["config"]
    src = sources[c["source_id"]]
    base = src["url"].rstrip("/")
    try:
        names = engine.fetch_source(src).get("layers") or {}
    except PipelineError:
        names = {}
    if base.rsplit("/", 1)[-1].isdigit():
        return [(base.rsplit("/", 1)[-1], base, names.get("") or "layer")]
    return [(str(l), f"{base}/{l}", names.get(str(l)) or f"layer {l}") for l in c.get("layers") or []]


def _grouped(text, step: dict | None, sources: dict, labels: dict) -> dict[str, list[str]]:
    """'Layer: value; Layer: value' (or plain values from one layer) as {label: [values]}."""
    layers = _layers(step, sources) if step else []
    by_name = {name: labels.get(lid) or name for lid, _, name in layers}
    single = by_name[layers[0][2]] if len(layers) == 1 else None
    out: dict[str, list[str]] = {}
    for item in _split(text):
        if single is None and ": " in item:
            name, v = item.split(": ", 1)
            label = by_name.get(name, name)
        else:
            label, v = single or "Cases", item
        out.setdefault(label, [])
        if v not in out[label]:
            out[label].append(v)
    return out


def _block_lookups(db: Client, part: dict, pipeline: dict, settings: dict, rules: dict,
                   problems: list[str]) -> tuple[dict, list[dict], dict]:
    """Per block, the lookup the stored rows hold (as zoning_lookup.lookup_block returns it); failures; new settings."""
    val = _getter(part)
    cols = part["columns"]
    sources = {s["id"]: s for s in pipeline["definition"].get("sources") or []}
    by_field = {(s.get("config") or {}).get("writes"): s for s in part["definition"]["steps"] if s["type"] == "spatial_lookup"}
    step_for = lambda col: by_field.get(cols.get(col)) if cols.get(col) else None
    # A part of the lookup is replaced only when a step in this output produces its field; otherwise
    # the block keeps what it has (mapping a field no step fills must not blank the stored zones).
    produced = {w for s in part["definition"]["steps"]
                for w in engine.step_io(s, part["fields"], pipeline["definition"].get("sources")).get("writes") or []}
    fills = lambda col: bool(cols.get(col)) and cols[col] in produced
    streets = {b["id"]: b.get("street_name") for b in db.table("blocks").select("id, street_name").execute().data or []}
    current: dict[int, list[dict]] = {}
    for r in ZoningRepository(db).get_all():
        current.setdefault(r["block_id"], []).append(r)
    zstep, pstep = step_for("zones"), step_for("prior_zones")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lookups, failures = {}, []
    for rec in part["records"]:
        try:
            b = int(float(val(rec, "block_id")))
        except (TypeError, ValueError):
            problems.append(f"{rec['id']} has no block id.")
            continue
        if b not in streets:
            problems.append(f"Block {b} is not a study block.")
            continue
        status = str(val(rec, "lookup_status") or "").strip()
        if status not in ("found", "no_zone"):
            failures.append({"id": b, "street_name": streets.get(b),
                             "error": "the lookup failed in the pipeline" if status == "failed" else "no lookup status"})
            continue
        lk = {k: v for k, v in (zoning.block_lookup(current.get(b, [])) or {}).items() if k != "signal_ids"}
        lk.setdefault("buffer_m", settings.get("buffer_m"))
        if fills("zones"):
            lk["zones"] = sorted(set(_split(val(rec, "zones")))) if status == "found" else []
        if fills("prior_zones"):
            lk["prior_zones"] = sorted(set(_split(val(rec, "prior_zones"))))
        if fills("cases"):
            lk["cases"] = _grouped(val(rec, "cases"), step_for("cases"), sources, rules.get("case_layers") or {})
        if fills("overlays"):
            ostep = step_for("overlays")
            groups = _grouped(val(rec, "overlays"), ostep, sources, rules.get("overlay_layers") or {})
            lk["overlays"] = [{"layer": k, "values": v} for k, v in groups.items()]
            if ostep:
                labels = rules.get("overlay_layers") or {}
                lk["overlays_checked"] = sorted(labels.get(lid) or name for lid, _, name in _layers(ostep, sources))
        if zstep:
            lk["buffer_m"] = zstep["config"]["buffer_m"]
            lk["layer_url"] = _layers(zstep, sources)[0][1]
        if pstep:
            lk["prior_layer_url"] = _layers(pstep, sources)[0][1]
        lk.setdefault("zones", [])
        lk["fetched_at"] = now
        lookups[str(b)] = lk
    new_settings = dict(settings)
    if zstep:
        new_settings.update(zoning_layer_url=_layers(zstep, sources)[0][1], zone_field=zstep["config"]["field"],
                            buffer_m=zstep["config"]["buffer_m"])
    if pstep:
        new_settings.update(prior_layer_url=_layers(pstep, sources)[0][1], prior_field=pstep["config"]["field"])
    return lookups, failures, new_settings


def proposal(db: Client, pipeline: dict, parts: list[dict]) -> dict:
    """The zoning data publishing these outputs would put in force, with every problem that stops it."""
    zoning.refresh_active(db)
    cur = {"doc": zoning.load_use_table(), "rules": zoning.load_rules(), "settings": zoning.active_settings()}
    by = {p["target"]: p for p in parts}
    problems: list[str] = []
    doc = _merge_uses(cur["doc"], by["use_permissions"], pipeline, problems) if "use_permissions" in by else cur["doc"]
    rules = (_merge_readings(cur["rules"], by["readings"], zoning.build_index(doc), pipeline, problems)
             if "readings" in by else cur["rules"])
    lookups, failures, settings = ({}, [], cur["settings"]) if "block_zones" not in by else \
        _block_lookups(db, by["block_zones"], pipeline, cur["settings"], rules, problems)
    return {"current": cur, "doc": doc, "rules": rules, "settings": settings, "lookups": lookups,
            "failures": failures, "problems": problems, "targets": list(by)}


# ── Diff ─────────────────────────────────────────────────────────────────────

def _outcome(code: str, label: str, rules: dict, index: dict) -> str:
    reading = rules["rec_uses"].get(label)
    if reading and reading.get("governed_by") == zoning.NOT_A_LAND_USE:
        return zoning.NOT_A_LAND_USE
    with zoning.using(None, rules, None):
        return zoning.zone_finding(code, reading, index)["outcome"]


def diff(db: Client, pipeline: dict, parts: list[dict]) -> dict:
    """What publishing would change against the zoning data in force: cells, readings, blocks, outcomes, scores."""
    prop = proposal(db, pipeline, parts)
    cur = prop["current"]
    base = zoning_refresh._diff(db, cur["doc"], prop["doc"], cur["settings"], prop["settings"],
                                prop["lookups"], prop["failures"])
    old_idx, new_idx = zoning.build_index(cur["doc"]), zoning.build_index(prop["doc"])

    readings = {"added": [], "changed": [], "removed": []}
    old_r, new_r = cur["rules"]["rec_uses"], prop["rules"]["rec_uses"]
    show = lambda m: (m.get("governed_by") or "use_table", [u["row"] for u in m.get("uses") or []], m.get("confidence"))
    for label in sorted(set(old_r) | set(new_r)):
        o, n = old_r.get(label), new_r.get(label)
        if o and not n:
            readings["removed"].append({"rec_label": label})
        elif n and not o:
            readings["added"].append({"rec_label": label, "governed_by": show(n)[0], "uses": show(n)[1]})
        elif show(o) != show(n):
            readings["changed"].append({"rec_label": label, "old": show(o), "new": show(n)})

    # Outcomes and scores under the old data vs the new (readings included, which _diff cannot see).
    current: dict[int, list[dict]] = {}
    for r in ZoningRepository(db).get_all():
        current.setdefault(r["block_id"], []).append(r)
    after = {bid: rows for bid, rows in current.items()}
    for key, lk in prop["lookups"].items():
        after[int(key)] = zoning_refresh._rows_from_lookup(lk)
    zones_after = sorted({r["current_zone"] for rows in after.values() for r in rows if r.get("current_zone")})
    flips = []
    for label in sorted(set(old_r) | set(new_r)):
        for z in zones_after:
            o, n = _outcome(z, label, cur["rules"], old_idx), _outcome(z, label, prop["rules"], new_idx)
            if o != n:
                flips.append({"rec_label": label, "zone": z, "old": o, "new": n})
    recs: dict[int, list[dict]] = {}
    for r in BlockRepository(db).all_recommendations():
        recs.setdefault(r["block_id"], []).append(r)
    streets = {b["id"]: b.get("street_name") for b in db.table("blocks").select("id, street_name").execute().data or []}
    scores = []
    for bid in sorted(streets):
        o, _ = zoning.score_block(recs.get(bid, []), current.get(bid, []), old_idx)
        with zoning.using(prop["doc"], prop["rules"], prop["settings"]):
            n, _ = zoning.score_block(recs.get(bid, []), after.get(bid, []), new_idx)
        if o != n:
            scores.append({"id": bid, "street_name": streets[bid], "old": o, "new": n})

    latest = datasets.resolve(db, "zoning")
    return {**base, "outcome_flips": flips, "score_changes": scores, "readings": readings,
            "problems": prop["problems"], "outputs": prop["targets"], "base_version": datasets.label(latest),
            "note": "Publishing merges into the zoning data in force; blocks, uses and readings not in this run are kept."}


# ── Load ─────────────────────────────────────────────────────────────────────

def load(db: Client, pipeline: dict, parts: list[dict], name: str | None, created_by: str | None,
         definition_version: int) -> dict:
    prop = proposal(db, pipeline, parts)
    if prop["problems"]:
        shown = " ".join(prop["problems"][:6]) + (f" (and {len(prop['problems']) - 6} more)" if len(prop["problems"]) > 6 else "")
        raise PipelineError("Not published: " + shown)
    who = (created_by or "").strip() or f"pipeline “{pipeline['name']}”"
    note = f"Pipeline “{pipeline['name']}” def v{definition_version}"
    versions = ZoningVersionRepository(db)
    number = versions.latest_number() + 1
    row = {"version": number, "use_table": prop["doc"], "settings": prop["settings"], "rules": prop["rules"],
           "applied_by": who, "note": note, "run_id": None}
    try:
        versions.insert(row)
    except APIError as e:
        if "rules" not in str(e):
            raise
        if "readings" in prop["targets"]:
            raise PipelineError(MIGRATION) from e
        # Without the column the rules in force are the committed file's, which this publish leaves as they are.
        versions.insert({k: v for k, v in row.items() if k != "rules"})
    zoning.refresh_active(db)

    if prop["lookups"]:
        rows = []
        for key, lk in prop["lookups"].items():
            rows += zoning.signal_rows(int(key), lk, f"pipeline-{pipeline['id']}")
        ZoningRepository(db).replace_for_blocks([int(k) for k in prop["lookups"]], rows)

    version = datasets.snapshot_zoning(
        db, name=name, note=note, created_by=created_by,
        source={"type": "pipeline", "pipeline_id": pipeline["id"], "definition_version": definition_version,
                "zoning_data_version": number, "outputs": prop["targets"]})
    return {"written": sum(len(p["records"]) for p in parts), "dataset_version": version,
            "zoning_data_version": number, "lookup_failures": prop["failures"]}
