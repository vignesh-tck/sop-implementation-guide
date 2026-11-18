from supabase import Client
from backend.repositories.base import BaseRepository
from backend.repositories.datasets import BlockAnalysisRepository


class FundingRepository(BaseRepository):
    """
    Funding signals, each belonging to the Block Funding analysis that wrote it.

    A block can hold several analyses (one per run, each against a Funding
    dataset version); reads default to the block's pinned one. Rows written
    before analyses existed carry analysis_id NULL and are what a block with
    no pinned analysis shows.
    """

    def __init__(self, client: Client):
        super().__init__(client, "funding_signals")

    def get_for_block(self, block_id: int, reviewed_only: bool = False,
                      analysis_id: int | None = None) -> list[dict]:
        """The block's signals from analysis_id, or from its pinned funding analysis when omitted."""
        if analysis_id is None:
            pinned = BlockAnalysisRepository(self._db).pinned(block_id, "funding")
            analysis_id = pinned["id"] if pinned else None
        q = self._db.table(self._table).select("*").eq("block_id", block_id)
        q = q.is_("analysis_id", "null") if analysis_id is None else q.eq("analysis_id", analysis_id)
        if reviewed_only:
            q = q.eq("reviewed", True)
        return q.execute().data or []

    def get_block_id_for_thread(self, thread_id: str) -> int | None:
        """
        Recover the block a thread wrote signals for.

        write_signals stamps thread_id and block_id on every row, so this survives
        the in-memory checkpointer being wiped by a server restart.
        """
        result = (
            self._db.table(self._table)
            .select("block_id")
            .eq("thread_id", thread_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return rows[0]["block_id"] if rows else None

    def get_for_rec(self, rec_id: int) -> list[dict]:
        """Signals written against one block recommendation, strongest fit first."""
        result = (
            self._db.table(self._table)
            .select("*")
            .eq("rec_id", rec_id)
            .order("relevance_score", desc=True)
            .execute()
        )
        return result.data or []

    def delete_for_rec(
        self, block_id: int, rec_id: int | None, program_names: list[str],
        analysis_id: int | None = None,
    ) -> list[int]:
        """
        Remove rejected signals, returning the ids actually deleted.

        Rejecting has to delete, not skip. The upsert key is
        (block_id, rec_id, program_name), so a row kept in an earlier review
        survives a later one that unticks it — the reviewer's second decision
        would silently not take.

        Grouped by recommendation so rejecting a whole recommendation costs one
        request rather than one per programme.

        Scoped to one analysis: rejecting a match in a new run must not delete
        the same match from an earlier run the block may be pinned to.
        """
        if not program_names:
            return []
        q = self._db.table(self._table).delete().eq("block_id", block_id)
        q = q.is_("analysis_id", "null") if analysis_id is None else q.eq("analysis_id", analysis_id)
        # Rows written before rec_id existed carry NULL, and eq() never matches a
        # NULL — those need is_ instead.
        q = q.is_("rec_id", "null") if rec_id is None else q.eq("rec_id", rec_id)
        result = q.in_("program_name", program_names).execute()
        return [r["id"] for r in (result.data or []) if r.get("id")]

    def upsert_signal(self, data: dict) -> dict:
        """
        Idempotent per (analysis, recommendation, programme).

        The same programme can legitimately appear under several recommendations,
        so rec_id is part of the conflict target, and under several analyses of
        the same block, so analysis_id is too. Rows written before rec_id or
        analysis_id existed carry NULL there, and Postgres treats NULLs as
        distinct in a UNIQUE constraint, so those legacy rows never match this
        upsert. scripts/seed_dataset_versions.py gives them an analysis.
        """
        return self.upsert(data, on_conflict="analysis_id,rec_id,program_name")
