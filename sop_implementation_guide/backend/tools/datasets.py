"""
Dataset versions — numbered snapshots of the funding catalogue and the zoning data.

    snapshot_funding()   after an approved write to funding_programs
    snapshot_zoning()    after an approved write to zoning_signals / the use table

Each snapshot copies the whole working dataset into dataset_records under a
new version number for its kind (Funding v1, v2 ...; Zoning v1, v2 ...),
optionally named. Versions are immutable, so a block analysis that records
"ran against Zoning v3" can be re-read or re-run against exactly that data at
any time, whatever has been applied since.

The working tables stay what discovery and the Zoning tab write to. A
snapshot never fails the write that triggered it: the data is already saved,
and a missing version is recoverable by snapshotting again.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from contextlib import contextmanager
from typing import Iterator

from postgrest.exceptions import APIError
from supabase import Client

from backend.repositories.datasets import (
    DatasetRecordRepository,
    DatasetTablesMissing,
    DatasetVersionRepository,
)
from backend.repositories.zoning import ZoningRepository
from backend.tools import zoning

log = logging.getLogger(__name__)

KINDS = ("funding", "zoning")


# ── Snapshots ────────────────────────────────────────────────────────────────

def _create_version(db: Client, kind: str, records: list[dict], summary: dict, *,
                    name: str | None, note: str | None, source: dict, created_by: str | None) -> dict:
    """Insert the version row, then its records; a failed record insert removes the version."""
    versions = DatasetVersionRepository(db)
    for attempt in range(2):
        latest = versions.latest(kind)
        number = (latest["version"] if latest else 0) + 1
        try:
            row = versions.create({
                "kind": kind, "version": number,
                "name": (name or "").strip() or None, "note": (note or "").strip() or None,
                "source": source, "record_count": len(records), "summary": summary,
                "created_by": (created_by or "").strip() or None,
            })
            break
        except APIError as e:
            # Two snapshots raced for the same number; take the next one.
            if e.code == "23505" and attempt == 0:
                continue
            raise
    try:
        DatasetRecordRepository(db).insert_many(
            [{"dataset_version_id": row["id"], **r} for r in records]
        )
    except Exception:
        versions.delete(row["id"])
        raise
    return row


def snapshot_funding(db: Client, *, name: str | None = None, note: str | None = None,
                     source: dict | None = None, created_by: str | None = None) -> dict:
    """Snapshot the whole funding_programs catalogue as the next Funding version."""
    programs = db.table("funding_programs").select("*").order("id").execute().data or []
    records = [
        {"record_type": "program", "record_key": p["program_key"], "data": p}
        for p in programs
    ]
    summary = {
        "programs": len(programs),
        "by_type": _count(p.get("program_type") or "unknown" for p in programs),
        "with_deadline": sum(1 for p in programs if p.get("deadline")),
    }
    return _create_version(db, "funding", records, summary, name=name, note=note,
                           source=source or {"type": "manual"}, created_by=created_by)


def snapshot_zoning(db: Client, *, name: str | None = None, note: str | None = None,
                    source: dict | None = None, created_by: str | None = None) -> dict:
    """
    Snapshot the zoning data in force as the next Zoning version: the use table,
    the rec-to-use rules, the GIS settings and every block's stored zones.
    """
    zoning.refresh_active(db)
    rows = ZoningRepository(db).get_all()
    rules = zoning.load_rules()
    records = [
        {"record_type": "use_table", "record_key": "use_table", "data": zoning.load_use_table()},
        {"record_type": "rules", "record_key": "rules", "data": rules},
        {"record_type": "settings", "record_key": "settings", "data": zoning.active_settings()},
    ] + [
        {"record_type": "block_zone", "record_key": f"{r['block_id']}:{r.get('current_zone') or '-'}:{r['id']}",
         "data": r}
        for r in rows
    ]
    blocks = {r["block_id"] for r in rows}
    summary = {
        "blocks": len(blocks),
        "zones": sorted({r["current_zone"] for r in rows if r.get("current_zone")}),
        "use_rows": len(zoning.load_use_table().get("uses", [])),
        "rules_version": rules.get("version"),
        "zoning_data_version": zoning.active_version()["version"],
    }
    return _create_version(db, "zoning", records, summary, name=name, note=note,
                           source=source or {"type": "manual"}, created_by=created_by)


def try_snapshot(kind: str, db: Client, **kwargs) -> dict | None:
    """Snapshot, but never fail the caller's already-saved write. Returns the version or None."""
    fn = snapshot_funding if kind == "funding" else snapshot_zoning
    try:
        return fn(db, **kwargs)
    except DatasetTablesMissing:
        log.warning("dataset_versions tables missing; %s write not versioned", kind)
    except Exception as e:  # noqa: BLE001
        log.warning("could not snapshot %s dataset: %s", kind, e)
    return None


def _count(values) -> dict:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


# ── Reading a version ────────────────────────────────────────────────────────

def resolve(db: Client, kind: str, version: int | None = None) -> dict | None:
    """The requested version of a kind, or its latest when version is None. None if there are none."""
    repo = DatasetVersionRepository(db)
    try:
        return repo.get(kind, version) if version is not None else repo.latest(kind)
    except DatasetTablesMissing:
        return None


def label(row: dict | None) -> dict | None:
    """The short form of a version that travels with analyses and API responses."""
    if not row:
        return None
    return {"id": row["id"], "kind": row["kind"], "version": row["version"], "name": row.get("name"),
            "created_at": row.get("created_at")}


def funding_programs(db: Client, version_id: int) -> list[dict]:
    """
    A Funding version's programmes, most actionable first: programmes with a
    real deadline ahead of open-ended ones, as FundingProgramRepository orders
    the live catalogue.
    """
    rows = [r["data"] for r in DatasetRecordRepository(db).for_version(version_id, "program")]
    rows.sort(key=lambda p: (p.get("deadline") is None, p.get("deadline") or ""))
    return rows


# Versions never change, so a loaded zoning bundle is cached for the process.
# A bundle holds the ~0.4 MB use table, hence the small bound.
_ZONING_CACHE: "OrderedDict[int, dict]" = OrderedDict()


def zoning_bundle(db: Client, version_id: int) -> dict:
    """A Zoning version's use table, rules, settings and per-block zone rows."""
    hit = _ZONING_CACHE.get(version_id)
    if hit is not None:
        _ZONING_CACHE.move_to_end(version_id)
        return hit
    bundle: dict = {"use_table": None, "rules": None, "settings": None, "rows": {}}
    for r in DatasetRecordRepository(db).for_version(version_id):
        t = r["record_type"]
        if t == "block_zone":
            bundle["rows"].setdefault(r["data"]["block_id"], []).append(r["data"])
        elif t in ("use_table", "rules", "settings"):
            bundle[t] = r["data"]
    for rows in bundle["rows"].values():
        rows.sort(key=lambda x: x.get("current_zone") or "")
    _ZONING_CACHE[version_id] = bundle
    while len(_ZONING_CACHE) > 3:
        _ZONING_CACHE.popitem(last=False)
    return bundle


@contextmanager
def zoning_scope(db: Client, version: dict | None) -> Iterator[dict | None]:
    """
    Evaluate zoning against a dataset version for the duration of the block.

    Yields the version's bundle, whose "rows" stand in for zoning_signals. With
    no version (no snapshots yet, or the tables are missing) it yields None and
    the data in force applies, as before versioning existed.
    """
    zoning.refresh_active(db)
    if version is None:
        yield None
        return
    bundle = zoning_bundle(db, version["id"])
    with zoning.using(bundle["use_table"], bundle["rules"], bundle["settings"]):
        yield bundle
