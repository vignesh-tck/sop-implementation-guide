from supabase import Client
from backend.repositories.base import BaseRepository


class FundingRepository(BaseRepository):
    def __init__(self, client: Client):
        super().__init__(client, "funding_signals")

    def get_for_block(self, block_id: int, reviewed_only: bool = False) -> list[dict]:
        q = self._db.table(self._table).select("*").eq("block_id", block_id)
        if reviewed_only:
            q = q.eq("reviewed", True)
        return q.execute().data or []

    def get_block_id_for_thread(self, thread_id: str) -> int | None:
        """
        Recover the block a thread wrote signals for.

        write_signals stamps thread_id and block_id on every row, so this survives
        the in-memory checkpointer being wiped by a server restart.
        """
        result = (
            self._db.table(self._table)
            .select("block_id")
            .eq("thread_id", thread_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return rows[0]["block_id"] if rows else None

    def get_for_rec(self, rec_id: int) -> list[dict]:
        """Signals written against one block recommendation, strongest fit first."""
        result = (
            self._db.table(self._table)
            .select("*")
            .eq("rec_id", rec_id)
            .order("relevance_score", desc=True)
            .execute()
        )
        return result.data or []

    def delete_for_rec(
        self, block_id: int, rec_id: int | None, program_names: list[str]
    ) -> list[int]:
        """
        Remove rejected signals, returning the ids actually deleted.

        Rejecting has to delete, not skip. The upsert key is
        (block_id, rec_id, program_name), so a row kept in an earlier review
        survives a later one that unticks it — the reviewer's second decision
        would silently not take.

        Grouped by recommendation so rejecting a whole recommendation costs one
        request rather than one per programme.
        """
        if not program_names:
            return []
        q = self._db.table(self._table).delete().eq("block_id", block_id)
        # Rows written before rec_id existed carry NULL, and eq() never matches a
        # NULL — those need is_ instead.
        q = q.is_("rec_id", "null") if rec_id is None else q.eq("rec_id", rec_id)
        result = q.in_("program_name", program_names).execute()
        return [r["id"] for r in (result.data or []) if r.get("id")]

    def upsert_signal(self, data: dict) -> dict:
        """
        Idempotent per (block, recommendation, programme).

        The same programme can legitimately appear under several recommendations,
        so rec_id is part of the conflict target. Rows written before rec_id
        existed carry NULL there, and Postgres treats NULLs as distinct in a
        UNIQUE constraint — those legacy rows will never match this upsert and
        accumulate instead of updating. Clear them once rather than trying to
        merge them.
        """
        return self.upsert(data, on_conflict="block_id,rec_id,program_name")
