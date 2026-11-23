from datetime import datetime, timezone

from postgrest.exceptions import APIError
from postgrest.types import ReturnMethod
from supabase import Client

from backend.repositories.base import BaseRepository
from backend.repositories.plans import _is_missing_table


class FeasibilityTableMissing(Exception):
    """feasibility_assessments has not been created in Supabase yet."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FeasibilityRepository(BaseRepository):
    """
    Feasibility assessments, one row per run on one (block, recommendation).

    At most one per recommendation is current (a partial unique index enforces
    it): the approved one the block page and the gold layer read.
    """

    # Lists leave out the evidence pack, which only the one-assessment view needs.
    LIGHT = ("id, block_id, rec_id, rec_label, status, inputs, proposed, proposed_score, score, band, "
             "decision, current, model, error, created_by, approved_by, approved_at, created_at, completed_at")

    def __init__(self, client: Client):
        super().__init__(client, "feasibility_assessments")

    def _run(self, query):
        try:
            return query.execute()
        except APIError as e:
            if _is_missing_table(e):
                raise FeasibilityTableMissing() from e
            raise

    def create(self, data: dict) -> dict:
        return (self._run(self._db.table(self._table).insert(data)).data or [{}])[0]

    def get(self, assessment_id: int) -> dict | None:
        result = self._run(self._db.table(self._table).select("*").eq("id", assessment_id).maybe_single())
        return result.data if result else None

    def update(self, assessment_id: int, data: dict) -> None:
        self._run(self._db.table(self._table).update(data, returning=ReturnMethod.minimal).eq("id", assessment_id))

    def for_block(self, block_id: int, rec_id: int | None = None, limit: int = 200) -> list[dict]:
        """The block's assessments (or one recommendation's), newest first."""
        q = self._db.table(self._table).select(self.LIGHT).eq("block_id", block_id)
        if rec_id is not None:
            q = q.eq("rec_id", rec_id)
        return self._run(q.order("created_at", desc=True).limit(limit)).data or []

    def current_for_block(self, block_id: int) -> list[dict]:
        return self._run(
            self._db.table(self._table).select(self.LIGHT).eq("block_id", block_id).eq("current", True)
        ).data or []

    def discard_open(self, block_id: int, rec_id: int) -> None:
        """Discard the recommendation's assessments awaiting review or failed: a new run replaces them."""
        self._run(
            self._db.table(self._table).update({"status": "discarded"}, returning=ReturnMethod.minimal)
            .eq("block_id", block_id).eq("rec_id", rec_id).in_("status", ["in_review", "failed"])
        )

    def make_current(self, row: dict, data: dict) -> None:
        """Approve this assessment as the recommendation's current one. Unset first: the index allows one."""
        self._run(
            self._db.table(self._table).update({"current": False}, returning=ReturnMethod.minimal)
            .eq("block_id", row["block_id"]).eq("rec_id", row["rec_id"]).eq("current", True)
        )
        self.update(row["id"], {**data, "current": True, "status": "approved", "approved_at": _now()})

    def finish(self, assessment_id: int, data: dict) -> None:
        self.update(assessment_id, {**data, "completed_at": _now()})
