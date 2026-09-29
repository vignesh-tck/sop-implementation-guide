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

    def upsert_signal(self, data: dict) -> dict:
        return self.upsert(data, on_conflict="block_id,program_name")
