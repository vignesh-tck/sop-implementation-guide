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

    def get_pending_review(self, thread_id: str) -> list[dict]:
        result = (
            self._db.table(self._table)
            .select("*")
            .eq("thread_id", thread_id)
            .eq("reviewed", False)
            .execute()
        )
        return result.data or []

    def mark_reviewed(self, signal_id: int) -> None:
        self._db.table(self._table).update({"reviewed": True}).eq("id", signal_id).execute()

    def upsert_signal(self, data: dict) -> dict:
        return self.upsert(data, on_conflict="block_id,program_name")
