from supabase import Client

from backend.repositories.base import BaseRepository


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
