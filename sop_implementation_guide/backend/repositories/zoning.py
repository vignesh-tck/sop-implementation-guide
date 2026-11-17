from postgrest.exceptions import APIError
from supabase import Client

from backend.repositories.base import BaseRepository
from backend.repositories.plans import _is_missing_table


class ZoningRepository(BaseRepository):
    """
    Zones fronting each block, one zoning_signals row per (block, zone).

    Written only by scripts/sync_block_zoning.py. Block-level facts (prior zones,
    case numbers, overlays, when it was looked up) are the same on every row of a
    block and live in raw_data["lookup"].

    A block with no rows has not been looked up. A lookup that found no zone at
    all writes one row with current_zone NULL, so "not looked up" and "no zone"
    stay distinguishable.
    """

    def __init__(self, client: Client):
        super().__init__(client, "zoning_signals")

    def get_for_block(self, block_id: int) -> list[dict]:
        result = (
            self._db.table(self._table).select("*")
            .eq("block_id", block_id).order("current_zone").execute()
        )
        return result.data or []

    def get_all(self) -> list[dict]:
        """Every block's rows. Paged, since PostgREST caps a response at 1,000 rows."""
        rows, page, start = [], 1000, 0
        while True:
            batch = (
                self._db.table(self._table).select("*")
                .order("block_id").order("id").range(start, start + page - 1).execute()
            ).data or []
            rows += batch
            if len(batch) < page:
                return rows
            start += page

    def replace_for_blocks(self, block_ids: list[int], rows: list[dict]) -> None:
        """Swap many blocks' rows at once: one delete and one insert, not two calls per block."""
        if not block_ids:
            return
        self._db.table(self._table).delete().in_("block_id", block_ids).execute()
        if rows:
            self._db.table(self._table).insert(rows).execute()

    def replace_for_block(self, block_id: int, rows: list[dict]) -> list[dict]:
        """
        Swap a block's rows for a fresh lookup.

        Delete-then-insert rather than upsert: parcel_id is NULL on these rows
        (zones are looked up per block, not per parcel), and NULLs never collide
        on the (block_id, parcel_id) unique key, so an upsert would duplicate.
        """
        self._db.table(self._table).delete().eq("block_id", block_id).execute()
        if not rows:
            return []
        result = self._db.table(self._table).insert(rows).execute()
        return result.data or []


class ZoningTablesMissing(Exception):
    """zoning_data_versions / zoning_refresh_runs have not been created in Supabase yet."""


class _ZoningAdminRepository(BaseRepository):
    """Shared handling: a missing table raises ZoningTablesMissing, not a bare API error."""

    def _run(self, query):
        try:
            return query.execute()
        except APIError as e:
            if _is_missing_table(e):
                raise ZoningTablesMissing() from e
            raise


class ZoningVersionRepository(_ZoningAdminRepository):
    """
    Applied versions of the zoning data (use table + GIS settings), newest wins.

    Insert-only, so every earlier version stays available. Version 0 is the
    committed file and default settings, and has no row.
    """

    def __init__(self, client: Client):
        super().__init__(client, "zoning_data_versions")

    def latest_number(self) -> int:
        result = self._run(
            self._db.table(self._table).select("version").order("version", desc=True).limit(1)
        )
        return (result.data or [{"version": 0}])[0]["version"]

    def get_version(self, version: int) -> dict | None:
        result = self._run(self._db.table(self._table).select("*").eq("version", version).maybe_single())
        return result.data if result else None

    def history(self, limit: int = 25) -> list[dict]:
        result = self._run(
            self._db.table(self._table)
            .select("version, applied_by, note, run_id, created_at, settings")
            .order("version", desc=True).limit(limit)
        )
        return result.data or []

    def insert(self, data: dict) -> dict:
        result = self._run(self._db.table(self._table).insert(data))
        return result.data[0] if result.data else {}


class ZoningRunRepository(_ZoningAdminRepository):
    """Refreshes started from the Zoning tab, each awaiting review until applied or discarded."""

    # Everything but the proposed payload, which can be several hundred KB.
    SUMMARY = ("id, status, inputs, requested_by, note, progress, diff, error, reviewed_by, "
               "applied_version, created_at, finished_at, reviewed_at")

    def __init__(self, client: Client):
        super().__init__(client, "zoning_refresh_runs")

    def create(self, data: dict) -> dict:
        result = self._run(self._db.table(self._table).insert(data))
        return result.data[0] if result.data else {}

    def update(self, run_id: int, data: dict) -> None:
        self._run(self._db.table(self._table).update(data).eq("id", run_id))

    def get(self, run_id: int, with_proposed: bool = False) -> dict | None:
        cols = "*" if with_proposed else self.SUMMARY
        result = self._run(self._db.table(self._table).select(cols).eq("id", run_id).maybe_single())
        return result.data if result else None

    def recent(self, limit: int = 10) -> list[dict]:
        result = self._run(
            self._db.table(self._table)
            .select("id, status, inputs, requested_by, note, progress, error, reviewed_by, "
                    "applied_version, created_at, finished_at, reviewed_at")
            .order("id", desc=True).limit(limit)
        )
        return result.data or []

    def running(self) -> list[dict]:
        result = self._run(self._db.table(self._table).select("id, created_at").eq("status", "running"))
        return result.data or []
