from supabase import Client
from backend.repositories.base import BaseRepository
from backend.config import settings


class GoldRepository(BaseRepository):
    def __init__(self, client: Client):
        super().__init__(client, "block_implementation_profiles")

    def get_for_block(self, block_id: int) -> dict | None:
        result = (
            self._db.table(self._table)
            .select("*")
            .eq("block_id", block_id)
            .single()
            .execute()
        )
        return result.data

    def upsert_profile(self, data: dict) -> dict:
        return self.upsert(data, on_conflict="block_id")

    def compute_feasibility_score(
        self, funding_score: float, zoning_score: float, policy_score: float
    ) -> float:
        """Weighted composite — weights configurable via .env."""
        score = (
            settings.weight_funding * funding_score
            + settings.weight_zoning * zoning_score
            + settings.weight_policy * policy_score
        )
        return round(min(max(score, 0.0), 100.0), 2)
