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
            # "*" rather than named columns: `revision` only exists once the
            # IMPLEMENTATION PLAN REVISIONS section has been run.
            .select("*")
            .eq("block_id", block_id)
        )
        return result.data or []

    def upsert_plan(self, data: dict) -> dict:
        result = self._run(
            self._db.table(self._table).upsert(data, on_conflict="block_id,rec_id")
        )
        return result.data[0] if result.data else {}


class RevisionTableMissing(Exception):
    """implementation_plan_revisions has not been created in Supabase yet."""


class PlanRevisionRepository(BaseRepository):
    """
    The audit trail of implementation plans: one append-only row per draft
    saved, approval or discarded draft, numbered per (block, recommendation).

    The latest row decides whether a draft is waiting: action 'draft' means
    yes; an 'approved' or 'discarded' row after it closes it. The approved
    steps themselves are also written to implementation_plans, which is what
    everything else reads.
    """

    def __init__(self, client: Client):
        super().__init__(client, "implementation_plan_revisions")

    def _run(self, query):
        try:
            return query.execute()
        except APIError as e:
            if _is_missing_table(e):
                raise RevisionTableMissing() from e
            raise

    def list_for_rec(self, block_id: int, rec_id: int) -> list[dict]:
        """Every revision, newest first."""
        result = self._run(
            self._db.table(self._table).select("*")
            .eq("block_id", block_id).eq("rec_id", rec_id).order("revision", desc=True)
        )
        return result.data or []

    def latest_for_rec(self, block_id: int, rec_id: int) -> dict | None:
        result = self._run(
            self._db.table(self._table).select("*")
            .eq("block_id", block_id).eq("rec_id", rec_id).order("revision", desc=True).limit(1)
        )
        return (result.data or [None])[0]

    def latest_actions_for_block(self, block_id: int) -> dict[int, dict]:
        """Each recommendation's newest revision (without its steps), by rec_id."""
        result = self._run(
            self._db.table(self._table)
            .select("rec_id, revision, action, made_by, created_at")
            .eq("block_id", block_id).order("revision", desc=True)
        )
        latest: dict[int, dict] = {}
        for row in result.data or []:
            latest.setdefault(row["rec_id"], row)
        return latest

    def insert(self, data: dict) -> dict:
        result = self._run(self._db.table(self._table).insert(data))
        return result.data[0] if result.data else {}
