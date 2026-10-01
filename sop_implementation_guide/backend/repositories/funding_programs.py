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
    """The funding catalogue — written by discovery sessions, read by FundingAgent."""

    def __init__(self, client: Client):
        super().__init__(client, "funding_programs")

    def upsert_program(self, data: dict) -> dict:
        return self.upsert(_jsonable(data), on_conflict="program_key")

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

    def get_by_keys(self, program_keys: list[str]) -> dict[str, dict]:
        """Current catalogue rows for the given keys, indexed by program_key."""
        if not program_keys:
            return {}
        result = (
            self._db.table(self._table)
            .select("*")
            .in_("program_key", program_keys)
            .execute()
        )
        return {r["program_key"]: r for r in (result.data or [])}

    def sessions_from_catalogue(self, limit: int = 500) -> list[dict]:
        """
        Past discovery sessions reconstructed by grouping the catalogue.

        This is the fallback for sessions that ran before discovery_sessions
        existed — it is the only way to see that history, since nothing else
        recorded it. It is lossy by nature: a program re-found by a later session
        carries the newer thread_id, so an older session can look emptier than it
        was. Recorded sessions take precedence wherever both exist.
        """
        result = (
            self._db.table(self._table)
            .select("discovery_thread_id, program_key, program_name, program_type, "
                    "source_agency, geo_scope, deadline, source_url, "
                    "extraction_confidence, raw_data, created_at")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )

        sessions: dict[str, dict] = {}
        for row in result.data or []:
            # Rows predating discovery (the Grants.gov sync) carry no thread_id and
            # belong to no session. Filtered here rather than in the query: the
            # catalogue is small, and this keeps the null handling readable.
            thread_id = row.get("discovery_thread_id")
            if not thread_id:
                continue
            raw = row.get("raw_data") or {}
            s = sessions.setdefault(thread_id, {
                "thread_id": thread_id,
                # The goal was stamped onto every program this session wrote, so any
                # row recovers it. Older rows may predate that too.
                "goal": raw.get("goal") or "(goal not recorded)",
                "sources": [],
                "programs": [],
                "created_at": row.get("created_at"),
                "record": "derived",
            })
            s["programs"].append({
                "program_key": row.get("program_key"),
                "program_name": row.get("program_name"),
                "program_type": row.get("program_type"),
                "source_agency": row.get("source_agency"),
                "geo_scope": row.get("geo_scope"),
                "deadline": row.get("deadline"),
                "source_url": row.get("source_url"),
                "extraction_confidence": row.get("extraction_confidence"),
            })
            for url in (raw.get("session_sources") or []):
                if url not in s["sources"]:
                    s["sources"].append(url)
            # Sources a session read but extracted nothing from are unrecoverable
            # here; the per-program source_url is the floor of what we can know.
            url = row.get("source_url")
            if url and url not in s["sources"]:
                s["sources"].append(url)
            earliest = s["created_at"]
            if row.get("created_at") and (not earliest or row["created_at"] < earliest):
                s["created_at"] = row["created_at"]

        for s in sessions.values():
            s["program_count"] = len(s["programs"])
        return sorted(sessions.values(), key=lambda s: s["created_at"] or "", reverse=True)


class DiscoverySessionRepository(BaseRepository):
    """Append-only history of completed discovery sessions."""

    def __init__(self, client: Client):
        super().__init__(client, "discovery_sessions")

    def record(self, data: dict) -> dict:
        """
        Write the session's record. Keyed on thread_id so a retried write of the
        same session updates rather than duplicating.
        """
        return self.upsert(_jsonable(data), on_conflict="thread_id")

    def list_sessions(self, limit: int = 100) -> list[dict]:
        result = (
            self._db.table(self._table)
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [{**r, "record": "recorded"} for r in (result.data or [])]

    def get_session(self, thread_id: str) -> dict | None:
        result = (
            self._db.table(self._table)
            .select("*")
            .eq("thread_id", thread_id)
            .maybe_single()
            .execute()
        )
        if not result or not result.data:
            return None
        return {**result.data, "record": "recorded"}


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
