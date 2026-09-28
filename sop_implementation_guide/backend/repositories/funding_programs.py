from datetime import date, datetime
from typing import Any

from supabase import Client

from backend.repositories.base import BaseRepository


def _jsonable(data: dict) -> dict:
    """Serialize dates for the PostgREST JSON body.

    The Supabase client posts JSON, which has no date type, so a datetime.date
    from Grants.gov would fail to encode.
    """
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


class FundingProgramRepository(BaseRepository):
    """The synced/curated funding catalog — read by agents, written by sync scripts."""

    def __init__(self, client: Client):
        super().__init__(client, "funding_programs")

    def upsert_program(self, data: dict) -> dict:
        return self.upsert(_jsonable(data), on_conflict="program_key")

    def upsert_programs(self, rows: list[dict]) -> list[dict]:
        return self.upsert_many([_jsonable(r) for r in rows], on_conflict="program_key")

    def get_by_key(self, program_key: str) -> dict | None:
        result = (
            self._db.table(self._table)
            .select("*")
            .eq("program_key", program_key)
            .maybe_single()
            .execute()
        )
        return result.data if result else None

    def list_candidates(self, limit: int = 50) -> list[dict]:
        """
        Candidate programs for a block, most actionable first.

        Ordering puts programs with a real deadline ahead of open-ended ones, since
        a concrete date is more useful to a city planner than an evergreen notice.
        """
        result = (
            self._db.table(self._table)
            .select("*")
            .order("deadline", desc=False, nullsfirst=False)
            .limit(limit)
            .execute()
        )
        return result.data or []


class TractEligibilityRepository(BaseRepository):
    """Per-tract funding designations (the minimal geo_lookup)."""

    def __init__(self, client: Client):
        super().__init__(client, "tract_eligibility")

    def upsert_determination(self, data: dict) -> dict:
        return self.upsert(_jsonable(data), on_conflict="tract_geoid,program_key")

    def get_for_tract(self, tract_geoid: str) -> list[dict]:
        result = (
            self._db.table(self._table)
            .select("*")
            .eq("tract_geoid", tract_geoid)
            .execute()
        )
        return result.data or []

    def designations_for_tract(self, tract_geoid: str) -> dict[str, dict]:
        """Map of program_key -> determination row, for O(1) lookup by the agent."""
        return {r["program_key"]: r for r in self.get_for_tract(tract_geoid)}
