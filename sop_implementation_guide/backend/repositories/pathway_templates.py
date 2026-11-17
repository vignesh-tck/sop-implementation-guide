from postgrest.exceptions import APIError
from supabase import Client

from backend.repositories.base import BaseRepository
from backend.repositories.plans import _is_missing_table


class TemplateTableMissing(Exception):
    """pathway_templates has not been created in Supabase yet."""


class PathwayTemplateRepository(BaseRepository):
    """
    Versions of the pathway templates saved from the app, newest wins.

    Rows are only ever inserted, never updated, so every earlier version stays
    available to restore. Like implementation_plans, the table is created by
    pasting its section of db/schema.sql into the Supabase SQL editor; until
    then reads raise TemplateTableMissing and the caller falls back to the file.
    """

    def __init__(self, client: Client):
        super().__init__(client, "pathway_templates")

    def _run(self, query):
        try:
            return query.execute()
        except APIError as e:
            if _is_missing_table(e):
                raise TemplateTableMissing() from e
            raise

    def latest(self) -> dict | None:
        result = self._run(
            self._db.table(self._table).select("*").order("version", desc=True).limit(1)
        )
        return (result.data or [None])[0]

    def get_version(self, version: int) -> dict | None:
        result = self._run(
            self._db.table(self._table).select("*").eq("version", version).maybe_single()
        )
        return result.data if result else None

    def history(self, limit: int = 25) -> list[dict]:
        result = self._run(
            self._db.table(self._table)
            .select("version, updated_by, note, created_at")
            .order("version", desc=True)
            .limit(limit)
        )
        return result.data or []

    def insert(self, data: dict) -> dict:
        result = self._run(self._db.table(self._table).insert(data))
        return result.data[0] if result.data else {}
