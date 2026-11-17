"""
Refresh the zoning data from sources the user names, review, then apply.

Started from the Zoning tab. A refresh can re-extract the use table from a
county code URL, re-look-up block zones from a zoning layer, or both. It never
changes what the app shows on its own:

  1. start()     records a run and does the work in a background thread,
                 writing progress as it goes;
  2. (thread)    extracts / looks up into a proposal, and computes the diff
                 against the data in force: use-table cells, recommendation
                 outcomes per zone, block zones and scores, plus any failures;
  3. apply()     after the user reviews the diff, stores the proposal as a new
                 zoning_data_versions row and replaces the refreshed blocks'
                 zoning_signals rows. Every finding, plan and score reads it
                 from the next request on;
     discard()   or drops it.

The same safety rails as the scripts apply. A user-supplied layer is checked
before any block runs. A layer with no polygons around our blocks is refused.
A use table whose zone columns moved is refused. A block whose lookup fails
keeps its current rows and is listed in the diff.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from supabase import Client

from backend.db.session import get_supabase
from backend.repositories.recommendations import BlockRepository
from backend.repositories.zoning import ZoningRepository, ZoningRunRepository, ZoningVersionRepository
from backend.tools import zoning, zoning_lookup
from backend.tools.use_table_extract import UseTableError, extract

log = logging.getLogger(__name__)

# A run still "running" after this long was cut off (e.g. a server restart).
STALE_AFTER = timedelta(minutes=30)
SETTING_KEYS = ("zoning_layer_url", "zone_field", "prior_layer_url", "prior_field", "buffer_m")


class RefreshError(Exception):
    """The request can't be run or applied as given."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(inputs: dict) -> dict:
    """Validate the form: what to refresh and from where. Raises RefreshError."""
    ut = inputs.get("use_table") or {}
    mp = inputs.get("map") or {}
    out = {
        "use_table": {"mode": ut.get("mode") or "keep", "url": (ut.get("url") or "").strip()},
        "map": {"mode": mp.get("mode") or "keep"},
    }
    if out["use_table"]["mode"] not in ("keep", "extract"):
        raise RefreshError("use_table.mode must be 'keep' or 'extract'.")
    if out["map"]["mode"] not in ("keep", "lookup"):
        raise RefreshError("map.mode must be 'keep' or 'lookup'.")
    if out["use_table"]["mode"] == "keep" and out["map"]["mode"] == "keep":
        raise RefreshError("Nothing to refresh: choose a use-table source, a zoning-map lookup, or both.")
    if out["use_table"]["mode"] == "extract" and not out["use_table"]["url"]:
        raise RefreshError("Give the URL of the use table to extract.")

    if out["map"]["mode"] == "lookup":
        current = zoning.active_settings()
        for k in SETTING_KEYS:
            v = mp.get(k)
            if isinstance(v, str):
                v = v.strip()
            out["map"][k] = v if v not in (None, "") else current[k]
        try:
            out["map"]["buffer_m"] = float(out["map"]["buffer_m"])
        except (TypeError, ValueError):
            raise RefreshError("The buffer must be a number of metres.")
        if not 5 <= out["map"]["buffer_m"] <= 100:
            raise RefreshError("The buffer must be between 5 and 100 metres.")
        blocks = mp.get("blocks")
        if blocks:
            try:
                out["map"]["blocks"] = sorted({int(b) for b in blocks})
            except (TypeError, ValueError):
                raise RefreshError("Blocks must be block ids, e.g. 15, 16.")
        else:
            out["map"]["blocks"] = None
    return out


def start(db: Client, inputs: dict, requested_by: str, note: str | None) -> dict:
    """Record a run and start it in the background. Raises RefreshError, ZoningTablesMissing."""
    requested_by = (requested_by or "").strip()
    if not requested_by:
        raise RefreshError("Say who is requesting the refresh.")
    runs = ZoningRunRepository(db)
    for r in runs.running():
        started = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - started > STALE_AFTER:
            runs.update(r["id"], {"status": "failed", "finished_at": _now(),
                                  "error": "Stopped without finishing (the server likely restarted). Start a new refresh."})
        else:
            raise RefreshError(f"Refresh #{r['id']} is still running. Wait for it, then review or discard it.")

    clean = normalize(inputs)
    clean["base_version"] = zoning.refresh_active(db)["version"]
    run = runs.create({
        "status": "running",
        "inputs": clean,
        "requested_by": requested_by,
        "note": (note or "").strip() or None,
        "progress": {"step": "Starting", "done": 0, "total": 0},
    })
    threading.Thread(target=_execute, args=(run["id"], clean), daemon=True).start()
    return run


def _execute(run_id: int, inputs: dict) -> None:
    db = get_supabase()
    runs = ZoningRunRepository(db)

    def progress(step: str, done: int = 0, total: int = 0):
        runs.update(run_id, {"progress": {"step": step, "done": done, "total": total}})

    try:
        zoning.refresh_active(db)
        rules = zoning.load_rules()
        cur_doc = zoning.load_use_table()
        cur_settings = zoning.active_settings()

        new_doc = cur_doc
        if inputs["use_table"]["mode"] == "extract":
            progress("Extracting the use table")
            new_doc = extract(inputs["use_table"]["url"])

        new_settings = cur_settings
        lookups: dict[str, dict] = {}
        failures: list[dict] = []
        if inputs["map"]["mode"] == "lookup":
            mp = inputs["map"]
            new_settings = {**cur_settings, **{k: mp[k] for k in SETTING_KEYS}}
            progress("Checking the zoning layers")
            zoning_lookup.describe_layer(new_settings["zoning_layer_url"], new_settings["zone_field"])
            zoning_lookup.describe_layer(new_settings["prior_layer_url"], new_settings["prior_field"])

            query = db.table("blocks").select("id, street_name, geometry_geojson").order("id")
            if mp.get("blocks"):
                query = query.in_("id", mp["blocks"])
            blocks = [b for b in (query.execute().data or []) if b.get("geometry_geojson")]
            if not blocks:
                raise RefreshError("None of the requested blocks exist.")
            covered = zoning_lookup.sanity_check(
                zoning_lookup.bbox([b["geometry_geojson"] for b in blocks]), new_settings, rules
            )
            if covered == 0:
                raise RefreshError("The zoning layer has no polygons around our blocks, so every block would "
                                   "come back with no zone. Check the layer URL.")
            for i, b in enumerate(blocks, start=1):
                progress(f"Looking up block {b['id']} ({b.get('street_name') or ''})", i - 1, len(blocks))
                try:
                    lookups[str(b["id"])] = zoning_lookup.lookup_block(b["geometry_geojson"], rules, new_settings)
                except zoning_lookup.ZoningLookupError as e:
                    failures.append({"id": b["id"], "street_name": b.get("street_name"), "error": str(e)})

        progress("Comparing with the data in force")
        diff = _diff(db, cur_doc, new_doc, cur_settings, new_settings, lookups, failures)
        runs.update(run_id, {
            "status": "ready",
            "diff": diff,
            "proposed": {
                "use_table": new_doc if new_doc is not cur_doc else None,
                "settings": new_settings,
                "lookups": lookups,
            },
            "progress": {"step": "Ready for review", "done": len(lookups), "total": len(lookups) + len(failures)},
            "finished_at": _now(),
        })
    except (UseTableError, zoning_lookup.ZoningLookupError, RefreshError) as e:
        runs.update(run_id, {"status": "failed", "error": str(e), "finished_at": _now()})
    except Exception as e:  # noqa: BLE001 — record any failure on the run rather than lose it in a thread
        log.exception("Zoning refresh %s failed", run_id)
        runs.update(run_id, {"status": "failed", "error": f"Unexpected error: {e}", "finished_at": _now()})


def _rows_from_lookup(lookup: dict) -> list[dict]:
    """Stand-in zoning_signals rows for a proposed lookup, so the diff can score it."""
    zones = lookup["zones"] or [None]
    return [{"current_zone": z, "raw_data": {"lookup": lookup}} for z in zones]


def _diff(db, cur_doc, new_doc, cur_settings, new_settings, lookups, failures) -> dict:
    rules = zoning.load_rules()
    old_idx, new_idx = zoning.build_index(cur_doc), zoning.build_index(new_doc)

    settings_changes = [
        {"key": k, "old": cur_settings.get(k), "new": new_settings.get(k)}
        for k in sorted(set(cur_settings) | set(new_settings)) if cur_settings.get(k) != new_settings.get(k)
    ]

    cells = []
    for name in sorted(set(old_idx) | set(new_idx)):
        o = (old_idx.get(name) or {}).get("zones", {})
        n = (new_idx.get(name) or {}).get("zones", {})
        use = (new_idx.get(name) or old_idx.get(name))["use"]
        for z in sorted(set(o) | set(n)):
            if o.get(z) != n.get(z):
                cells.append({"use": use, "zone": z, "old": o.get(z), "new": n.get(z)})
    added = sorted(new_idx[k]["use"] for k in set(new_idx) - set(old_idx))
    removed = sorted(old_idx[k]["use"] for k in set(old_idx) - set(new_idx))

    streets = {b["id"]: b.get("street_name")
               for b in (db.table("blocks").select("id, street_name").order("id").execute().data or [])}
    current: dict[int, list[dict]] = {}
    for r in ZoningRepository(db).get_all():
        current.setdefault(r["block_id"], []).append(r)
    after = {bid: (_rows_from_lookup(lookups[str(bid)]) if str(bid) in lookups else rows)
             for bid, rows in {**{b: [] for b in streets}, **current}.items()}

    def summary(lk: dict | None) -> dict:
        lk = lk or {}
        return {"zones": lk.get("zones", []), "prior_zones": lk.get("prior_zones", []),
                "cases": sum(len(v) for v in (lk.get("cases") or {}).values()),
                "overlays": sorted(o["layer"] for o in lk.get("overlays") or [])}

    blocks = []
    for key, lk in lookups.items():
        bid = int(key)
        old, new = summary(zoning.block_lookup(current.get(bid, []))), summary(lk)
        if old != new:
            blocks.append({"id": bid, "street_name": streets.get(bid), "old": old, "new": new})

    zones_after = sorted({r["current_zone"] for rows in after.values() for r in rows if r.get("current_zone")})
    flips = []
    for label, mapping in rules["rec_uses"].items():
        for z in zones_after:
            o = zoning.zone_finding(z, mapping, old_idx)["outcome"]
            n = zoning.zone_finding(z, mapping, new_idx)["outcome"]
            if o != n:
                flips.append({"rec_label": label, "zone": z, "old": o, "new": n})

    recs: dict[int, list[dict]] = {}
    for r in BlockRepository(db).all_recommendations():
        recs.setdefault(r["block_id"], []).append(r)
    scores = []
    for bid in streets:
        o, _ = zoning.score_block(recs.get(bid, []), current.get(bid, []), old_idx)
        n, _ = zoning.score_block(recs.get(bid, []), after.get(bid, []), new_idx)
        if o != n:
            scores.append({"id": bid, "street_name": streets[bid], "old": o, "new": n})

    old_src, new_src = cur_doc["source"], new_doc["source"]
    return {
        "use_table": {
            "refreshed": new_doc is not cur_doc,
            "old_amended_through": old_src.get("amended_through"),
            "new_amended_through": new_src.get("amended_through"),
            "old_url": old_src.get("url"),
            "new_url": new_src.get("url"),
            "cells_changed": cells[:500],
            "cells_changed_count": len(cells),
            "uses_added": added,
            "uses_removed": removed,
        },
        "settings_changed": settings_changes,
        "blocks_looked_up": len(lookups),
        "blocks_changed": blocks,
        "lookup_failures": failures,
        "outcome_flips": flips,
        "score_changes": scores,
    }


def apply(db: Client, run_id: int, reviewed_by: str) -> dict:
    """Make a reviewed refresh the data in force. Raises RefreshError."""
    reviewed_by = (reviewed_by or "").strip()
    if not reviewed_by:
        raise RefreshError("Applying needs the reviewer's name.")
    runs = ZoningRunRepository(db)
    run = runs.get(run_id, with_proposed=True)
    if not run:
        raise RefreshError(f"Refresh #{run_id} not found.")
    if run["status"] != "ready":
        raise RefreshError(f"Refresh #{run_id} is {run['status']}; only a refresh ready for review can be applied.")

    versions = ZoningVersionRepository(db)
    latest = versions.latest_number()
    if latest != run["inputs"].get("base_version"):
        raise RefreshError(f"Version {latest} was applied after this refresh ran, so its diff is out of date. "
                           "Discard it and start a new refresh.")

    proposed = run["proposed"] or {}
    zoning.refresh_active(db)
    use_table = proposed.get("use_table") or zoning.load_use_table()
    version = latest + 1
    versions.insert({
        "version": version,
        "use_table": use_table,
        "settings": proposed.get("settings") or zoning.active_settings(),
        "applied_by": reviewed_by,
        "note": run.get("note"),
        "run_id": run_id,
    })
    zoning.refresh_active(db)

    lookups = proposed.get("lookups") or {}
    if lookups:
        rows = []
        for key, lk in lookups.items():
            rows += zoning.signal_rows(int(key), lk, f"zoning-refresh-{run_id}")
        ZoningRepository(db).replace_for_blocks([int(k) for k in lookups], rows)

    runs.update(run_id, {"status": "applied", "reviewed_by": reviewed_by, "reviewed_at": _now(),
                         "applied_version": version})
    return {"version": version, "blocks_updated": len(lookups)}


def discard(db: Client, run_id: int, reviewed_by: str | None) -> None:
    runs = ZoningRunRepository(db)
    run = runs.get(run_id)
    if not run:
        raise RefreshError(f"Refresh #{run_id} not found.")
    if run["status"] not in ("ready", "failed"):
        raise RefreshError(f"Refresh #{run_id} is {run['status']} and can't be discarded.")
    runs.update(run_id, {"status": "discarded", "reviewed_by": (reviewed_by or "").strip() or None,
                         "reviewed_at": _now(), "proposed": None})
