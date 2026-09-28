from supabase import Client
from backend.repositories.base import BaseRepository


class ZoningRepository(BaseRepository):
    def __init__(self, client: Client):
        super().__init__(client, "zoning_signals")

    def get_for_block(self, block_id: int) -> list[dict]:
        return self._db.table(self._table).select("*").eq("block_id", block_id).execute().data or []

    def upsert_signal(self, data: dict) -> dict:
        return self.upsert(data, on_conflict="block_id,parcel_id")
