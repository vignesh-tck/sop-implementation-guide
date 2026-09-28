from typing import Any, Generic, TypeVar
from supabase import Client

T = TypeVar("T")


class BaseRepository(Generic[T]):
    """Agents never write SQL directly — all DB access goes through a Repository."""

    def __init__(self, client: Client, table: str):
        self._db = client
        self._table = table

    def get_by_id(self, id: Any) -> dict | None:
        # maybe_single() returns None for zero rows; single() raises.
        result = (
            self._db.table(self._table).select("*").eq("id", id).maybe_single().execute()
        )
        return result.data if result else None

    def list_all(self) -> list[dict]:
        result = self._db.table(self._table).select("*").execute()
        return result.data or []

    def upsert(self, data: dict, on_conflict: str = "id") -> dict:
        """INSERT ON CONFLICT DO UPDATE — agents are safe to re-run."""
        result = (
            self._db.table(self._table)
            .upsert(data, on_conflict=on_conflict)
            .execute()
        )
        return result.data[0] if result.data else {}

    def upsert_many(self, rows: list[dict], on_conflict: str = "id") -> list[dict]:
        result = (
            self._db.table(self._table)
            .upsert(rows, on_conflict=on_conflict)
            .execute()
        )
        return result.data or []

    def delete(self, id: Any) -> None:
        self._db.table(self._table).delete().eq("id", id).execute()
