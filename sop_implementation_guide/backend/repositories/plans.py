from postgrest.exceptions import APIError
from supabase import Client

from backend.repositories.base import BaseRepository


class PlanTableMissing(Exception):
    """implementation_plans has not been created in Supabase yet."""


def _is_missing_table(e: APIError) -> bool:
    # PGRST205 is PostgREST's "table not in the schema cache"; 42P01 is Postgres's own.
    return e.code in ("PGRST205", "42P01")


class PlanRepository(BaseRepository):
    """
    Reviewer-saved implementation plans, one per (block, recommendation).

    The table is created by pasting the IMPLEMENTATION PLANS section of
    db/schema.sql into the Supabase SQL editor — PostgREST cannot run DDL. Until
    then, reads behave as if nothing has been saved (a plan still renders from
    its template) and writes raise PlanTableMissing, so the API can say exactly
    what to do instead of returning a bare 500.
    """

    def __init__(self, client: Client):
        super().__init__(client, "implementation_plans")

    def _run(self, query):
        try:
            return query.execute()
        except APIError as e:
            if _is_missing_table(e):
                raise PlanTableMissing() from e
            raise

    def get_for_rec(self, block_id: int, rec_id: int) -> dict | None:
        result = self._run(
            self._db.table(self._table).select("*")
            .eq("block_id", block_id).eq("rec_id", rec_id).maybe_single()
        )
        return result.data if result else None

    def list_for_block(self, block_id: int) -> list[dict]:
        result = self._run(
            self._db.table(self._table)
            .select("rec_id, status, reviewed_by, updated_at")
            .eq("block_id", block_id)
        )
        return result.data or []

    def upsert_plan(self, data: dict) -> dict:
        result = self._run(
            self._db.table(self._table).upsert(data, on_conflict="block_id,rec_id")
        )
        return result.data[0] if result.data else {}
