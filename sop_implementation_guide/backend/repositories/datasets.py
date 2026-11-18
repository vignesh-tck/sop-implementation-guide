from datetime import datetime, timezone

from postgrest.exceptions import APIError
from supabase import Client

from backend.repositories.base import BaseRepository
from backend.repositories.plans import _is_missing_table


class DatasetTablesMissing(Exception):
    """dataset_versions / dataset_records / block_analyses have not been created in Supabase yet."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _DatasetRepository(BaseRepository):
    """Shared handling: a missing table raises DatasetTablesMissing, not a bare API error."""

    def _run(self, query):
        try:
            return query.execute()
        except APIError as e:
            if _is_missing_table(e):
                raise DatasetTablesMissing() from e
            raise


class DatasetVersionRepository(_DatasetRepository):
    """
    Numbered snapshots of the funding catalogue and the zoning data, per kind.

    Insert-only apart from the optional name and note: a version is what a
    block analysis was run against, so its records never change once written.
    """

    SUMMARY = "id, kind, version, name, note, source, record_count, summary, created_by, created_at"

    def __init__(self, client: Client):
        super().__init__(client, "dataset_versions")

    def latest(self, kind: str) -> dict | None:
        result = self._run(
            self._db.table(self._table).select(self.SUMMARY)
            .eq("kind", kind).order("version", desc=True).limit(1)
        )
        rows = result.data or []
        return rows[0] if rows else None

    def get(self, kind: str, version: int) -> dict | None:
        result = self._run(
            self._db.table(self._table).select(self.SUMMARY)
            .eq("kind", kind).eq("version", version).maybe_single()
        )
        return result.data if result else None

    def get_by_id(self, version_id: int) -> dict | None:
        result = self._run(
            self._db.table(self._table).select(self.SUMMARY).eq("id", version_id).maybe_single()
        )
        return result.data if result else None

    def list(self, kind: str, limit: int = 50) -> list[dict]:
        result = self._run(
            self._db.table(self._table).select(self.SUMMARY)
            .eq("kind", kind).order("version", desc=True).limit(limit)
        )
        return result.data or []

    def create(self, data: dict) -> dict:
        result = self._run(self._db.table(self._table).insert(data))
        return result.data[0] if result.data else {}

    def update_label(self, version_id: int, data: dict) -> dict:
        result = self._run(self._db.table(self._table).update(data).eq("id", version_id))
        return result.data[0] if result.data else {}

    def delete(self, version_id: int) -> None:
        self._run(self._db.table(self._table).delete().eq("id", version_id))


class DatasetRecordRepository(_DatasetRepository):
    def __init__(self, client: Client):
        super().__init__(client, "dataset_records")

    def insert_many(self, rows: list[dict], batch: int = 200) -> None:
        for i in range(0, len(rows), batch):
            self._run(self._db.table(self._table).insert(rows[i:i + batch]))

    def for_version(self, version_id: int, record_type: str | None = None) -> list[dict]:
        """Every record of a version. Paged, since PostgREST caps a response at 1,000 rows."""
        out, page, start = [], 1000, 0
        while True:
            q = self._db.table(self._table).select("record_type, record_key, data").eq(
                "dataset_version_id", version_id)
            if record_type:
                q = q.eq("record_type", record_type)
            batch = self._run(q.order("id").range(start, start + page - 1)).data or []
            out += batch
            if len(batch) < page:
                return out
            start += page


class BlockAnalysisRepository(_DatasetRepository):
    """
    Block Funding / Block Zoning runs, each tied to one dataset version.

    At most one per (block, kind) is pinned (a partial unique index enforces
    it); that is the one plans, signals and the gold layer read.
    """

    SUMMARY = ("id, block_id, kind, dataset_version_id, dataset_version, name, note, status, "
               "thread_id, result, error, pinned, created_by, created_at, completed_at")

    def __init__(self, client: Client):
        super().__init__(client, "block_analyses")

    def create(self, data: dict) -> dict:
        result = self._run(self._db.table(self._table).insert(data))
        return result.data[0] if result.data else {}

    def create_many(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        return self._run(self._db.table(self._table).insert(rows)).data or []

    def update(self, analysis_id: int, data: dict) -> None:
        self._run(self._db.table(self._table).update(data).eq("id", analysis_id))

    def get(self, analysis_id: int) -> dict | None:
        result = self._run(
            self._db.table(self._table).select(self.SUMMARY).eq("id", analysis_id).maybe_single()
        )
        return result.data if result else None

    def list_for_block(self, block_id: int, kind: str | None = None, limit: int = 50) -> list[dict]:
        q = self._db.table(self._table).select(self.SUMMARY).eq("block_id", block_id)
        if kind:
            q = q.eq("kind", kind)
        return self._run(q.order("created_at", desc=True).limit(limit)).data or []

    def pinned(self, block_id: int, kind: str) -> dict | None:
        result = self._run(
            self._db.table(self._table).select(self.SUMMARY)
            .eq("block_id", block_id).eq("kind", kind).eq("pinned", True).limit(1)
        )
        rows = result.data or []
        return rows[0] if rows else None

    def pinned_for_kind(self, kind: str) -> dict[int, dict]:
        """block_id -> pinned analysis, for every block that has one."""
        rows = self._run(
            self._db.table(self._table).select(self.SUMMARY).eq("kind", kind).eq("pinned", True)
        ).data or []
        return {r["block_id"]: r for r in rows}

    def unpin(self, block_ids: list[int], kind: str) -> None:
        if block_ids:
            self._run(
                self._db.table(self._table).update({"pinned": False})
                .in_("block_id", block_ids).eq("kind", kind).eq("pinned", True)
            )

    def pin(self, analysis: dict) -> None:
        """Make this analysis the block's pinned one for its kind. Unpin first: the index allows one."""
        self.unpin([analysis["block_id"]], analysis["kind"])
        self.update(analysis["id"], {"pinned": True})

    def complete(self, analysis_id: int, result: dict | None = None, status: str = "complete") -> None:
        self.update(analysis_id, {"status": status, "result": result, "completed_at": _now()})
