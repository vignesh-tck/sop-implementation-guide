from datetime import datetime, timedelta, timezone

from postgrest.exceptions import APIError
from supabase import Client

from backend.repositories.base import BaseRepository
from backend.repositories.plans import _is_missing_table

# A turn or run that has been "busy" this long is assumed dead (the server
# restarted mid-turn), so the pipeline is usable again.
STALE_AFTER = timedelta(minutes=10)


class PipelineTablesMissing(Exception):
    """pipelines / pipeline_messages / pipeline_runs have not been created in Supabase yet."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _PipelineRepository(BaseRepository):
    def _run(self, query):
        try:
            return query.execute()
        except APIError as e:
            if _is_missing_table(e):
                raise PipelineTablesMissing() from e
            raise


class PipelineRepository(_PipelineRepository):
    SUMMARY = "id, kind, name, definition_version, busy, busy_since, created_by, created_at, updated_at"

    def __init__(self, client: Client):
        super().__init__(client, "pipelines")

    def create(self, data: dict) -> dict:
        return self._run(self._db.table(self._table).insert(data)).data[0]

    def get(self, pipeline_id: int) -> dict | None:
        result = self._run(self._db.table(self._table).select("*").eq("id", pipeline_id).maybe_single())
        return result.data if result else None

    def list(self, kind: str | None = None) -> list[dict]:
        q = self._db.table(self._table).select(self.SUMMARY + ", definition")
        if kind:
            q = q.eq("kind", kind)
        return self._run(q.order("updated_at", desc=True)).data or []

    def update(self, pipeline_id: int, data: dict) -> None:
        self._run(self._db.table(self._table).update({**data, "updated_at": _now()}).eq("id", pipeline_id))

    def delete(self, pipeline_id: int) -> None:
        self._run(self._db.table(self._table).delete().eq("id", pipeline_id))

    @staticmethod
    def is_busy(p: dict) -> bool:
        if not p.get("busy"):
            return False
        since = p.get("busy_since")
        if not since:
            return True
        started = datetime.fromisoformat(since.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - started < STALE_AFTER

    def claim(self, pipeline_id: int, what: str) -> bool:
        """Mark busy unless already busy. Not atomic, which is fine for one planner at a time."""
        p = self.get(pipeline_id)
        if not p or self.is_busy(p):
            return False
        self.update(pipeline_id, {"busy": what, "busy_since": _now()})
        return True

    def release(self, pipeline_id: int) -> None:
        self.update(pipeline_id, {"busy": None, "busy_since": None})


class PipelineMessageRepository(_PipelineRepository):
    def __init__(self, client: Client):
        super().__init__(client, "pipeline_messages")

    def add(self, pipeline_id: int, role: str, content) -> dict:
        return self._run(self._db.table(self._table).insert(
            {"pipeline_id": pipeline_id, "role": role, "content": content})).data[0]

    def for_pipeline(self, pipeline_id: int) -> list[dict]:
        out, page, start = [], 1000, 0
        while True:
            batch = self._run(
                self._db.table(self._table).select("*").eq("pipeline_id", pipeline_id)
                .order("id").range(start, start + page - 1)).data or []
            out += batch
            if len(batch) < page:
                return out
            start += page


class PipelineRunRepository(_PipelineRepository):
    SUMMARY = ("id, pipeline_id, definition_version, name, coverage, diff, dataset_version_id, "
               "dataset_version, created_by, created_at")

    def __init__(self, client: Client):
        super().__init__(client, "pipeline_runs")

    def create(self, data: dict) -> dict:
        return self._run(self._db.table(self._table).insert(data)).data[0]

    def for_pipeline(self, pipeline_id: int, limit: int = 20) -> list[dict]:
        return self._run(
            self._db.table(self._table).select(self.SUMMARY).eq("pipeline_id", pipeline_id)
            .order("id", desc=True).limit(limit)).data or []

    def latest_published(self, pipeline_id: int) -> dict | None:
        """The last load's records, to show what a new run changes since it was published."""
        rows = self._run(
            self._db.table(self._table).select("id, dataset_version, name, records, created_at")
            .eq("pipeline_id", pipeline_id).order("id", desc=True).limit(1)).data or []
        return rows[0] if rows else None

    def latest(self, pipeline_id: int) -> dict | None:
        rows = self._run(
            self._db.table(self._table).select("id, definition, definition_version")
            .eq("pipeline_id", pipeline_id).order("id", desc=True).limit(1)).data or []
        return rows[0] if rows else None
