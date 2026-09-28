from supabase import Client
from backend.repositories.base import BaseRepository


class BlockRepository(BaseRepository):
    def __init__(self, client: Client):
        super().__init__(client, "blocks")

    def get_with_recs(self, block_id: int) -> dict | None:
        block = self.get_by_id(block_id)
        if not block:
            return None
        recs = (
            self._db.table("block_recommendations")
            .select("*")
            .eq("block_id", block_id)
            .order("rec_rank")
            .execute()
        )
        block["recommendations"] = recs.data or []
        return block

    def get_by_tract(self, tract_geoid: str) -> list[dict]:
        result = (
            self._db.table(self._table)
            .select("*")
            .eq("tract_geoid", tract_geoid)
            .execute()
        )
        return result.data or []

    def list_summary(self) -> list[dict]:
        """Return lightweight list — id, street_name, sop_index_norm, tract_geoid."""
        result = (
            self._db.table(self._table)
            .select("id, street_name, intersection_a, intersection_b, sop_index_norm, tract_geoid, median_hh_income, geometry_geojson")
            .execute()
        )
        return result.data or []


class BlockRecommendationRepository(BaseRepository):
    def __init__(self, client: Client):
        super().__init__(client, "block_recommendations")

    def get_for_block(self, block_id: int, dimension: str | None = None) -> list[dict]:
        q = (
            self._db.table(self._table)
            .select("*")
            .eq("block_id", block_id)
            .order("rec_rank")
        )
        if dimension:
            q = q.eq("dimension", dimension)
        return q.execute().data or []
